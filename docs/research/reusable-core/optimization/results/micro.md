# Micro measurements, as printed

Machine: Apple M5 Pro, 15 cores, 24 GB, macOS 27.0, Python 3.14.5, pydantic-ai-slim 2.46.0. Other users'
processes were running (load average 1.4 to 2.8 through the session).

## hooks_floor.py (from `core/`, at 3bf8d3f's dependencies)

A bare Pydantic AI agent on a scripted model, one streamed reply per run, 400 runs per round, 7 rounds.

```text
0 Hooks:  2078.7 us per run (min 1986.4, max 2131.5), +   0.0 us over none
1 Hooks:  2131.9 us per run (min 2100.0, max 2227.4), +  53.1 us over none
2 Hooks:  2256.5 us per run (min 2207.7, max 2364.0), + 177.7 us over none
5 Hooks:  2640.3 us per run (min 2538.1, max 2906.2), + 561.6 us over none
8 Hooks:  2976.8 us per run (min 2829.1, max 3050.6), + 898.1 us over none
```

## projection.py (from `use-cases/salon-graph/`, at 0d7fc69)

`history.project` and `paired(project(...))` over the long salon call's history, after the turn shown.
Median of 5 rounds of 200 calls.

| turn | messages | spoken segments | project us | paired(project) us |
|---|---|---|---|---|
| 1 | 5 | 1 | 12 | 18 |
| 6 | 25 | 7 | 59 | 92 |
| 12 | 37 | 13 | 92 | 137 |
| 18 | 49 | 19 | 126 | 186 |
| 24 | 61 | 25 | 162 | 232 |
| 30 | 73 | 31 | 197 | 285 |

## context_share.py (each member's folder, at 0d7fc69)

Bytes over every request of one scripted call.

```text
{"workload": "faro", "requests": 8, "instructions": 33047, "context": 17420, "facts": 6572, "empty_facts": 1983, "done": 7718, "done_repeating_facts": 3430}
{"workload": "salon", "requests": 10, "instructions": 62586, "context": 11551, "facts": 2941, "empty_facts": 1251, "done": 1479, "done_repeating_facts": 481}
{"workload": "salon-long", "requests": 34, "instructions": 176888, "context": 44239, "facts": 11173, "empty_facts": 3075, "done": 11487, "done_repeating_facts": 2473}
{"workload": "desk", "requests": 7, "instructions": 8685, "context": 4998, "facts": 984, "empty_facts": 425, "done": 792, "done_repeating_facts": 214}
```

## Profile callers of `dataclasses.replace` (baseline, 5 long salon calls)

From `pstats.print_callers`, the two callers that matter:

```text
24675  0.009  0.214  pydantic_ai/capabilities/combined.py:1101(_replace_capability_context)
22045  0.006  0.063  core/history.py:52(project)
 6425  0.002  0.017  core/history.py:27(paired)
```
