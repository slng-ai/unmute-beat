"""Known defects: checks that fail today on purpose, kept out of `python -m tests`.

Run: uv run python -m tests.defects

Each check states the behaviour the harness should have. It prints
`KNOWN DEFECT` and the session that owns the repair while the defect is still
there, and `fixed` once it is gone. The exit code is the number of defects
still present, so session 2 can see its repair land, and then move the check
into `tests/__main__.py`, where a regression fails the suite.

Not in the suite, because a suite that is red by design stops being read, and
not deleted, because a defect with no reproducible check is only a rumour.
"""

import subprocess
import sys

from core.proposals import BaseProposal
from core.state import CallContext, VoiceState


class Desk(VoiceState):
    agent: str = 'desk'


class Call(CallContext):
    state: Desk = Desk()


READBACK = BaseProposal(about='book', value='slot-1', spoken='Tuesday at ten. Shall I book it?')


def unrecorded_readback_authorizes() -> bool:
    """A question whose words were never recorded as said cannot be answered.

    `ProposalLedger.last` counts a question as heard unless it finds a copy
    that was cut off. With no copy at all it finds nothing to object to, so a
    yes to words nobody has evidence were said still reaches `pending()`,
    which is what `Operation._authorise` reads.
    """
    call = Call()
    call.turn_started('I want a haircut')
    call.ask(READBACK)
    call.turn_started('yes')
    return call.pending_question() is not None


def unplayed_readback_authorizes() -> bool:
    """A read-back handed to speech but never started playing is not heard.

    The runtime reports playback on this call (the greeting played), so it
    would say when the read-back started. It never does. The caller then
    speaks without cutting anything off, so this is a new turn, and the
    queued copy carries no mark against it.
    """
    call = Call()
    call.note_said('fixed', 'Hello, Sage and Stone.', origin='entry')
    call.note_reached()
    call.note_drained()
    call.turn_started('I want a haircut')
    call.ask(READBACK)
    call.note_said('fixed', READBACK.spoken, origin='ask')
    call.turn_started('yes')
    return call.pending_question() is not None


def importing_core_loads_the_env_file() -> bool:
    """Importing core reads no `.env`: environment handling belongs to the launcher.

    `core.observability` calls `load_dotenv()` at import, which walks up from
    core's own folder, so any process importing core, the offline suites
    included, picks up the checkout's real Langfuse keys and exports its spans.
    Checked in a fresh process, with `load_dotenv` replaced by a recorder.
    """
    probe = (
        'import dotenv\n'
        'calls = []\n'
        'dotenv.load_dotenv = lambda *a, **k: calls.append(1)\n'
        'import core.observability\n'
        'print(len(calls))\n'
    )
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1] != '0'


DEFECTS = {
    'unknown delivery authorizes: no record of the read-back being said': (unrecorded_readback_authorizes, 2),
    'unknown delivery authorizes: read-back queued, never played': (unplayed_readback_authorizes, 2),
    'importing core loads .env: offline runs get real Langfuse keys': (importing_core_loads_the_env_file, 2),
}
"""Each defect, its check (True while the defect is present) and the session that owns the repair."""


def main() -> int:
    present = 0
    for name, (check, session) in DEFECTS.items():
        if check():
            present += 1
            print(f'KNOWN DEFECT (session {session}): {name}')
        else:
            print(f'fixed: {name}')
    return present


if __name__ == '__main__':
    raise SystemExit(main())
