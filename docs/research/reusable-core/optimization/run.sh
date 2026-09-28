#!/usr/bin/env bash
# Every bench.py measurement for one checkout: ./run.sh TREE OUT [quick]
#
# TREE is a checkout of the repo (the baseline export or this branch), OUT
# the folder for the JSON. The bench script is always this folder's copy, so
# before and after run the same measurement on the same scripted calls.
# Nothing else should run on the machine meanwhile.
set -euo pipefail
tree=$(cd "$1" && pwd)
out=$2
quick=${3:-}
bench=$(cd "$(dirname "$0")" && pwd)/bench.py
mkdir -p "$out"
reps=20 warm=3 calls=20 active=5 compiles=50 cold=10
if [[ -n "$quick" ]]; then reps=2 warm=1 calls=3 active=2 compiles=3 cold=2; fi

run() {
  local member=$1 mode=$2 workload=$3; shift 3
  echo "== $member $mode $workload"
  (cd "$tree/$member" && uv run --frozen python "$bench" "$mode" "$workload" --out "$out/$mode-$workload.json" "$@" \
    > /dev/null 2> "$out/$mode-$workload.err") || { tail -20 "$out/$mode-$workload.err"; exit 1; }
}

for pair in core:desk use-cases/salon-graph:salon use-cases/salon-graph:salon-long use-cases/faro-language-learning:faro; do
  member=${pair%%:*} workload=${pair##*:}
  [[ $workload == salon-long ]] || run "$member" compile "$workload" --reps "$reps"
  run "$member" turns "$workload" --reps "$reps" --warmup "$warm"
  run "$member" cold "$workload" --reps "$cold"
  run "$member" payload "$workload"
  run "$member" memory "$workload" --calls "$calls" --active "$active" --compiles "$compiles"
  run "$member" profile "$workload" --reps "$reps" --warmup "$warm" --top 40
done
run use-cases/salon-graph apps salon
