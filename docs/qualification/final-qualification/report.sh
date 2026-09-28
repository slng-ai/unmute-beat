#!/bin/bash
# The real load_report on the normal run, with the reviewed evidence and each simulation's CallSid:
#   docs/qualification/final-qualification/report.sh PRIVATE_DIR ENV_FILE
# The --call pairs are the ones coval_sim printed (PRIVATE_DIR/run-normal.out). The export it saves holds speech,
# so it goes to PRIVATE_DIR, never into the repository.
set -uo pipefail
private=$1 env_file=$2
here=$(cd "$(dirname "$0")" && pwd)
calls=()
while read -r _ sim _ _ sid _; do calls+=(--call "$sim=$sid"); done < <(grep '^simulation ' "$private/run-normal.out")
cd "$here/../../../use-cases/concierge-tools" || exit 2
uv run --env-file "$env_file" python load_report.py GKAmHKajJGTn3wSG9ztSZb \
  --out "$here/load-report-normal" --evidence "$here/evidence-normal.json" --call-seconds 300 \
  --save "$private/load-report-normal.export.json" "${calls[@]}"
