#!/usr/bin/env bash
# The package proof: build the core wheel, install it outside the checkout with nothing inherited and no
# provider key, and run the clinic and the support desk from another directory, including the manifest keys
# `restated: confirms` and a choice's `after:`.
#
#   scripts/package-proof.sh [work-dir]
#
# Run from the workspace root. It copies the two public fixtures that are the reuse proof.
set -euo pipefail

root=$(pwd)
here="$root/scripts"
work=${1:-$(mktemp -d)}
uv_bin=$(command -v uv)
clean=(env -i "PATH=$(dirname "$uv_bin"):/usr/bin:/bin" "HOME=$HOME")

echo "== build"
uv build --package slng-voice-core --wheel --out-dir "$work/dist" -q
wheel=$(ls "$work"/dist/slng_voice_core-*.whl)
shasum -a 256 "$wheel"

echo "== the applications, copied: the clinic and the support desk, as packages"
mkdir -p "$work/app"
cp -R "$root/core/tests/clinic_flow" "$root/core/tests/ticket_flow" "$work/app/"
rm -rf "$work/app/clinic_flow/__pycache__" "$work/app/ticket_flow/__pycache__" "$work/app/ticket_flow/worker.py"
cp "$here/package-scenario.py" "$work/app/scenario.py"

echo "== fresh environment: the wheel alone, its dependencies held to the workspace lock"
uv export --locked --package slng-voice-core --no-hashes --no-emit-project --no-dev > "$work/constraints.txt"
cd "$work/app"
"${clean[@]}" uv venv -q --python 3.14 .venv
"${clean[@]}" uv pip install -q --python .venv/bin/python -c "$work/constraints.txt" "$wheel"
"${clean[@]}" uv pip freeze --python .venv/bin/python > "$work/deps.txt"
cat "$work/deps.txt"

echo "== run from another directory, no key: nothing is found relative to the working directory"
cd /
"${clean[@]}" "PYTHONPATH=$work/app" "$work/app/.venv/bin/python" "$work/app/scenario.py"
echo "package proof ok, in $work"
