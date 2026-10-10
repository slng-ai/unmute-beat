"""Run: uv run python -m tests.test_editor

The config editor on copies of the repository's own salon and Faro, never the originals:

* a class operation's closing line becomes config (`{class: ..., say: ...}`), its comments and layout
  kept, and the copy still loads and compiles;
* a long-form operation's `say:` is set in place; an action step's routes are set and proved;
* a placeholder the receipt does not have, and an unclosed brace, are caught before save, and a
  failed check writes nothing;
* a save writes only what changed, and the new version names the version it was edited from;
* an edit that cannot be proved line by line is refused, and the Full config tab's manifest is checked whole;
* lines by result: each case is checked against its own result's fields and values (a refusal's
  `{reason}` only on a refusal line, a `when` value the field never holds), saved as config, and read back.
"""

import shutil
import tempfile
from pathlib import Path

import yaml

from studio import configs, editor

ROOT = Path(__file__).resolve().parents[2]
SALON = ROOT / 'use-cases' / 'salon' / 'salon'
FARO = ROOT / 'use-cases' / 'faro-language-learning' / 'faro'


def copied(package: Path) -> Path:
    """The package copied into a temporary use-case folder of its own; returns its manifest."""
    folder = Path(tempfile.mkdtemp()) / package.parent.name
    shutil.copytree(package, folder / package.name, ignore=shutil.ignore_patterns('__pycache__'))
    return folder / package.name / 'agent.yaml'


def a_class_closing_line_becomes_config():
    manifest = copied(SALON)
    before = manifest.read_text()
    found = editor.read(manifest, 'sage-and-stone', manifest.parents[1])
    book = next(op for op in found.operations if op.name == 'book_appointment')
    assert book.say == "You're all set for {spoken}. Anything else I can help with?" and book.form == 'class'
    assert {'spoken', 'booking_id', 'service'} <= set(book.fields), book.fields
    assert not found.problems, found.problems

    edit = editor.Edit(
        base=found.version, closing={'book_appointment': editor.Closing(say='Booked: {spoken}.')}
    )
    saved = editor.save(manifest, edit, 'sage-and-stone', manifest.parents[1])
    assert saved.ok and saved.saved and saved.changed == ['agent.yaml'], saved.problems
    after = manifest.read_text()
    assert '  book_appointment: {class: "operations.py:BookAppointment", say: "Booked: {spoken}."}\n' in after
    assert [a for a in after.splitlines() if a.startswith('#')] == [
        b for b in before.splitlines() if b.startswith('#')
    ], 'every comment kept'
    assert (
        saved.config is not None and saved.config.parent == found.version and saved.version != found.version
    )
    reread = editor.read(manifest, 'sage-and-stone', manifest.parents[1])
    assert next(op for op in reread.operations if op.name == 'book_appointment').say == 'Booked: {spoken}.'
    assert reread.version == saved.version == configs.version(configs.files_of(manifest))

    back = editor.Edit(base=reread.version, closing={'book_appointment': editor.Closing(say='')})
    assert editor.save(manifest, back, 'sage-and-stone', manifest.parents[1]).ok
    assert '  book_appointment: operations.py:BookAppointment\n' in manifest.read_text(), (
        'with no override left, the one-line form comes back'
    )


def a_broken_closing_line_is_caught_before_save():
    manifest = copied(SALON)
    before = manifest.read_text()
    for line, said in (
        ("You're set for {when}.", 'names {when}, which a success does not have'),
        ("You're set for {spoken.", 'the line could not be said'),
    ):
        edit = editor.Edit(closing={'book_appointment': editor.Closing(say=line)})
        checked = editor.save(manifest, edit, 'sage-and-stone', manifest.parents[1])
        assert not checked.ok and not checked.saved, checked
        assert any(said in p.what for p in checked.problems), [p.what for p in checked.problems]
        assert manifest.read_text() == before, 'a failed check writes nothing'
    recap_on_a_read = editor.Edit(closing={'lookup_customer': editor.Closing(recap='We know you.')})
    checked = editor.check(manifest, recap_on_a_read)
    assert not checked.ok and any('nothing of it to recap' in p.what for p in checked.problems), (
        checked.problems
    )


def a_long_form_line_and_an_action_step():
    manifest = copied(FARO)
    found = editor.read(manifest, 'faro', manifest.parents[1])
    help_step = next(s for s in found.steps if s.id == 'help')
    assert help_step.kind == 'action' and help_step.operation == 'note_help' and help_step.then == 'coach'
    note = next(op for op in found.operations if op.name == 'note_help')
    assert note.form == 'function' and note.say == '{said}' and 'said' in note.fields

    edit = editor.Edit(
        closing={'note_help': editor.Closing(say='Here is a hint: {said}')},
        actions={'help': editor.ActionRoutes(on_error='menu')},
    )
    checked = editor.check(manifest, edit)
    assert checked.ok, checked.problems
    saved = editor.save(manifest, edit, 'faro', manifest.parents[1])
    data = yaml.safe_load(manifest.read_text())
    assert data['operations']['note_help']['say'] == 'Here is a hint: {said}'
    assert data['steps']['help']['on_error'] == 'menu' and data['steps']['help']['then'] == 'coach'
    assert saved.saved


