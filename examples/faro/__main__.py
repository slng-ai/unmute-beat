"""Faro's database tools: uv run python -m examples.faro fixture|backup|bench PATH ... (see `store.py`)."""

import asyncio
import sqlite3
import sys

from examples.faro.store import __doc__ as usage
from examples.faro.store import backup, bench, fixture

match sys.argv[1:]:
    case ['fixture', path]:
        print(asyncio.run(fixture(path).counts()))
    case ['backup', source, target]:
        backup(source, target)
        print(f'backed up {source} to {target}')
    case ['bench', path]:
        print(f'SQLite {sqlite3.sqlite_version}, WAL, synchronous=FULL, {sys.platform}')
        print(asyncio.run(bench(path)))
    case _:
        raise SystemExit(usage)
