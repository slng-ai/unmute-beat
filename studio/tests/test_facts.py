"""Run: uv run python -m tests.test_facts

The Facts editor on copies of the repository's own salon and Wasla, never the originals:

* each fact is read from its real type: a phone number, the caller's own words, a date not in the past,
  "a time, or one of: morning, afternoon, evening", a group of details with its own facts inside; a rule
  written in code with what its error says and what the caller hears; who fills it in (the caller, a
  tool, the application when the call opens); and where it is used, as the engine reads the manifest;
* each safe edit is made and proved: a description (one written over several lines included), a new fact
  of each kind the studio writes (with the import it needs added), its choices, its limits, whether it may
  be empty, the steps that fill it in, and removing it again, which gives back the file as it was;
* the proof refuses a wrong edit: text that changes another fact too, a fact written differently from what
  was asked, text that does not read as Python, a change to the steps that fill it in that the engine does
  not read back;
* a fact in use cannot be removed, and says what uses it; a rename is refused; choices defined elsewhere
  are edited in code;
* a saved fact makes a new config version, which includes the state module, and names the one it was
  edited from;
* the studio places a run only on a runner whose code is the code on disk, and asks for new runners when
  every one carries older code.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException

import core
from studio.agents.configs import AgentConfig
from studio.agents.facts_editor import FactsEditor
from studio.agents.models import Fact, FactEdit, FactLimits
from studio.agents.state_source import SourceProof, StateSource
from studio.runs.hub import Hub, RunnerInfo
from studio.runs.store import MemoryStore
from studio.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SALON = ROOT / 'use-cases' / 'salon' / 'salon'
WASLA = ROOT / 'use-cases' / 'wasla' / 'wasla'


def copied(package: Path) -> Path:
    """The package copied into a temporary use-case folder of its own; returns its manifest."""
    folder = Path(tempfile.mkdtemp()) / package.parent.name
    shutil.copytree(package, folder / package.name, ignore=shutil.ignore_patterns('__pycache__'))
    return folder / package.name / 'agent.yaml'


def editor(manifest: Path, agent: str) -> FactsEditor:
    return FactsEditor(manifest, agent, manifest.parents[1])


def facts(found: Any) -> dict[str, Fact]:
    return {fact.name: fact for fact in found.facts}


def uses(fact: Fact, kind: str) -> list[str]:
    return sorted({use.step or use.operation or use.file for use in fact.uses if use.kind == kind})


def ruff_clean(path: Path) -> None:
    """The edited module passes the repository's own lint and format, as a person's would have to."""
    for args in (['check', '--quiet'], ['format', '--check', '--quiet']):
        done = subprocess.run(
            [sys.executable, '-m', 'ruff', *args, str(path)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        assert done.returncode == 0, (args, done.stdout, done.stderr, path.read_text())


def each_fact_is_read_from_its_type():
    salon = facts(editor(copied(SALON), 'sage-and-stone').read())
    assert salon['customer_phone'].kind.words == 'Phone number'
    assert salon['complaint'].kind.words == "The caller's own words"
    assert salon['booking_day'].kind.words == 'A date not in the past'
    assert salon['booking_time'].kind.words == 'A time, or one of: morning, afternoon, evening'
    assert salon['wants_refund'].kind.words == 'Yes/No' and salon['wants_refund'].starts_empty
    customer = salon['customer']
    assert customer.kind.code == 'group' and customer.kind.defined_in == 'salon/receipts.py', customer.kind
    assert [f.name for f in customer.kind.fields] == ['customer_status', 'customer_phone']
    assert customer.kind.fields[0].kind.choices == ['existing', 'created']
    assert customer.group == 'tools' and uses(customer, 'set_by') == ['lookup_customer']
    assert uses(customer, 'needed') == ['booking', 'cancelling', 'complaints', 'moving']
    assert customer.note.startswith('Set only by the lookup'), (
        'a note in code is shown, apart from the description'
    )

    day = salon['booking_day']
    assert day.group == 'caller' and day.label == 'Booking day'
    assert uses(day, 'fills') == ['booking', 'concierge', 'moving', 'verify']
    assert {(u.choice, u.step) for u in day.uses if u.kind == 'rests_on'} == {
        ('slot', 'booking'),
        ('slot', 'moving'),
    }
    phone = salon['customer_phone']
    assert uses(phone, 'identity') == [''] and uses(phone, 'input') == ['lookup_customer']
    assert uses(phone, 'asks') == ['verify'] and uses(phone, 'read_back') == ['verify']
    assert {u.operation for u in salon['customer'].uses if u.kind == 'input'} >= {'book_appointment'}, (
        'an argument taken from inside a fact (AliasPath) uses that fact'
    )
    assert 'Service, defined in salon/receipts.py' in salon['service'].why_not['choices']
    assert phone.kind.description == core.Phone.DESCRIPTION, 'the kind says what the model reads of it'
    assert salon['complaint'].kind.description == '', 'plain text has no description of its own'
    assert day.excerpt is not None and any('booking_day' in line for line in day.excerpt.lines)
    assert day.excerpt.lines[day.line - day.excerpt.first].lstrip().startswith('booking_day'), day.excerpt
    code = [u for u in salon['customer_phone'].uses if u.kind == 'code']
    assert code and all(
        u.excerpt and 'customer_phone' in u.excerpt.lines[u.line - u.excerpt.first] for u in code
    ), code

    wasla = facts(editor(copied(WASLA), 'wasla').read())
    promise = wasla['promise_day']
    assert promise.kind.words == 'A date not in the past'
    assert [r.name for r in promise.rules] == ['_by_deadline'] and promise.rules[0].types == [
        'after_deadline'
    ]
    assert promise.rules[0].says[0].startswith('{day} is after the deadline'), promise.rules[0]
    assert promise.rules[0].heard == [
        'I can only note a day up to {deadline_spoken}. Which day by then works for you?'
    ]
    assert not promise.editable['limits'], 'limits on a fact with a rule in code are set in code'
    assert {wasla[n].group for n in ('account_id', 'holder', 'account')} == {'open'}, (
        'set when the call opens: no step or tool fills them in'
    )
    assert 'prompts/identify.md' in uses(wasla['holder'], 'prompt'), (
        'a prompt naming {{state.facts.holder...}}'
    )
    assert uses(wasla['account'], 'prompt') == ['prompts/account.md'], 'identify never sees the account'
    assert uses(wasla['good_time'], 'carried_out') == ['account']


def safe_edits_are_proved_on_the_salon():
    manifest = copied(SALON)
    state = manifest.parent / 'state.py'
    original = state.read_text()
    edit = editor(manifest, 'sage-and-stone')
    before = edit.read()

    described = edit.save(
        FactEdit(base=before.version, action='change', name='booking_day', description='The day they want.')
    )
    assert described.ok and described.saved and described.changed == ['state.py'], described.problems
    assert len(described.proved) == 3, described.proved
    assert described.config is not None and described.config.parent == before.version
    assert described.version == AgentConfig(manifest).version() != before.version, (
        'a fact change is a new version'
    )
    assert 'state.py' in AgentConfig(manifest).files(), 'the version holds the facts'
    assert facts(edit.read())['booking_day'].description == 'The day they want.'

    added = edit.save(
        FactEdit(
            action='add',
            name='hair_length',
            kind='choice',
            choices=['short', 'medium', 'long'],
            description="How long the caller's hair is.",
            fill_steps=['booking'],
        )
    )
    assert added.ok and added.saved and added.changed == ['agent.yaml', 'state.py'], added.problems
    assert 'exactly the steps asked for fill it in: booking' in added.proved, added.proved
    yaml_text = manifest.read_text()
    assert '    fields: [service, booking_day, booking_time, slot, hair_length]\n' in yaml_text
    assert '    context: {facts: [service, booking_day, booking_time, slot, hair_length]}\n' in yaml_text
    hair = facts(edit.read())['hair_length']
    assert hair.kind.choices == ['short', 'medium', 'long'] and uses(hair, 'fills') == ['booking']
    assert hair.group == 'caller' and hair.editable['choices'] and not hair.editable['remove']

    chosen = edit.save(FactEdit(action='change', name='hair_length', choices=['short', 'long']))
    assert chosen.ok and chosen.saved, chosen.problems
    assert facts(edit.read())['hair_length'].kind.choices == ['short', 'long']

    limited = edit.save(FactEdit(action='change', name='slot', limits=FactLimits(max_length=40)))
    assert limited.ok and limited.saved, limited.problems
    assert facts(edit.read())['slot'].limits == {'max_length': 40}

    fixed = edit.save(FactEdit(action='change', name='hair_length', may_be_empty=False, start='short'))
    assert fixed.ok and fixed.saved, fixed.problems
    held = facts(edit.read())['hair_length']
    assert not held.starts_empty and held.start == 'short'
    assert edit.save(FactEdit(action='change', name='hair_length', may_be_empty=True)).saved
    ruff_clean(state)

    unfilled = edit.save(FactEdit(action='change', name='hair_length', fill_steps=[]))
    assert unfilled.ok and unfilled.saved, unfilled.problems
    hair = facts(edit.read())['hair_length']
    assert hair.editable['remove'], hair.why_not
    removed = edit.save(FactEdit(action='remove', name='hair_length'))
    assert removed.ok and removed.saved, removed.problems
    assert 'hair_length' not in facts(edit.read())
    described_only = StateSource(original, 'SalonState').with_description('booking_day', 'The day they want.')
    back = StateSource(described_only, 'SalonState').with_limits('slot', {'max_length': 40})
    assert state.read_text() == back, 'adding a fact and removing it again leaves the file as it was'


def safe_edits_are_proved_on_wasla():
    manifest = copied(WASLA)
    state = manifest.parent / 'state.py'
    edit = editor(manifest, 'wasla')
    described = edit.save(
        FactEdit(action='change', name='promise_day', description='The day they promise to pay.')
    )
    assert described.ok and described.saved, described.problems
    assert (
        "    promise_day: InWindow | None = Field(None, description='The day they promise to pay.')\n"
        in state.read_text()
    )

    added = edit.save(
        FactEdit(
            action='add',
            name='mood',
            kind='choice',
            choices=['calm', 'upset'],
            description='How they sound.',
            fill_steps=['account'],
        )
    )
    assert added.ok and added.saved, added.problems
    assert 'from typing import Annotated, Literal\n' in state.read_text(), (
        'the import a new kind needs is added'
    )
    assert '    fields: [good_time, promise_day, mood]\n' in manifest.read_text()
    assert '    context: {facts: [holder, account, good_time, promise_day, mood]}\n' in manifest.read_text()

    for kind in ('text', 'whole_number', 'number', 'yes_no', 'date', 'time', 'not_past', 'phone', 'quoted'):
        made = edit.check(FactEdit(action='add', name=f'new_{kind}', kind=kind, description=f'A {kind}.'))
        assert made.ok, (kind, made.problems)
    text = edit.save(
        FactEdit(action='add', name='extra_note', kind='text', description='Anything else they said.')
    )
    assert text.ok and text.saved, text.problems
    assert edit.save(
        FactEdit(action='change', name='extra_note', limits=FactLimits(min_length=2, max_length=200))
    ).saved
    assert facts(edit.read())['extra_note'].limits == {'min_length': 2, 'max_length': 200}
    ruff_clean(state)
    removed = edit.save(FactEdit(action='remove', name='extra_note'))
    assert removed.ok and removed.saved, removed.problems


def a_wrong_edit_is_refused():
    manifest = copied(SALON)
    original = (manifest.parent / 'state.py').read_text()
    source = StateSource(original, 'SalonState')
    both = StateSource(source.with_description('booking_day', 'One.'), 'SalonState').with_description(
        'slot', 'Two.'
    )
    assert SourceProof(original, both, 'SalonState').differences('booking_day') == [
        'a fact other than booking_day changed, moved or appeared'
    ]
    moved = original.replace('WANTED', 'WANTED') + '\nEXTRA = 1\n'
    assert SourceProof(original, moved, 'SalonState').differences('booking_day') == [
        'a statement outside the facts changed'
    ]

    class Sneaky(FactsEditor):
        """Writes something else than what was asked: the proof must catch it."""

        def __init__(self, *args: Any, change: Any) -> None:
            super().__init__(*args)
            self.change = change

        def apply(self, current, edit, before):
            edited = super().apply(current, edit, before)
            return edited | {'state.py': self.change(edited['state.py'])}

    asked = FactEdit(action='change', name='booking_day', description='The day they want.')
    cases = (
        (
            lambda text: StateSource(text, 'SalonState').with_description('slot', 'Something else.'),
            'a fact other than booking_day',
        ),
        (
            lambda text: text.replace('The day they want.', 'The day they need.'),
            'booking_day is not what was asked for',
        ),
        (lambda text: text + '\n    oops(\n', 'does not read as Python'),
        (
            lambda text: text.replace(
                "Literal['morning', 'afternoon', 'evening']", "Literal['morning', 'evening']"
            ),
            'a statement outside the facts changed',
        ),
    )
    for change, said in cases:
        found = Sneaky(manifest, 'sage-and-stone', manifest.parents[1], change=change).check(asked)
        assert not found.ok and any(said in p.what for p in found.problems), (said, found.problems)
    saved = Sneaky(manifest, 'sage-and-stone', manifest.parents[1], change=cases[0][0]).save(asked)
    assert not saved.saved and (manifest.parent / 'state.py').read_text() == original, 'nothing written'

    class Unfilled(FactsEditor):
        """Claims to fill a fact in on a step but writes no such change."""

        def apply(self, current, edit, before):
            edited = super().apply(current, edit, before)
            return edited | {'agent.yaml': current['agent.yaml']}

    found = Unfilled(manifest, 'sage-and-stone', manifest.parents[1]).check(
        FactEdit(action='change', name='wants_refund', fill_steps=['complaints', 'booking'])
    )
    assert not found.ok and any('the steps that fill it in are' in p.what for p in found.problems), (
        found.problems
    )


def what_cannot_change_here_says_why():
    manifest = copied(SALON)
    edit = editor(manifest, 'sage-and-stone')
    original = {path.name: path.read_text() for path in manifest.parent.iterdir() if path.is_file()}
    used = edit.check(FactEdit(action='remove', name='booking_day'))
    assert not used.ok, used
    words = used.problems[0].what
    assert 'filled in by concierge, verify, booking, moving' in words and 'offers.py' in words, words
    renamed = edit.check(FactEdit(action='rename', name='booking_day', new_name='day'))
    assert not renamed.ok and 'renaming is not done here' in renamed.problems[0].what
    shared = edit.check(FactEdit(action='change', name='service', choices=['haircut']))
    assert not shared.ok and 'edit them in code' in shared.problems[0].what, shared.problems
    union = edit.check(FactEdit(action='change', name='booking_time', choices=['morning']))
    assert not union.ok and 'edit it in code' in union.problems[0].what, union.problems
    taken = edit.check(FactEdit(action='add', name='slot', kind='text'))
    assert not taken.ok and 'slot is taken' in taken.problems[0].what
    bad = edit.check(FactEdit(action='add', name='Hair Length', kind='text'))
    assert not bad.ok and 'not a name for a fact' in bad.problems[0].what
    stale = edit.check(FactEdit(base='000000000000', action='change', name='slot', description='x'))
    assert not stale.ok and any('changed on disk' in p.what for p in stale.problems)
    after = {path.name: path.read_text() for path in manifest.parent.iterdir() if path.is_file()}
    assert after == original, 'a check writes nothing'


def runs_go_only_to_runners_with_the_code_on_disk():
    asked: list[str] = []

    def stale() -> str:
        asked.append('renew')
        return 'the studio is starting a runner with the new code'

    hub = Hub(Settings.from_env(), MemoryStore(), lambda: [], stale=stale)
    old = RunnerInfo(
        runner='old',
        url='http://old',
        max_calls=4,
        agents=['salon'],
        code={'salon': 'aaa'},
        ready=True,
        warming=False,
    )
    hub.runners['old'] = old.model_copy(update={'seen': __import__('time').monotonic()})
    try:
        hub.place('salon', 'bbb')
    except HTTPException as error:
        assert error.status_code == 503 and 'started before its code last changed' in error.detail, (
            error.detail
        )
        assert 'starting a runner with the new code' in error.detail and asked == ['renew']
    else:
        raise AssertionError('a run was placed on a runner with old code')
    new = old.model_copy(update={'runner': 'new', 'url': 'http://new', 'code': {'salon': 'bbb'}})
    hub.runners['new'] = new.model_copy(update={'seen': __import__('time').monotonic()})
    assert hub.place('salon', 'bbb').runner == 'new'
    assert hub.place('salon', '').runner in ('old', 'new'), 'no code to match: any ready runner'
    quiet = old.model_copy(update={'runner': 'quiet', 'code': {}})
    hub.runners = {'quiet': quiet.model_copy(update={'seen': __import__('time').monotonic()})}
    assert hub.place('salon', 'bbb').runner == 'quiet', (
        'a runner that says no code is taken to carry the code on disk'
    )


if __name__ == '__main__':
    each_fact_is_read_from_its_type()
    safe_edits_are_proved_on_the_salon()
    safe_edits_are_proved_on_wasla()
    a_wrong_edit_is_refused()
    what_cannot_change_here_says_why()
    runs_go_only_to_runners_with_the_code_on_disk()
    print(
        'facts ok: read from their types, safe edits proved on the salon and Wasla, wrong edits and used facts refused, versions, placement by code'
    )
