"""Single-request classification between arbitrary Pydantic input and output models."""

from typing import Any

from pydantic import BaseModel
from pydantic_ai import ModelRequest, UnexpectedModelBehavior
from pydantic_ai.direct import model_request
from pydantic_ai.messages import TextPart, ThinkingPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits


class Classifier[InputT: BaseModel, OutputT: BaseModel]:
    """Validate one input, request one structured answer, and validate that answer.

    The caller owns the model client, deadline, and any fallback. An instance
    holds configuration only and can be shared between concurrent calls.
    """

    def __init__(
        self,
        model: Model | str,
        input_type: type[InputT],
        output_type: type[OutputT],
        instructions: str = 'Classify the input using the class descriptions in the output schema.',
        model_settings: ModelSettings | None = None,
    ) -> None:
        self.model = model
        self.input_type = input_type
        self.output_type = output_type
        self.instructions = instructions
        self.model_settings = model_settings

    def json_schema(self) -> dict[str, Any]:
        """The output schema exactly as it is sent: object-shaped, with every union as `anyOf`.

        A discriminated union renders as `oneOf` plus a `discriminator`, and
        Gemini rejects `oneOf`. Its members are disjoint by their tag, so
        `anyOf` means the same; the tag is made required in every member, so an
        answer that leaves it out fails validation instead of matching a
        member by its default. Validation still uses the model itself.
        """
        schema = self.output_type.model_json_schema()
        if schema.get('type') != 'object':
            raise ValueError('Native classification requires an object-shaped output model.')
        definitions = schema.get('$defs', {})

        def member(node: dict[str, Any]) -> dict[str, Any]:
            reference = node.get('$ref', '')
            return definitions[reference.rsplit('/', 1)[-1]] if reference.startswith('#/$defs/') else node

        def rewrite(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    rewrite(item)
                return
            if not isinstance(node, dict):
                return
            if 'oneOf' in node:
                node['anyOf'] = node.pop('oneOf')
                if (discriminator := node.pop('discriminator', None)) is not None:
                    tag = discriminator['propertyName']
                    for option in node['anyOf']:
                        required = member(option).setdefault('required', [])
                        if tag not in required:
                            required.insert(0, tag)
            for child in node.values():
                rewrite(child)

        rewrite(schema)
        return schema

    async def classify(
        self,
        value: InputT,
        *,
        usage: RunUsage | None = None,
        usage_limits: UsageLimits | None = None,
    ) -> OutputT:
        """Return validated output, counting even requests whose answer is invalid.

        Validation and provider errors propagate. There are no repair requests;
        cancellation and budget exhaustion retain their original exceptions.
        """
        value = self.input_type.model_validate(value.model_dump())
        schema = self.json_schema()
        account = usage if usage is not None else RunUsage()
        if usage_limits is not None:
            usage_limits.check_before_request(account)
        account.requests += 1
        response = await model_request(
            self.model,
            [ModelRequest.user_text_prompt(value.model_dump_json(), instructions=self.instructions)],
            model_settings=self.model_settings,
            model_request_parameters=ModelRequestParameters(
                output_mode='native',
                output_object=OutputObjectDefinition(json_schema=schema, name=self.output_type.__name__),
            ),
        )
        account.incr(response.usage)
        if usage_limits is not None:
            usage_limits.check_tokens(account)
        parts = [part for part in response.parts if not isinstance(part, ThinkingPart)]
        if (
            response.finish_reason not in (None, 'stop')
            or len(parts) != 1
            or not isinstance(parts[0], TextPart)
        ):
            raise UnexpectedModelBehavior('Classifier must return exactly one complete JSON text answer.')
        return self.output_type.model_validate_json(parts[0].content)
