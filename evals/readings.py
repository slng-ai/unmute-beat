"""The narrow readings as Langfuse datasets and prompts, and experiments across models.

    uv run --env-file .env python -m evals.readings build [--publish]   # datasets, and prompts as `candidate`
    uv run --env-file .env python -m evals.readings run [--label L | --version N] [--repeat N] [--split S] [MODEL...] [READING...]

One dataset and one prompt per reading (`core.readings`), named
`harness-reading/<reading>`. An item is exactly what the reading is sent (the
question, the answer, the facts it needs) plus the business's context
sentence, and its expected label. The prompt is the instructions
`core.classifiers.Classifier` builds, with the context as the one variable,
so the same prompt serves any business and a new version of it is a change
to every business at once.

Items come from three places, each marked in `metadata.label_source`: the
focused live failures (`evals.focused`), the reviewed reply cases
(`evals.cases`), and a few items from other businesses (a clinic, a
restaurant, an IT help desk...) that check the classes carry across projects.

`run` sends every item through the harness's own `Classifier`, the same
request path a live call takes, with the model swapped and the instructions
taken from the prompt version in Langfuse. Scores: exact match, false accept
(a label that would let the application act when the expected label would
not), and per run accuracy, false accepts, latency and input tokens.

`build` writes a changed prompt under the label `candidate`; only
`--publish` moves `production`, so building is never a release by accident.
The classes and their descriptions are sent from the code, not from the
prompt's config, so `run` refuses a prompt version whose recorded schema is
not what the code would send: a run is only ever labelled with the version it
really measured.
"""

import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langfuse import Evaluation, get_client
from pydantic import BaseModel
from pydantic_ai.models import infer_model
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.usage import RunUsage

from core.classifiers import TASK, Classifier, Exchange
from core.readings import DetourInput, ReadBackInput, Reading, TakeBackInput
from evals.cases import CASES as REVIEWED
from evals.consent import label_of, reading_for, reading_input
from evals.focused import CASES as FOCUSED
from evals.focused import NINE, SLOTS, TIMES
from salon.classification import (
    APPOINTMENT_CHOICE,
    CHOSEN_OFFER,
    CONFIRMATION,
    CONVERSATION,
    DETOUR,
    LAST_CHANGE,
    RETAINED_APPROVAL,
)
from salon.models.consent import OfferInput

MODELS = {
    'gpt-6-luna': 'openai:gpt-6-luna',
    'gpt-5.6-luna': 'openai:gpt-5.6-luna',
    'gemini-3.5-flash-lite': 'google-cloud:gemini-3.5-flash-lite',
    'gemini-3.1-flash-lite': 'google-cloud:gemini-3.1-flash-lite',
}
"""The models every reading is measured on. Small ones first: a reading should not need a large one."""

GEMINI = GoogleModelSettings(
    service_tier='priority',
    google_thinking_config={'thinking_level': 'MINIMAL'},  # ty: ignore[invalid-argument-type]
)
"""The harness's own settings for Gemini (`salon.classification.SETTINGS`). OpenAI runs on its defaults."""


@dataclass(frozen=True)
class Kind:
    """One reading as a dataset: its input model, the reading whose classes it uses, and what authorises an action."""

    reading: Reading
    input_type: type[Exchange]
    acts_on: frozenset[str]
    """Labels the application acts on. An empty string never acts; for a choice, any id does."""

    def acts(self, label: str) -> bool:
        return label in self.acts_on or (self.reading.field == 'choice' and label != '')


KINDS = {
    'confirm': Kind(CONFIRMATION, ReadBackInput, frozenset({'accept'})),
    'choose': Kind(CHOSEN_OFFER, OfferInput, frozenset()),
    'take-back': Kind(LAST_CHANGE, TakeBackInput, frozenset({'withdraw'})),
    'still-agreed': Kind(RETAINED_APPROVAL, TakeBackInput, frozenset({'keep'})),
    'closing': Kind(CONVERSATION, Exchange, frozenset({'end'})),
    'request-kept': Kind(DETOUR, DetourInput, frozenset({'kept'})),
}
"""`choose` uses the offer input for every item, move-or-add included, with `holding` empty where it has none."""

