#!/usr/bin/env bash
# Regenerate requirements.txt from pyproject.toml + uv.lock.
#
# pyproject.toml is where dependencies are declared and argued about; this
# file exists because the packaging scripts and the release workflow still run
# `pip install -r requirements.txt`, and those run on machines (and in CI
# images) where uv may not be present. Exporting the LOCK rather than the
# ranges means a build installs the same versions the gate ran against, which
# is the point: `fit-tool>=0.9.13,<1` resolving to whichever version was
# newest is how a from-source install shipped workout durations 1000x too
# long.
#
# Run after changing dependencies in pyproject.toml, and commit both files.
set -euo pipefail
cd "$(dirname "$0")/.."

uv lock
{
    echo "# GENERATED from pyproject.toml + uv.lock by tools/export-requirements.sh."
    echo "# Do not edit: declare dependencies in pyproject.toml, then re-run that script."
    echo "#"
    uv export --no-hashes --no-dev --no-emit-project | tail -n +3
} > requirements.txt
echo "wrote requirements.txt ($(grep -c '==' requirements.txt) pinned packages)"
