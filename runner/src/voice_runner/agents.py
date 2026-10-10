"""Every agent in the repository that a runner can carry: each use case's `core.Entry`, imported by package name.

A use case built on core says what a launcher needs in its package's `entry.py` (`ENTRY`). Each use
case's folder is its own import root (`use-cases/salon` holds the package `salon`), so the runner puts
each folder on the path and imports `<package>.entry`. Only folders with an entry are carried: the
two baselines (one prompt, Pipecat Flows) are not built on core and have none.

An entry that fails to import is reported with its error and left out; the others still load.
"""

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

import core


@dataclass(frozen=True)
class Found:
    """One entry the runner found: its folder and package, and the entry, or why it could not be imported."""

    folder: Path
    package: str
    entry: core.Entry | None
    error: str = ''


class EntryFinder:
    """Every `entry.py` under the repository, imported. A use case whose package name another one already took is refused."""

    def __init__(self, root: Path, pattern: str = 'use-cases/*/*/entry.py') -> None:
        self.root = root
        self.pattern = pattern

    def find(self) -> list[Found]:
        """Each entry found, or why it could not be imported, in path order."""
        taken: set[str] = set()
        found: list[Found] = []
        for path in sorted(self.root.glob(self.pattern)):
            folder, package = path.parent.parent, path.parent.name
            if package in taken:
                found.append(
                    Found(folder, package, None, f'the package name {package!r} is taken by another use case')
                )
                continue
            taken.add(package)
            found.append(self._load(folder, package))
        return found

    @staticmethod
    def _load(folder: Path, package: str) -> Found:
        """Import one use case's `<package>.entry`, its folder first on the path."""
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
        try:
            entry = importlib.import_module(f'{package}.entry').ENTRY
            if not isinstance(entry, core.Entry):
                raise TypeError(f'{package}.entry.ENTRY is a {type(entry).__name__}, not a core.Entry')
        except Exception as error:
            logger.exception('the entry of {} did not load', folder.name)
            return Found(folder, package, None, f'{type(error).__name__}: {error}')
        return Found(folder, package, entry)
