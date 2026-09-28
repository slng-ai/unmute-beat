"""Inventory every top-level name and method in core's production modules, with where each name appears.

Static only: a count of whole-word occurrences outside the definition's own line,
split by area. A zero is a removal candidate, never proof; every candidate is then
checked by hand for dynamic use (hooks, validators, protocols, callbacks, manifests).
"""

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(sys.argv[1])
SRC = ROOT / 'core/src/core'
FILES = [
    p
    for p in ROOT.rglob('*')
    if p.suffix in {'.py', '.yaml', '.md', '.js', '.toml', '.html'}
    and '.venv' not in p.parts
    and 'node_modules' not in p.parts
    and 'research' not in p.parts
]
TEXT = {p: p.read_text(errors='ignore') for p in FILES}


def area(path: Path, module: Path) -> str:
    rel = path.relative_to(ROOT)
    if path == module:
        return 'self'
    if rel.parts[:3] == ('core', 'src', 'core'):
        return 'core'
    if rel.parts[:2] == ('core', 'tests'):
        return 'core-tests'
    if rel.parts[0] == 'use-cases':
        return rel.parts[1]
    return 'docs'


def names(module: Path):
    tree = ast.parse(module.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.name, 'function', node.lineno
        elif isinstance(node, ast.ClassDef):
            yield node.name, 'class', node.lineno
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield f'{node.name}.{item.name}', 'method', item.lineno
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.TypeAlias):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target if not isinstance(node, ast.TypeAlias) else node.name]
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    yield target.id, 'value', node.lineno


LEGACY = {'classifiers', 'readings', 'conversation', 'interpreters', 'desk', 'decisions'}


def legacy_only():
    """Names in the kept modules whose only users outside their own module are the legacy path or Faro."""
    for module in sorted(SRC.glob('*.py')):
        if module.stem in LEGACY:
            continue
        for qualified, kind, line in names(module):
            short = qualified.split('.')[-1]
            if short.startswith('__') and short.endswith('__'):
                continue
            pattern = re.compile(rf'\b{re.escape(short)}\b')
            users = set()
            for path, text in TEXT.items():
                rel = path.relative_to(ROOT)
                if path == module or rel.parts[0] == 'docs' or path.suffix == '.md':
                    continue
                if pattern.search(text):
                    users.add(str(rel))
            legacy = {
                u
                for u in users
                if (Path(u).stem in LEGACY and u.startswith('core/src'))
                or 'faro' in u
                or u.endswith(('tests/clinic.py', 'test_reuse.py', 'test_classes.py'))
            }
            if users and users == legacy:
                print(f'{module.stem}\t{qualified}\t{kind}\t{line}\t{sorted(users)}')


if sys.argv[2:] == ['legacy']:
    legacy_only()
    raise SystemExit
print('module\tname\tkind\tline\tself\tcore\tcore-tests\tuse-cases\tdocs')
for module in sorted(SRC.glob('*.py')):
    for qualified, kind, line in names(module):
        short = qualified.split('.')[-1]
        if short.startswith('__') and short.endswith('__'):
            continue
        pattern = re.compile(rf'\b{re.escape(short)}\b')
        counts = defaultdict(int)
        for path, text in TEXT.items():
            found = len(pattern.findall(text))
            if path == module:
                found -= 1
            if found > 0:
                counts[area(path, module)] += found
        uses = sum(v for k, v in counts.items() if k not in {'self', 'core', 'core-tests', 'docs'})
        print(
            f'{module.stem}\t{qualified}\t{kind}\t{line}\t{counts["self"]}\t{counts["core"]}\t{counts["core-tests"]}\t{uses}\t{counts["docs"]}'
        )
