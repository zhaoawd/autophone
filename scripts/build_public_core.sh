#!/usr/bin/env bash
# Build the public installation set in a fresh directory.
set -euo pipefail
cd "$(dirname "$0")/.."
output="${1:-dist/core}"
if [[ -e "$output" ]]; then
    echo "Output already exists: $output" >&2
    exit 2
fi
if [[ -f scripts/export_public_core.py ]]; then
    echo "Build from the exported public source directory." >&2
    exit 2
fi
mkdir -p "$(dirname "$output")"
stage="$(mktemp -d "$(dirname "$output")/.core-build.XXXXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
for package in autophone autophone-kernel autophone-rig autophone-domain autophone-console; do
    uv build --package "$package" --out-dir "$stage"
done
python3 scripts/verify_core_archives.py "$stage"
mkdir -p "$stage/examples/food-ordering/apps/food_ordering/"{cases,discrimination,operations,recipes/drafts,screens,symbols/artifacts}
for file in \
    profile.yaml \
    api_fixtures.yaml \
    cases/cases.yaml \
    discrimination/negative_controls.yaml \
    operations/operations.yaml \
    recipes/r1.yaml \
    recipes/drafts/r2.yaml \
    recipes/drafts/r3.yaml \
    screens/1.0.0-bld-14282f675363.yaml \
    symbols/artifacts/14282f675363.yaml \
    symbols/claims.json; do
    cp "examples/food-ordering/apps/food_ordering/$file" "$stage/examples/food-ordering/apps/food_ordering/$file"
done
mkdir -p "$stage/examples/ios-settings-smoke/apps/ios_settings_smoke/"{cases,recipes,operations}
for file in profile.yaml cases/cases.yaml recipes/r1.yaml recipes/r2.yaml operations/operations.yaml; do
    cp "examples/ios-settings-smoke/apps/ios_settings_smoke/$file" "$stage/examples/ios-settings-smoke/apps/ios_settings_smoke/$file"
done
python3 -c 'import os, sys; os.rename(*sys.argv[1:])' "$stage" "$output"
