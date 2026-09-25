#!/usr/bin/env bash
# The package proof: build the core wheel, install it outside the checkout with nothing inherited, run it.
#
#   docs/research/reusable-core/session-2/package-proof.sh [work-dir]
#
# Run from the workspace root. Prints the wheel's sha256, the dependency set
# of each install, and each check's result; exits non-zero on the first failure.
set -euo pipefail

root=$(pwd)
here="$root/docs/research/reusable-core/session-2"
work=${1:-$(mktemp -d)}
uv_bin=$(command -v uv)
clean=(env -i "PATH=$(dirname "$uv_bin"):/usr/bin:/bin" "HOME=$HOME")

echo "== build"
uv build --package slng-voice-core --wheel --out-dir "$work/dist" -q
wheel=$(ls "$work"/dist/slng_voice_core-*.whl)
shasum -a 256 "$wheel"

echo "== fresh project, core only"
mkdir -p "$work/app"
cp -R "$root/core/tests/clinic_flow" "$work/app/"
cp "$here/package-scenario.py" "$work/app/scenario.py"
cd "$work/app"
"${clean[@]}" uv venv -q --python 3.14 .venv
"${clean[@]}" uv pip install -q --python .venv/bin/python "$wheel"
"${clean[@]}" uv pip freeze --python .venv/bin/python > "$work/deps-core.txt"
cat "$work/deps-core.txt"
"${clean[@]}" .venv/bin/python scenario.py

echo "== missing extras"
for module in core.processor core.observability; do
    if "${clean[@]}" .venv/bin/python -c "import $module" 2> "$work/missing.txt"; then
        echo "$module imported without its extra"; exit 1
    fi
    tail -1 "$work/missing.txt"
done

echo "== with the voice and tracing extras"
"${clean[@]}" uv pip install -q --python .venv/bin/python "$wheel[voice,tracing]"
"${clean[@]}" uv pip freeze --python .venv/bin/python > "$work/deps-extras.txt"
diff "$work/deps-core.txt" "$work/deps-extras.txt" | grep '^>' | sed 's/^> /added: /' || true
"${clean[@]}" .venv/bin/python -c "import core.processor, core.observability; print('voice and tracing import')"
echo "package proof ok, in $work"
