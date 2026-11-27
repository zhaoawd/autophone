#!/usr/bin/env bash
# build_ax_dump.sh — compile and ad-hoc sign the in-process iOS AX dumper dylib.
#
# Run on a macOS host with Xcode installed. Produces an arm64 + arm64e fat dylib:
# arm64 for third-party iOS apps and arm64e for Apple system processes.
#
# Usage: ./scripts/build_ax_dump.sh [output_path]
# Default output: support/ax_dump.dylib (gitignored; read by autophone/device/ax_session.py).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/support/ax-dump/ax_dump.m"
MIN_IOS="16.0"
OUT="${1:-$ROOT/support/ax_dump.dylib}"

if [[ ! -f "$SRC" ]]; then
    echo "build_ax_dump: missing source $SRC" >&2
    exit 2
fi

if ! command -v xcrun >/dev/null 2>&1; then
    echo "build_ax_dump: xcrun is required (install Xcode on macOS)" >&2
    exit 2
fi

SDK="$(xcrun --sdk iphoneos --show-sdk-path)" || {
    echo "build_ax_dump: Xcode iOS SDK (iphoneos) is not installed or selected (check xcode-select -p)" >&2
    exit 2
}
echo "SDK: $SDK"

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp -d /tmp/ax_dump.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
MODULE_CACHE="$TMP/module-cache"
mkdir -p "$MODULE_CACHE"

for ARCH in arm64 arm64e; do
    xcrun --sdk iphoneos clang -arch "$ARCH" -isysroot "$SDK" \
        -mios-version-min="$MIN_IOS" -Wall -fobjc-arc -fmodules \
        -fmodules-cache-path="$MODULE_CACHE" -dynamiclib \
        -framework Foundation -framework UIKit -framework CoreGraphics -framework CoreLocation \
        "$SRC" -o "$TMP/ax_dump-$ARCH.dylib"
done

FAT_TMP="$TMP/ax_dump.dylib"
lipo -create "$TMP/ax_dump-arm64.dylib" "$TMP/ax_dump-arm64e.dylib" -output "$FAT_TMP"

if command -v ldid >/dev/null 2>&1 && (ldid -S "$FAT_TMP" >/dev/null 2>&1; exit $?) 2>/dev/null; then
    SIGNER="ldid"
elif command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - --identifier com.autophone.ax_dump "$FAT_TMP"
    SIGNER="codesign (ad-hoc)"
else
    echo "WARN: no working ldid/codesign found — sign before loading on device" >&2
    SIGNER="unsigned"
fi

chmod 755 "$FAT_TMP"
mv -f "$FAT_TMP" "$OUT"

echo "compiled fat dylib: $OUT ($(lipo -archs "$OUT"), $SIGNER)"
