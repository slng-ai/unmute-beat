#!/usr/bin/env bash
# Every member's offline suite, one log each: ./suites.sh OUT [member ...]
#
# Prints each module's own result line and the suite's exit code. Run it
# apart from any timing: it loads every core.
set -uo pipefail
root=$(cd "$(dirname "$0")/../../../.." && pwd)
out=$(mkdir -p "$1" && cd "$1" && pwd); shift
members=("$@")
[[ ${#members[@]} -eq 0 ]] && members=(core use-cases/salon-graph use-cases/faro-language-learning use-cases/salon-pipecat-flows)
status=0
for member in "${members[@]}"; do
  log="$out/$(basename "$member").log"
  (cd "$root/$member" && uv run --frozen python -m tests > "$log" 2>&1)
  code=$?
  [[ $code -ne 0 ]] && status=1
  echo "== $member: exit $code"
  grep -E '^[A-Za-z0-9 _-]+ ok[:,]|^AssertionError|Error: ' "$log" | grep -v ' | ' || true
done
exit $status