BY_READING = {
    CONFIRMATION.name: 'confirm',
    APPOINTMENT_CHOICE.name: 'choose',
    CHOSEN_OFFER.name: 'choose',
    LAST_CHANGE.name: 'take-back',
    RETAINED_APPROVAL.name: 'still-agreed',
    CONVERSATION.name: 'closing',
    DETOUR.name: 'request-kept',
}

FOCUSED_LABELS: dict[str, tuple[str, tuple[str, ...]]] = {
    'phone-yes-correct': ('accept', ()),
    'phone-yes-right': ('accept', ()),
    'phone-yes-got-it': ('accept', ()),
    'phone-no': ('revise', ('reject',)),
    'pick-nine': ('2026-09-25T0900|hair_colour', ()),
    'pick-conditional': ('', ()),
    'pick-after-price': ('2026-09-25T0900|hair_colour', ()),
    'pick-then-goodbye': ('2026-09-25T0900|hair_colour', ()),
    'withdraw': ('withdraw', ()),
    'keep-after-question': ('keep', ()),
    'condition-after-booking': ('unclear', ('keep',)),
    'withdraw-later': ('withdraw', ()),
    'goodbye-keeps-booking': ('keep', ()),
    'cancel-yes': ('accept', ()),
    'closing': ('end', ()),
    'closing-question': ('continue', ()),
    'choice-move': ('move', ()),
    'choice-add': ('add', ()),
    'choice-bare-yes': ('', ()),
    'choice-unasked-move': ('move', ()),
    'complaint-kept': ('keep', ()),
    'complaint-changed': ('withdraw', ()),
    'detour-kept': ('kept', ()),
    'detour-kept-pieces': ('kept', ()),
    'detour-changed': ('changed', ()),
    'detour-question': ('changed', ()),
}
"""Each focused case's expected label, and any other label that is also right. `evals.focused` checks a predicate."""

CLINIC = 'A patient is on the phone with the receptionist of a dental clinic.'
RESTAURANT = 'A guest is on the phone with the host of a restaurant.'
HELPDESK = 'An employee is on the phone with their company IT help desk.'
INSURER = 'A customer is on the phone with an agent of their home insurer.'
BANK = 'A customer is on the phone with their bank.'
COURIER = 'A customer is on the phone with a parcel courier.'

