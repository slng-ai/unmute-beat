"""Single-request classification between arbitrary Pydantic input and output models."""

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
        schema = self.output_type.model_json_schema()
        if schema.get('type') != 'object':
            raise ValueError('Native classification requires an object-shaped output model.')
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
