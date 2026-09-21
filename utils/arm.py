"""What a run was, recorded beside what it measured.

A latency or request-count difference between two arms means nothing without
the two configurations it is a difference between, and "the repository at the
time" is not a record anybody can pair a later run against. Both the offline
benchmark and the live bot write this, so a scripted run and a spoken one can
be compared with each other rather than only with themselves.

Unknown stays unknown. The fields this process cannot see are exactly the
ones that block a strict matched-configuration claim, and leaving them out is
how that block goes unnoticed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import version

from salon import diary
from voice.harness import TurnBudget


def _git(*args: str) -> str:
    return subprocess.run(['git', *args], capture_output=True, text=True, check=False).stdout.strip()


def settings() -> dict:
    """This arm, as far as this process can see it."""
    from salon import agents

    return {
        'revision': _git('rev-parse', 'HEAD') or 'unknown',
        'uncommitted_changes': bool(_git('status', '--porcelain')),
        'python': sys.version.split()[0],
        'pydantic_ai': version('pydantic-ai-slim'),
        # The id, not `str(model)`: a pinned `GoogleModel` renders as
        # `GoogleModel()`, which records nothing at all about which model ran
        # -- on the one field the whole comparison is most sensitive to.
        'model': getattr(agents.MODEL, 'model_name', None) or str(agents.MODEL),
        'model_pinned': bool(os.getenv('BENCH_PINNED')),
        # Unpinned unless Application Default Credentials exist: an
        # Express-Mode API key has none, so Google routes where it likes.
        'region': getattr(getattr(agents.MODEL, 'client', None), 'location', None) or 'unpinned',
        'model_settings': str(agents.SETTINGS),
        'budget': str(TurnBudget()),  # what the processor is built with
        'speak_by': agents.SPEAK_BY,
        'timezone': str(diary.TZ),
        'salon_clock': diary.now().isoformat(),
        # Whether this call started from an empty salon or inherited the last
        # one's. Two calls are only comparable when they agree on it, and a
        # continuation scenario is only meaningful when they do not.
        'fixture': os.getenv('SALON_FIXTURE', 'continue'),
        'started': datetime.now(UTC).isoformat(),
        'seed_customers': sorted(diary._customers),
        # The rule that decides how many turns a booking takes, and one of the
        # things two arms must match before their turn counts mean the same.
        # This one books on an unambiguous choice of an offered time; the
        # native arm reads the booking back and waits for a separate yes.
        'confirmation_policy': 'books on an unambiguous selection; no separate readback turn',
        # What this process cannot see, and every one of them is a dimension
        # two arms have to match on before a difference between them means
        # anything. Named, because an unknown left out of a record reads as
        # a match.
        'unknown': ['provider endpoint', 'stt/tts route and voice', 'transport', 'audio settings'],
    }


def outcome(phone: str) -> dict:
    """What the salon holds now. The diary is in memory, so it dies with the process.

    Which is the whole reason this exists: a live call leaves a trace full of
    timings and no way at all to ask whether it booked the right thing, and a
    benchmark that can only score the turns it can see scores the wrong
    thing quickly.
    """
    return {
        'bookings': diary.bookings_for(phone, diary.now().date()),
        'complaints': [c for c in diary.complaints() if c['phone'] == phone],
    }