OTHER: tuple[tuple[str, str, dict[str, Any], str], ...] = (
    (
        'confirm',
        CLINIC + ' The receptionist read something back and is waiting for a yes.',
        {
            'question': "So that's a cleaning with Dr. Park on Tuesday at 4 PM. Shall I book it?",
            'answer': "Yes please, that's perfect.",
            'proposal': "So that's a cleaning with Dr. Park on Tuesday at 4 PM. Shall I book it?",
        },
        'accept',
    ),
    (
        'confirm',
        RESTAURANT + ' The host read something back and is waiting for a yes.',
        {
            'question': "That's a table for four tonight at 8. Shall I confirm it?",
            'answer': "Only if it's not next to the kitchen.",
            'proposal': "That's a table for four tonight at 8. Shall I confirm it?",
        },
        'conditional',
    ),
    (
        'confirm',
        HELPDESK + ' The agent read something back and is waiting for a yes.',
        {
            'question': "I'll reset your password and sign you out of every device. OK to go ahead?",
            'answer': 'Wait, will I lose the documents I have open?',
            'proposal': "I'll reset your password and sign you out of every device. OK to go ahead?",
        },
        'unclear',
    ),
    (
        'choose',
        CLINIC + ' The patient was offered appointment times.',
        {
            'question': 'I have Tuesday at 4 PM or Wednesday at 9 AM. Which suits you?',
            'answer': 'Wednesday morning works.',
            'options': [
                {'id': 'tue-16', 'said': 'Tuesday at 4 PM'},
                {'id': 'wed-09', 'said': 'Wednesday at 9 AM'},
            ],
            'holding': [],
        },
        'wed-09',
    ),
    (
        'choose',
        RESTAURANT + ' The guest was offered two times.',
        {
            'question': 'I can do 7 or 8 tonight. Which would you like?',
            'answer': "Either is fine, whatever's easier.",
            'options': [{'id': 't19', 'said': '7 PM'}, {'id': 't20', 'said': '8 PM'}],
            'holding': [],
        },
        '',
    ),
    (
        'choose',
        COURIER + ' The customer was asked where to leave the parcel.',
        {
            'question': 'Shall we leave it at your door, or with a neighbour?',
            'answer': 'With the neighbour, number 12, please.',
            'options': [
                {'id': 'door', 'said': 'at the door'},
                {'id': 'neighbour', 'said': 'with a neighbour'},
            ],
            'holding': [],
        },
        'neighbour',
    ),
    (
        'take-back',
        RESTAURANT
        + ' Earlier in this call the host booked a table for the guest, which they can still take back.',
        {
            'question': 'Your table for four tonight at 8 is booked. Anything else?',
            'answer': "Actually, cancel that, we'll stay in tonight.",
            'agreed': 'Your table for four tonight at 8 is booked.',
        },
        'withdraw',
    ),
    (
        'take-back',
        CLINIC
        + ' Earlier in this call the receptionist booked an appointment, which the patient can still take back.',
        {
            'question': 'Your cleaning on Tuesday at 4 PM is booked. Anything else?',
            'answer': "No, that's all. See you Tuesday.",
            'agreed': 'Your cleaning on Tuesday at 4 PM is booked.',
        },
        'keep',
    ),
    (
        'take-back',
        HELPDESK
        + ' Earlier in this call the agent reset the password, which the employee can still take back.',
        {
            'question': "I've reset your password. Anything else?",
            'answer': 'Yes, can you also unlock my account?',
            'agreed': "I've reset your password.",
        },
        'keep',
    ),
    (
        'still-agreed',
        INSURER
        + ' The customer approved a claim, and the agent is checking their policy number before filing it.',
        {
            'question': 'Can I have your policy number, please?',
            'answer': "It's P 4 4 8 2 1.",
            'agreed': 'Water damage in the kitchen on May 3rd, asking for the repair costs.',
        },
        'keep',
    ),
    (
        'still-agreed',
        INSURER
        + ' The customer approved a claim, and the agent is checking their policy number before filing it.',
        {
            'question': 'Can I have your policy number, please?',
            'answer': "It's P 4 4 8 2 1. Oh, the damage was in the bathroom, not the kitchen.",
            'agreed': 'Water damage in the kitchen on May 3rd, asking for the repair costs.',
        },
        'withdraw',
    ),
    (
        'still-agreed',
        BANK
        + ' The customer approved a transfer, and the bank is checking a security code before sending it.',
        {
            'question': 'What is the code we just texted you?',
            'answer': "It's 4 9 1 2. And make it 250 instead.",
            'agreed': 'A transfer of 200 euros to Anna Lopez.',
        },
        'withdraw',
    ),
    (
        'closing',
        CLINIC,
        {'question': 'Is there anything else I can help with?', 'answer': "No, that's everything, thanks."},
        'end',
    ),
    (
        'closing',
        RESTAURANT,
        {'question': 'Anything else?', 'answer': 'Yes, do you have vegan options?'},
        'continue',
    ),
    (
        'closing',
        HELPDESK,
        {'question': 'Did that fix it?', 'answer': "No, thanks, it still doesn't connect."},
        'continue',
    ),
    (
        'request-kept',
        CLINIC + ' The patient asked for something, then the receptionist stopped to check who they are.',
        {
            'question': "What's your date of birth? So that's the 3rd of March 1985. Is that right?",
            'answer': "It's the 3rd of March 1985. Yes, that's right.",
            'request': "I'd like to book a cleaning on Tuesday.",
        },
        'kept',
    ),
    (
        'request-kept',
        RESTAURANT + ' The guest asked for something, then the host stopped to take their phone number.',
        {
            'question': "What's your phone number? That's 600 123 456. Is that right?",
            'answer': "It's 600 123 456. Oh, and make it five people. Yes, that's right.",
            'request': 'A table for four tonight, please.',
        },
        'changed',
    ),
    (
        'request-kept',
        HELPDESK + ' The employee asked for something, then the agent stopped to check who they are.',
        {
            'question': "What's your employee ID? That's 4471. Correct?",
            'answer': "It's 4471. Yes, correct.",
            'request': "My laptop won't connect to the office wifi.",
        },
        'kept',
    ),
)
"""Items from businesses other than the salon, written for this check and not yet reviewed."""


