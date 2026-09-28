#!/usr/bin/env bash
# Session 3's package proof: build the core wheel, install it outside the checkout with nothing inherited,
# then run the clinic's three manifests and the salon identity slice from another directory.
#
#   docs/research/reusable-core/session-3/package-proof.sh [work-dir]
#
# Run from the workspace root. Session 2's proof (../session-2/package-proof.sh) is the starting point;
# this one adds the manifests, resources read through importlib.resources, and the salon slice.
set -euo pipefail

root=$(pwd)
here="$root/docs/research/reusable-core/session-3"
work=${1:-$(mktemp -d)}
uv_bin=$(command -v uv)
clean=(env -i "PATH=$(dirname "$uv_bin"):/usr/bin:/bin" "HOME=$HOME")

echo "== build"
uv build --package slng-voice-core --wheel --out-dir "$work/dist" -q
wheel=$(ls "$work"/dist/slng_voice_core-*.whl)
shasum -a 256 "$wheel"

echo "== the application, copied: the clinic, and the salon slice with the salon modules it imports"
mkdir -p "$work/app/salon/models" "$work/app/tests"
cp -R "$root/core/tests/clinic_flow" "$work/app/"
cp -R "$root/use-cases/salon-graph/tests/slice" "$work/app/tests/"
cp "$root/use-cases/salon-graph/salon/__init__.py" "$root/use-cases/salon-graph/salon/diary.py" "$work/app/salon/"
cp "$root/use-cases/salon-graph/salon/models/__init__.py" "$root/use-cases/salon-graph/salon/models/receipts.py" \
    "$root/use-cases/salon-graph/salon/models/shapes.py" "$work/app/salon/models/"
cp "$here/package-scenario.py" "$work/app/scenario.py"

echo "== fresh environment: the wheel, and the phone types the salon slice declares"
cd "$work/app"
"${clean[@]}" uv venv -q --python 3.14 .venv
"${clean[@]}" uv pip install -q --python .venv/bin/python "$wheel" 'pydantic-extra-types[phonenumbers]>=2.11.1'
"${clean[@]}" uv pip freeze --python .venv/bin/python > "$work/deps.txt"
cat "$work/deps.txt"

echo "== run from another directory: nothing is found relative to the working directory"
cd /
"${clean[@]}" "PYTHONPATH=$work/app" "$work/app/.venv/bin/python" "$work/app/scenario.py"
echo "package proof ok, in $work"
