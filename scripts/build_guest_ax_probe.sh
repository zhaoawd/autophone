#!/usr/bin/env bash
# build_guest_ax_probe.sh — compile and ad-hoc sign the standalone out-of-process
# iOS AXRuntime probe (Stage 3).
#
# Run on a macOS host with Xcode installed. Produces an arm64 + arm64e fat Mach-O
# executable linking only Foundation and CoreGraphics (zero UIKit linkage), signed
# with the minimal verified entitlement com.apple.accessibility.api = true.
#
# Usage: ./scripts/build_guest_ax_probe.sh [output_path]
# Default output: support/guest_ax_probe (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/support/guest-ax-probe/ax_runtime_probe.m"
ENTITLEMENTS="$ROOT/support/guest-ax-probe/entitlements.plist"
MIN_IOS="16.0"
OUT="${1:-$ROOT/support/guest_ax_probe}"

if [[ ! -f "$SRC" || ! -f "$ENTITLEMENTS" ]]; then
    echo "build_guest_ax_probe: missing source or entitlements under $ROOT/support/guest-ax-probe/" >&2
    exit 2
fi

if ! command -v xcrun >/dev/null 2>&1; then
    echo "build_guest_ax_probe: xcrun is required (install Xcode on macOS)" >&2
    exit 2
fi

if ! command -v codesign >/dev/null 2>&1; then
    echo "build_guest_ax_probe: codesign is required on macOS" >&2
    exit 2
fi

SDK="$(xcrun --sdk iphoneos --show-sdk-path)" || {
    echo "build_guest_ax_probe: Xcode iOS SDK (iphoneos) is not installed or selected (check xcode-select -p)" >&2
    exit 2
}
echo "SDK: $SDK"

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp -d /tmp/guest_ax_probe.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
MODULE_CACHE="$TMP/module-cache"
mkdir -p "$MODULE_CACHE"

for ARCH in arm64 arm64e; do
    xcrun --sdk iphoneos clang -arch "$ARCH" -isysroot "$SDK" \
        -mios-version-min="$MIN_IOS" -O2 -Wall -fobjc-arc -fmodules \
        -fmodules-cache-path="$MODULE_CACHE" \
        -framework Foundation -framework CoreGraphics \
        "$SRC" -o "$TMP/guest_ax_probe-$ARCH"
done

FAT_TMP="$TMP/guest_ax_probe"
lipo -create "$TMP/guest_ax_probe-arm64" "$TMP/guest_ax_probe-arm64e" -output "$FAT_TMP"

codesign --force --sign - \
    --identifier com.autophone.guest_ax_probe \
    --generate-entitlement-der \
    --entitlements "$ENTITLEMENTS" \
    "$FAT_TMP"

chmod 755 "$FAT_TMP"
mv -f "$FAT_TMP" "$OUT"

BIN_SHA="$(shasum -a 256 "$OUT" | awk '{print $1}')"
SRC_SHA="$(shasum -a 256 "$SRC" | awk '{print $1}')"
ENT_SHA="$(shasum -a 256 "$ENTITLEMENTS" | awk '{print $1}')"
BUILD_RECORD="${OUT}.build.json"
cat > "$BUILD_RECORD" <<EOF
{
  "schema": "autophone.guest-helper-build/v1",
  "protocol": "autophone.ax_probe/1",
  "probe_version": 1,
  "binary_sha256": "$BIN_SHA",
  "source_sha256": "$SRC_SHA",
  "entitlements_sha256": "$ENT_SHA",
  "archs": ["arm64", "arm64e"]
}
EOF

echo "compiled fat executable: $OUT ($(lipo -archs "$OUT"), codesign ad-hoc + DER entitlements, build record: $BUILD_RECORD)"
