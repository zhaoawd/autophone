#!/usr/bin/env bash
# Build and ad-hoc sign the read-only iOS SpringBoard region probe used for
# pre-install checks on upstream vphone guests that do not ship `procexp`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/support/springboard-regions/springboard_regions.c"
ENTITLEMENTS="$ROOT/support/springboard-regions/springboard_regions.entitlements"
OUT="${1:-$ROOT/dist/guest/autophone-springboard-regions}"

if [[ ! -f "$SRC" || ! -f "$ENTITLEMENTS" ]]; then
    echo "build_guest_helpers: missing guest probe sources under $ROOT/support/springboard-regions/" >&2
    exit 2
fi

if ! command -v xcrun >/dev/null 2>&1; then
    echo "build_guest_helpers: xcrun is required (install Xcode on macOS)" >&2
    exit 2
fi

if ! xcrun --sdk iphoneos --show-sdk-path >/dev/null 2>&1; then
    echo "build_guest_helpers: Xcode iOS SDK (iphoneos) is not installed or selected (check xcode-select -p)" >&2
    exit 2
fi

mkdir -p "$(dirname "$OUT")"
tmp="${OUT}.tmp.$$"
trap 'rm -f -- "$tmp"' EXIT

xcrun --sdk iphoneos clang -arch arm64 -mios-version-min=16.0 -O2 \
    "$SRC" -o "$tmp"
codesign --force --sign - --identifier "$(basename "$OUT")" --generate-entitlement-der \
    --entitlements "$ENTITLEMENTS" "$tmp"
chmod 755 "$tmp"
mv -f "$tmp" "$OUT"
trap - EXIT

printf 'Built SpringBoard region probe: %s\n' "$OUT"
printf 'Use with: autophone rig up --target <target.json> --springboard-region-probe %s\n' "$OUT"