FOCUSED_NAMED = {'pick-conditional': NINE}
"""The option a focused pick case names without picking it. Every other pick item names none."""

NAMED: tuple[tuple[str, dict[str, Any], str, str], ...] = (
    (
        CHOSEN_OFFER.classifier.context,
        {'question': TIMES, 'answer': 'The first one, if it is under 30 euros.'},
        NINE,
        'dev',
    ),
    (
        CHOSEN_OFFER.classifier.context,
        {'question': TIMES, 'answer': 'Is the 9:30 one with the senior stylist? Then that one.'},
        SLOTS[1].id,
        'dev',
    ),
    (
        CLINIC + ' The patient was offered appointment times.',
        {
            'question': 'I have Tuesday at 4 PM or Wednesday at 9 AM. Which suits you?',
            'answer': 'Tuesday at 4, as long as it is Dr. Park.',
            'options': [
                {'id': 'tue-16', 'said': 'Tuesday at 4 PM'},
                {'id': 'wed-09', 'said': 'Wednesday at 9 AM'},
            ],
        },
        'tue-16',
        'test',
    ),
    (
        RESTAURANT + ' The guest was offered two times.',
        {
            'question': 'I can do 7 or 8 tonight. Which would you like?',
            'answer': "Is the 8 o'clock one on the terrace? If so, that one.",
            'options': [{'id': 't19', 'said': '7 PM'}, {'id': 't20', 'said': '8 PM'}],
        },
        't20',
        'test',
    ),
    (
        COURIER + ' The customer was asked where to leave the parcel.',
        {
            'question': 'Shall we leave it at your door, or with a neighbour?',
            'answer': "With the neighbour, if they're in. Are they?",
            'options': [
                {'id': 'door', 'said': 'at the door'},
                {'id': 'neighbour', 'said': 'with a neighbour'},
            ],
        },
        'neighbour',
        'test',
    ),
)
"""Picks with a condition or a question: no plain pick, and the option they name. Written by Claude, not reviewed.

For the `named` field of the pick reading (`Offer.named`). The salon's two
are `dev`; the other businesses' are `test`, as the rest of `OTHER` is.
"""


def _input(kind: str, value: BaseModel) -> dict[str, Any]:
    data = value.model_dump(mode='json')
    if kind == 'choose':
        data.setdefault('holding', [])
    return data


SPLITS = ('dev', 'validation', 'test')
"""Where an item may be used. `dev` items may be looked at and tuned on; `test` items are measured, never tuned on.

Every salon item is `dev`: confirm's version 3 was written against their
failures, so none of them is held out any more. The other businesses' items
are `test`, a held-out domain: measured, never tuned on. `validation`, for
choosing between models and versions, is empty until new items are written
for it, and new items for `test` must be from a domain or pattern the tuning
never saw.
"""


