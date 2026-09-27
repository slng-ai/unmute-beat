r"""A second caller for one live check: it takes a time the moment the bot reads a move to it back.

    uv run python -m evals.contend --log <serve's bot log> --diary <state>/diary.db \\
        --heard 'Shall I move your haircut' --heard '3:00 PM' --day 2026-10-01 --at 15:00

Session 4d, live case 4: a move refused because another caller took its
destination between the read-back and the yes. Offers live in each worker's
memory, so nothing in the shared diary file says when a move is being read
back. The bot's log does: the TTS line with the read-back's words. When a
line has every `--heard` phrase, this books `--day` at `--at` for
`--phone` (a synthetic caller) through `Diary` on the same SQLite file, as
another worker would, and exits. It changes nothing in the bot.

Test-only tooling for one scripted live call, never part of the runtime.
ponytail: tails the log by polling every 50 ms; the read-back takes seconds
to play, so the booking lands well before the caller can answer it.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from salon.diary import TZ, Diary

CONTENDER = '+34671540199'
"""The second caller: synthetic, never dialled, only a customer in the diary."""


def matches(line: str, heard: list[str]) -> bool:
    return 'Generating TTS' in line and all(words in line for words in heard)


def take(
    diary_path: str, phone: str, day: str, at: str, service: str = 'haircut', now: datetime | None = None
) -> dict:
    """Book this time for another caller, through the diary's own offer and write."""
    now = now or datetime.now(TZ)
    diary = Diary(path=diary_path, clock=now)
    diary.find_or_create_customer(phone)
    diary.find_slots(phone, day, service, now, offered_to='contender', prefer=at)
    slot = f'{day}T{at.replace(":", "")}|{service}'
    return diary.save_booking(phone, 'book', slot, '', False, now, offered_to='contender')


def watch(log: Path, heard: list[str], timeout: float, poll: float = 0.05) -> str:
    """The first new log line with every phrase in it, or '' after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    with log.open(encoding='utf-8', errors='replace') as handle:
        handle.seek(0, 2)
        while time.monotonic() < deadline:
            line = handle.readline()
            if not line:
                time.sleep(poll)
                continue
            if matches(line, heard):
                return line.strip()
    return ''


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--diary', required=True)
    parser.add_argument('--heard', action='append', required=True)
    parser.add_argument('--day', required=True, help='YYYY-MM-DD')
    parser.add_argument('--at', required=True, help='HH:MM')
    parser.add_argument('--phone', default=CONTENDER)
    parser.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args()
    line = watch(args.log, args.heard, args.timeout)
    if not line:
        print(json.dumps({'took': False, 'why': 'no matching read-back before the timeout'}))
        return 1
    result = take(args.diary, args.phone, args.day, args.at)
    print(
        json.dumps({'took': True, 'line': line[-200:], 'at': datetime.now(TZ).isoformat(), 'result': result})
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
