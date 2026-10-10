"""`agent.yaml` edited line by line, so its comments and layout stay as they were, and every edit proved.

An entry under a top-level section (`operations:`, `steps:`) may be written three ways: a one-line
value (`book: operations.py:Book`, which becomes `{class: operations.py:Book, ...}`), a flow mapping
on one line, or a block mapping under it. A key is set, replaced (with any lines that continued its
old value) or removed in place. The edited text is then parsed again and must equal the original
with exactly that change; anything else is refused, and the Full config tab is the way to make it.
"""

import copy
import json
import re
from typing import Any

import yaml


class EditRefusedError(ValueError):
    """An edit the line editor could not make and prove; the Full config tab can."""


class ManifestText:
    """The text of one manifest, and the same text with an entry's keys changed."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.data = yaml.safe_load(text) or {}

    def with_keys(self, section: str, name: str, changes: dict[str, Any], one_line: str = '') -> str:
        """The text with these keys of `section.name` set, or removed where the value is None; proved."""
        if not changes:
            return self.text
        current = self._entry(section, name, one_line)
        wanted = {key: value for key, value in (current | changes).items() if value is not None}
        lines = self.text.splitlines(keepends=True)
        start, end = self._section(lines, section)
        at = self._line_of(lines, start, end, section, name)
        value = lines[at].split(':', 1)[1].strip()
        if value and not value.startswith('#'):
            lines[at] = self._one_line(lines[at], section, name, value, wanted, one_line)
        else:
            self._block(lines, at, end, changes)
        result = ''.join(lines)
        expected = copy.deepcopy(self.data)
        expected[section][name] = wanted[one_line] if one_line and set(wanted) == {one_line} else wanted
        if (yaml.safe_load(result) or {}) != expected:
            raise EditRefusedError(
                f'{section}.{name}: the edit could not be made without changing something else; use Full config'
            )
        return result

    def _entry(self, section: str, name: str, one_line: str) -> dict[str, Any]:
        """The entry as a mapping: a one-line value becomes `{one_line: value}` when that form is allowed."""
        entries = self.data.get(section) or {}
        if name not in entries:
            raise EditRefusedError(f'{section}.{name} is not in the manifest')
        current = entries[name]
        if isinstance(current, str) and one_line:
            return {one_line: current}
        if not isinstance(current, dict):
            raise EditRefusedError(f'{section}.{name} is not a mapping; use Full config')
        return current

    @staticmethod
    def _section(lines: list[str], section: str) -> tuple[int, int]:
        """The first line of a top-level section and the line after its last."""
        start = next(
            (n for n, line in enumerate(lines) if re.match(rf'^{re.escape(section)}:\s*(#.*)?$', line)), None
        )
        if start is None:
            raise EditRefusedError(f'{section}: is not a block this editor can find; use Full config')
        end = next(
            (
                n
                for n in range(start + 1, len(lines))
                if lines[n].strip() and not lines[n].startswith((' ', '#'))
            ),
            len(lines),
        )
        return start, end

    @staticmethod
    def _line_of(lines: list[str], start: int, end: int, section: str, name: str) -> int:
        """The line an entry starts on."""
        found = next(
            (n for n in range(start + 1, end) if re.match(rf'^(\s+){re.escape(name)}:(\s|$)', lines[n])), None
        )
        if found is None:
            raise EditRefusedError(f'{section}.{name}: its line could not be found; use Full config')
        return found

    def _one_line(
        self, line: str, section: str, name: str, value: str, wanted: dict[str, Any], one_line: str
    ) -> str:
        """An entry written on one line, written again with its new keys: back to a plain value when only that is left."""
        if value.startswith('{') and not value.rstrip().endswith('}') and '#' not in value:
            raise EditRefusedError(f'{section}.{name} spans several lines; use Full config')
        indent = self.indent(line)
        if one_line and set(wanted) == {one_line}:
            return f'{indent}{name}: {wanted[one_line]}\n'
        pairs = ', '.join(f'{key}: {self.scalar(item)}' for key, item in wanted.items())
        return f'{indent}{name}: {{{pairs}}}\n'

    def _block(self, lines: list[str], at: int, end: int, changes: dict[str, Any]) -> None:
        """A block mapping's keys set in place: a key's old value goes with every line that continued it."""
        indent = self.indent(lines[at])
        child = next(
            (
                self.indent(lines[n])
                for n in range(at + 1, end)
                if lines[n].strip() and not lines[n].lstrip().startswith('#')
            ),
            indent + '  ',
        )
        block_end = next(
            (n for n in range(at + 1, end) if lines[n].strip() and len(self.indent(lines[n])) <= len(indent)),
            end,
        )
        for key, value in changes.items():
            found = next(
                (
                    n
                    for n in range(at + 1, block_end)
                    if re.match(rf'^{child}{re.escape(key)}:(\s|$)', lines[n])
                ),
                None,
            )
            if found is not None:
                held = next(
                    (
                        n
                        for n in range(found + 1, block_end)
                        if lines[n].strip() and len(self.indent(lines[n])) <= len(child)
                    ),
                    block_end,
                )
                del lines[found + 1 : held]
                block_end -= held - found - 1
            if value is None and found is not None:
                del lines[found]
                block_end -= 1
            elif value is not None and found is not None:
                lines[found] = f'{child}{key}: {self.scalar(value)}\n'
            elif value is not None:
                last = max((n for n in range(at + 1, block_end) if lines[n].strip()), default=at)
                lines.insert(last + 1, f'{child}{key}: {self.scalar(value)}\n')
                block_end += 1

    @staticmethod
    def indent(line: str) -> str:
        """The spaces a line starts with."""
        return line[: len(line) - len(line.lstrip())]

    @staticmethod
    def scalar(value: Any) -> str:
        """A YAML value on one line that reads back as exactly this value: booleans bare, strings double-quoted.

        A list or mapping (closing lines by result) is written as JSON, which YAML reads as a flow collection.
        """
        if isinstance(value, list | dict):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, int | float):
            return str(value)
        return json.dumps(str(value), ensure_ascii=False)