def items() -> list[tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Every item as (dataset, id, input, expected output, metadata). The metadata's `split` is one of `SPLITS`."""
    rows = []
    for case in FOCUSED:
        kind = BY_READING[case.reading.name]
        label, also = FOCUSED_LABELS[case.id]
        rows.append(
            (
                kind,
                f'focused-{case.id}',
                {'context': case.reading.classifier.context, **_input(kind, case.value)},
                {'label': label, 'also_ok': list(also), **_named(kind, FOCUSED_NAMED.get(case.id, ''))},
                {
                    'source': 'evals/focused.py',
                    'business': 'salon',
                    'reading': case.reading.name,
                    'label_source': 'live failure, label by Claude, not reviewed',
                    'split': 'dev',
                },
            )
        )
    for case in REVIEWED:
        value = reading_input(case)
        if value is None:
            continue
        reading = reading_for(case)
        kind = BY_READING[reading.name]
        label = label_of(case.legacy_meaning, reading.name) or ''
        rows.append(
            (
                kind,
                f'reviewed-{case.id}',
                {'context': reading.classifier.context, **_input(kind, value)},
                {'label': label, 'also_ok': [], **_named(kind, '')},
                {
                    'source': 'evals/cases.py',
                    'business': 'salon',
                    'reading': reading.name,
                    'group': case.group,
                    'label_source': 'reviewed case',
                    'split': 'dev',
                },
            )
        )
    for n, (kind, context, data, label) in enumerate(OTHER):
        rows.append(
            (
                kind,
                f'other-{kind}-{n}',
                {'context': context, **data},
                {'label': label, 'also_ok': [], **_named(kind, '')},
                {
                    'source': 'evals/readings.py',
                    'business': context.split(' with ')[1].split('.')[0],
                    'reading': KINDS[kind].reading.name,
                    'label_source': 'written by Claude, not reviewed',
                    'split': 'test',
                },
            )
        )
    for n, (context, data, named, split) in enumerate(NAMED):
        rows.append(
            (
                'choose',
                f'named-{n}',
                {'context': context, 'options': [o.model_dump() for o in SLOTS], 'holding': [], **data},
                {'label': '', 'also_ok': [], 'named': named},
                {
                    'source': 'evals/readings.py',
                    'business': context.split(' with ')[1].split('.')[0],
                    'reading': CHOSEN_OFFER.name,
                    'label_source': 'written by Claude, not reviewed',
                    'split': split,
                },
            )
        )
    return rows


def _named(kind: str, named: str) -> dict[str, str]:
    """A pick item's expected `named`, for the pick reading with it on. Other readings have none."""
    return {'named': named} if kind == 'choose' else {}


def _names(kind: Kind) -> bool:
    return 'named' in kind.reading.classifier.output_type.model_fields


def instructions(kind: Kind) -> str:
    """The prompt: the instructions `Classifier` builds, with the business's context as the one variable."""
    fields = [
        f'- `{name}`: {field.description}'
        for name, field in kind.input_type.model_fields.items()
        if field.description
    ]
    return '\n\n'.join(('{{context}}', TASK, 'The input:\n' + '\n'.join(fields)))


def build(publish: bool = False) -> None:
    lf = get_client()
    rows = items()
    for name, kind in KINDS.items():
        dataset = f'harness-reading/{name}'
        output = kind.reading.classifier.output_type
        lf.create_dataset(
            name=dataset,
            description=f'The {kind.reading.name} reading: {output.__doc__ or ""}'.strip(),
            metadata={
                'reading': kind.reading.name,
                'field': kind.reading.field,
                'acts_on': sorted(kind.acts_on),
            },
        )
        for _, item_id, data, expected, metadata in (r for r in rows if r[0] == name):
            lf.create_dataset_item(
                dataset_name=dataset,
                id=f'{name}-{item_id}',
                input=data,
                expected_output=expected,
                metadata=metadata,
            )
        label = 'production' if publish else 'candidate'
        text, config = instructions(kind), prompt_config(kind)
        current = None
        for wanted in ('candidate', 'production'):
            try:
                current = lf.get_prompt(dataset, label=wanted, cache_ttl_seconds=0)
                break
            except Exception:
                current = None
        if current is None or current.prompt != text or current.config != config:
            current = lf.create_prompt(
                name=dataset,
                type='text',
                prompt=text,
                labels=[label],
                config=config,
                commit_message='The instructions and classes core.classifiers.Classifier builds today.',
            )
        elif publish and 'production' not in current.labels:
            lf.update_prompt(name=dataset, version=current.version, new_labels=['production'])
        rows_here = sum(1 for r in rows if r[0] == name)
        print(f'{dataset}: {rows_here} items, prompt v{current.version} {current.labels}')
    lf.flush()


def prompt_config(kind: Kind) -> dict[str, Any]:
    """What a prompt version records beside its text: the input and the classes the code sends with it."""
    return {
        'input_type': kind.input_type.__name__,
        'field': kind.reading.field,
        'json_schema': kind.reading.classifier.json_schema(),
    }


def model(name: str):
    return infer_model(MODELS[name])


def right(output: dict[str, Any], expected: dict[str, Any]) -> bool:
    return output['label'] == expected['label'] or output['label'] in expected['also_ok']


def task_for(name: str, kind: Kind, prompt: Any, model_name: str):
    """One item through the harness's own `Classifier`, with this model and this prompt version."""
    settings = GEMINI if model_name.startswith('gemini') else None

    async def task(*, item, **kwargs):
        data = dict(item.input)
        context = data.pop('context')
        output_type = kind.reading.classifier.output_type
        classifier = Classifier(
            model(model_name), kind.input_type, output_type, context=context, model_settings=settings
        )
        classifier.instructions = prompt.compile(context=context)
        usage = RunUsage()
        started = time.perf_counter()
        with get_client().start_as_current_observation(
            name=f'{name} reading', as_type='generation', model=model_name, input=data, prompt=prompt
        ) as generation:
            try:
                result = await classifier.classify(kind.input_type.model_validate(data), usage=usage)
                label = getattr(result, kind.reading.field)
                named = getattr(result, 'named', None)
            except Exception as error:
                label, named = f'ERROR {type(error).__name__}', None
            seconds = time.perf_counter() - started
            answer = {'label': label, **({'named': named} if named is not None else {})}
            generation.update(
                output=answer,
                usage_details={'input': usage.input_tokens, 'output': usage.output_tokens},
            )
        return {**answer, 'seconds': round(seconds, 3), 'input_tokens': usage.input_tokens}

    return task


def scorers(kind: Kind):
    """The item scores: exact match, and a false accept (a label the application acts on, where it should not)."""

    def exact_match(*, output, expected_output, **kwargs):
        return Evaluation(
            name='exact-match', value=int(right(output, expected_output)), comment=f'got {output["label"]!r}'
        )

    def false_accept(*, output, expected_output, **kwargs):
        return Evaluation(
            name='false-accept', value=int(kind.acts(output['label']) and not right(output, expected_output))
        )

    def named_match(*, output, expected_output, **kwargs):
        expected = (expected_output or {}).get('named', '')
        return Evaluation(
            name='named-match',
            value=int(output.get('named', '') == expected),
            comment=f'named {output.get("named", "")!r}, expected {expected!r}',
        )

    return [exact_match, false_accept, *([named_match] if _names(kind) else [])]


def totals(*, item_results, **kwargs):
    """The run scores: accuracy, false accepts, and the mean latency and input tokens of a request."""
    n = max(len(item_results), 1)

    def total(score: str) -> float:
        return sum(e.value for r in item_results for e in r.evaluations if e.name == score)

    named = [e.value for r in item_results for e in r.evaluations if e.name == 'named-match']
    return [
        *([Evaluation(name='named-accuracy', value=sum(named) / len(named))] if named else []),
        Evaluation(name='accuracy', value=total('exact-match') / n),
        Evaluation(name='false-accepts', value=total('false-accept')),
        Evaluation(name='mean-seconds', value=sum(r.output['seconds'] for r in item_results) / n),
        Evaluation(name='mean-input-tokens', value=sum(r.output['input_tokens'] for r in item_results) / n),
    ]


@dataclass(frozen=True)
class Plan:
    """One `run`: which models and readings, which prompt version, how often, on which items."""

    models: tuple[str, ...] = tuple(MODELS)
    only: tuple[str, ...] = ()
    label: str = 'production'
    version: int | None = None
    """Pins the prompt version; the label is then ignored."""
    repeat: int = 1
    split: str = ''
    """Only the items of this split (`SPLITS`); empty runs them all."""


def run(plan: Plan) -> None:
    lf = get_client()
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M')
    for name, kind in KINDS.items():
        if plan.only and name not in plan.only:
            continue
        dataset = lf.get_dataset(f'harness-reading/{name}')
        if plan.version is not None:
            prompt = lf.get_prompt(f'harness-reading/{name}', version=plan.version, cache_ttl_seconds=0)
        else:
            prompt = lf.get_prompt(f'harness-reading/{name}', label=plan.label, cache_ttl_seconds=0)
        if (prompt.config or {}).get('json_schema') != kind.reading.classifier.json_schema():
            raise SystemExit(
                f'{name}: prompt v{prompt.version} records other classes than the code sends. '
                'Build it, or check out the code it was built from.'
            )
        chosen = [i for i in dataset.items if not plan.split or (i.metadata or {}).get('split') == plan.split]
        if not chosen:
            print(f'{name:13} no items in split {plan.split!r}; skipped', flush=True)
            continue
        for model_name, n in ((m, n) for m in plan.models for n in range(1, plan.repeat + 1)):
            result = lf.run_experiment(
                name=f'{name} v{prompt.version} {model_name}',
                run_name=f'{name}-v{prompt.version}-{model_name}-{plan.split or "all"}-{stamp}-r{n}',
                description=f'The {name} reading, prompt version {prompt.version}, on {model_name}, '
                f'{plan.split or "all"} items, repeat {n} of {plan.repeat}.',
                data=chosen,
                task=task_for(name, kind, prompt, model_name),
                evaluators=scorers(kind),
                run_evaluators=[totals],
                max_concurrency=8,
                metadata={
                    'reading': name,
                    'prompt_version': str(prompt.version),
                    'prompt_labels': ','.join(prompt.labels),
                    'model': model_name,
                    'repeat': str(n),
                    'split': plan.split or 'all',
                    'items': str(len(chosen)),
                },
            )
            rows = result.item_results
            scores = {e.name: e.value for e in result.run_evaluations}
            seconds = sorted(r.output['seconds'] for r in rows)
            wrong = [
                (getattr(r.item, 'id', ''), getattr(r.item, 'expected_output', None), r.output['label'])
                for r in rows
                for e in r.evaluations
                if e.name == 'exact-match' and not e.value
            ]
            print(
                f'{name:13} v{prompt.version} {plan.split or "all":4} r{n} {model_name:22} accuracy {scores["accuracy"]:.2f} of {len(rows):<3} '
                f'false accepts {scores["false-accepts"]:.0f}  median {seconds[len(seconds) // 2]:.2f}s  '
                f'{scores["mean-input-tokens"]:.0f} tokens in'
                + (f'  named {scores["named-accuracy"]:.2f}' if 'named-accuracy' in scores else ''),
                flush=True,
            )
            for r in rows:
                for e in r.evaluations:
                    if e.name == 'named-match' and not e.value:
                        print('     named wrong:', getattr(r.item, 'id', ''), e.comment)
            for row in wrong:
                print('     wrong:', row)
    lf.flush()


OPTIONS = ('--label', '--version', '--repeat', '--split')


def arguments(args: list[str]) -> Plan:
    """`run`'s arguments as a plan. Anything unknown stops the run before a request is made."""
    options: dict[str, str] = {}
    names: list[str] = []
    rest = iter(args)
    for arg in rest:
        if arg in OPTIONS:
            options[arg] = next(rest, '')
        else:
            names.append(arg)
    unknown = [n for n in names if n not in MODELS and n not in KINDS]
    values = options.values()
    if (
        unknown
        or any(not v or v in OPTIONS for v in values)
        or not options.get('--repeat', '1').isdigit()
        or not options.get('--version', '1').isdigit()
        or options.get('--split', 'dev') not in SPLITS
    ):
        raise SystemExit(
            f'unknown {unknown or "option value"}. Models: {", ".join(MODELS)}. Readings: {", ".join(KINDS)}. '
            f'Options: {", ".join(OPTIONS)}; splits: {", ".join(SPLITS)}.'
        )
    return Plan(
        models=tuple(n for n in names if n in MODELS) or tuple(MODELS),
        only=tuple(n for n in names if n in KINDS),
        label=options.get('--label', 'production'),
        version=int(options['--version']) if '--version' in options else None,
        repeat=int(options.get('--repeat', '1')),
        split=options.get('--split', ''),
    )


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else ''
    if command == 'build':
        build(publish='--publish' in sys.argv[2:])
    elif command == 'run':
        run(arguments(sys.argv[2:]))
    else:
        raise SystemExit(__doc__)
