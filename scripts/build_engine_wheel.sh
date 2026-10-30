#!/bin/sh
# Build the pinned ReCET engine (engine/recet-commit) as a wheel in engine/wheels/, for the web image (Dockerfile) to install.
#   RECET_READ_TOKEN=... scripts/build_engine_wheel.sh       (a fine-grained token with read-only Contents on ReCET)
#   RECET_SOURCE=/path/to/ReCET scripts/build_engine_wheel.sh (a local checkout, at the pinned commit)
# The token is used here only, never inside the image build.
set -eu
cd "$(dirname "$0")/.."
commit=$(cat engine/recet-commit)
rm -rf engine/wheels && mkdir -p engine/wheels
if [ -n "${RECET_SOURCE:-}" ]; then
  src="$RECET_SOURCE"
  [ "$(git -C "$src" rev-parse HEAD)" = "$commit" ] || { echo "$src is not at the pinned commit $commit" >&2; exit 1; }
else
  src="recet @ git+https://x-access-token:${RECET_READ_TOKEN:?set RECET_READ_TOKEN or RECET_SOURCE}@github.com/JRichardson-PGE/ReCET@$commit"
fi
uvx --from pip pip wheel --quiet --no-deps --wheel-dir engine/wheels "$src"
ls engine/wheels/recet-*.whl