def a_prompt_and_the_whole_manifest():
    manifest = copied(SALON)
    found = editor.read(manifest, 'sage-and-stone', manifest.parents[1])
    prompt = 'prompts/booking.md'
    assert prompt in found.files
    saved = editor.save(
        manifest,
        editor.Edit(files={prompt: found.files[prompt] + '\nBe brief.\n'}),
        'sage-and-stone',
        manifest.parents[1],
    )
    assert saved.saved and saved.changed == [prompt]
    refused = editor.check(manifest, editor.Edit(files={'../outside.md': 'x'}))
    assert not refused.ok and 'inside the agent' in refused.problems[0].what
    broken = editor.check(
        manifest, editor.Edit(manifest=found.manifest.replace('entry: concierge', 'entry: nowhere'))
    )
    assert not broken.ok and any('nowhere' in p.what for p in broken.problems), broken.problems


def lines_by_result_are_checked_and_saved():
    manifest = copied(SALON)
    found = editor.read(manifest, 'sage-and-stone', manifest.parents[1])
    lookup = next(op for op in found.operations if op.name == 'lookup_customer')
    book = next(op for op in found.operations if op.name == 'book_appointment')
    assert lookup.values['customer_status'] == ['existing', 'created'] and not lookup.refused_fields
    assert 'reason' in book.refused_fields and 'reason' not in book.fields, (book.fields, book.refused_fields)
    case = editor.Case
    for name, say, said in (
        (
            'book_appointment',
            [case(line='Booked, {reason}.')],
            'names {reason}, which a success does not have',
        ),
        (
            'lookup_customer',
            [case(when={'customer_status': 'verified'}, line='Hi.'), case(line='Thanks.')],
            'customer_status is never verified',
        ),
        (
            'book_appointment',
            [case(when={'status': 'booked'}, line='Booked, {reason}.'), case(line='Done.')],
            'names {reason}, which a success does not have',
        ),
        ('lookup_customer', [case(when={'name': 'x'}, line='Hi.')], 'which its receipt does not have'),
        (
            'lookup_customer',
            [case(line='A.'), case(when={'customer_status': 'new'}, line='B.')],
            'put it last',
        ),
        ('lookup_customer', [case(when='refused', line='No.')], 'never refused with a receipt'),
    ):
        checked = editor.check(manifest, editor.Edit(closing={name: editor.Closing(say=say)}))
        assert any(said in p.what for p in checked.problems), (said, [p.what for p in checked.problems])
    say = [
        case(when={'customer_status': 'existing'}, line='Welcome back.'),
        case(when={'customer_status': 'created'}, line="Thanks, you're new here: I've added you."),
    ]
    refusal = [
        case(when='refused', line='That did not go through: {status}.'),
        case(line='Booked, {spoken}.'),
    ]
    edit = editor.Edit(
        base=found.version,
        closing={'lookup_customer': editor.Closing(say=say), 'book_appointment': editor.Closing(say=refusal)},
    )
    saved = editor.save(manifest, edit, 'sage-and-stone', manifest.parents[1])
    assert saved.ok and saved.saved, saved.problems
    data = yaml.safe_load(manifest.read_text())
    assert data['operations']['lookup_customer']['say'][0] == {
        'when': {'customer_status': 'existing'},
        'line': 'Welcome back.',
    }
    reread = editor.read(manifest, 'sage-and-stone', manifest.parents[1])
    assert next(op for op in reread.operations if op.name == 'lookup_customer').say == say
    assert not reread.problems, reread.problems
    block = 'operations:\n  a:\n    class: x.py:A\n    say:\n      - line: one\n    recap: r\n'
    assert yaml.safe_load(editor.set_closing(block, 'a', editor.Closing(say='two')))['operations']['a'] == {
        'class': 'x.py:A',
        'say': 'two',
        'recap': 'r',
    }, 'a block list replaced whole'


def an_edit_that_cannot_be_proved_is_refused():
    text = 'operations:\n  a: {class: "x.py:A",\n      say: "hi"}\n'
    try:
        editor.set_closing(text, 'a', editor.Closing(say='there'))
    except editor.EditRefusedError as error:
        assert 'Full config' in str(error)
    else:
        raise AssertionError('a flow mapping over several lines is edited on the Full config tab')
    assert editor.placeholders('{a} and {b.c} and {d[0]} and {a}') == ['a', 'b', 'd']


if __name__ == '__main__':
    a_class_closing_line_becomes_config()
    a_broken_closing_line_is_caught_before_save()
    a_long_form_line_and_an_action_step()
    a_prompt_and_the_whole_manifest()
    lines_by_result_are_checked_and_saved()
    an_edit_that_cannot_be_proved_is_refused()
    print('editor ok: closing lines as config, action routes, placeholders checked, versions with parents')
