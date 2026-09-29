"""How much of each request's application-state block repeats itself: offline, a proposal only, nothing changed.

    cd <member> && uv run python <this folder>/context_share.py WORKLOAD

For every request of `bench.py`'s scripted call, the bytes of the
`core.steps.context` block, of its `Facts:` JSON, of the facts that are empty
(`null`, `""`, `[]`, `{}`), and of the `Done on this call` lines. What leaving
the empty facts out, or printing only a receipt's fields the facts do not
already hold, would save is counted here and not done: either changes what
the model reads, which needs a live check before it ships.
"""

import asyncio
import json
import runpy
import sys
from pathlib import Path

bench = runpy.run_path(str(Path(__file__).with_name('bench.py')), run_name='bench')
EMPTY = (None, '', [], {})


def main() -> None:
    probe = bench['Probe']()
    told: list[str] = []
    wrap = probe.wrap

    def recording(stream):
        inner = wrap(stream)

        async def recorded(messages, info):
            told.append(info.instructions or '')
            async for part in inner(messages, info):
                yield part

        return recorded

    probe.wrap = recording
    workload = bench['WORKLOADS'][sys.argv[1]](probe)
    asyncio.run(workload.run())
    totals = dict.fromkeys(
        ('instructions', 'context', 'facts', 'empty_facts', 'done', 'done_repeating_facts'), 0
    )
    for text in told:
        at = text.find('## Application state')
        block = text[at:] if at >= 0 else ''
        totals['instructions'] += len(text.encode())
        totals['context'] += len(block.encode())
        facts: dict = {}
        for line in block.splitlines():
            if line.startswith('Facts: '):
                facts = json.loads(line.removeprefix('Facts: '))
                totals['facts'] += len(line.encode())
                empty = {k: v for k, v in facts.items() if v in EMPTY}
                totals['empty_facts'] += len(json.dumps(empty, separators=(',', ':')).encode()) - 2
            elif line.startswith('Done on this call: '):
                totals['done'] += len(line.encode())
                receipt = json.loads(line.split(' -> ', 1)[1])
                repeated = {k: v for k, v in receipt.items() if k in facts and facts[k] == v}
                totals['done_repeating_facts'] += len(json.dumps(repeated).encode()) - 2 if repeated else 0
    print(json.dumps({'workload': sys.argv[1], 'requests': len(told)} | totals))


if __name__ == '__main__':
    main()
