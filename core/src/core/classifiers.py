"""Single-request classification of one answer to one question, into classes the author defines.

What every classification is made of, whatever the business:

* the question the person was asked and their answer (`Exchange`, filled from
  the call's state by the application, never by a model);
* the author's context: what the classifier is for, in a sentence or two;
* any other facts the application knows, as more input fields, each
  described with `Field(description=...)`;
* the classes, each with its description: the output model's fields, written
  as `Literal` members with descriptions or built from plain data with
  `ClassSet` and `output_model`.

The request says the same fixed task for every classifier. Nothing in here
knows a business: a new one brings its own context, facts and classes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_ai import ModelRequest, UnexpectedModelBehavior
from pydantic_ai.direct import model_request
from pydantic_ai.messages import TextPart, ThinkingPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

TASK = (
    'You classify the answer to a question. For each output field, choose exactly one class, using the class '
    'descriptions in the output schema. Judge only from the input. When the answer does not clearly fit a '
    'class, choose the class described for an unclear answer.'
)
"""The task every classifier is given. The author's context and the facts' descriptions are added to it."""


class Exchange(BaseModel):
    """One question put to a person and their answer: what every classification reads.

    A business adds its facts as more fields, each with a description. The
    classifier lists those descriptions to the model, so a fact without one
    is sent but never explained.
    """

    model_config = ConfigDict(extra='forbid', frozen=True)
    question: str = Field(
        description='What the person was last asked, word for word. Empty when nothing was asked.'
    )
    answer: str = Field(description='What the person said back, as transcribed.')


@dataclass(frozen=True)
class ClassSet:
    """One output field defined as data: what it decides, its classes with their descriptions, and its default.

    The default is what a failed or stale reading falls back to, so it must
    be a class that authorises nothing.
    """

    about: str
    classes: Mapping[str, str]
    default: str

    def annotation(self) -> Any:
        """The field's type: one `Literal` member per class, each carrying its description."""
        if self.default not in self.classes:
            raise ValueError(f'default {self.default!r} is not one of the classes {list(self.classes)}')
        members = tuple(
            Annotated[Literal[name], Field(description=text)]  # ty: ignore[invalid-type-form]
            for name, text in self.classes.items()
        )
        return Union[members] if len(members) > 1 else members[0]  # noqa: UP007


def output_model(name: str, fields: Mapping[str, ClassSet]) -> type[BaseModel]:
    """An output model built from data, for a classifier whose classes come from configuration."""
    definitions: dict[str, Any] = {
        key: (spec.annotation(), Field(default=spec.default, description=spec.about))
        for key, spec in fields.items()
    }
    return create_model(name, __config__=ConfigDict(extra='forbid', frozen=True), **definitions)


class Classifier[InputT: Exchange, OutputT: BaseModel]:
    """Validate one input, request one structured answer, and validate that answer.

    The caller owns the model client, deadline, and any fallback. An instance
    holds configuration only and can be shared between concurrent calls.
    """

    def __init__(
        self,
        model: Model | str,
        input_type: type[InputT],
        output_type: type[OutputT],
        context: str = '',
        model_settings: ModelSettings | None = None,
    ) -> None:
        self.model = model
        self.input_type = input_type
        self.output_type = output_type
        self.context = context
        """The author's words on what this classifier is for. The only part of the instructions a business writes."""
        self.model_settings = model_settings
        self.instructions = self._instructions()

    def _instructions(self) -> str:
        """The context, the fixed task, and what each input field is, from its description."""
        fields = [
            f'- `{name}`: {field.description}'
            for name, field in self.input_type.model_fields.items()
            if field.description
        ]
        return '\n\n'.join(
            part for part in (self.context.strip(), TASK, 'The input:\n' + '\n'.join(fields)) if part
        )

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
