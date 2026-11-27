#!/bin/bash
# build_ocr.sh — compile the host-side OCR spike tools (macOS, needs Xcode CLT).
#
# Produces, next to this script:
#   ocr_locate    — production: image -> JSON text+boxes (Vision)
#   make_fixture   — test-only: draws a deterministic Chinese-label fixture image
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

swiftc -O -framework Vision -framework CoreGraphics -framework ImageIO \
    "$HERE/ocr_locate.swift" -o "$HERE/ocr_locate"
echo "built: $HERE/ocr_locate"

swiftc -O -framework AppKit \
    "$HERE/make_fixture.swift" -o "$HERE/make_fixture"
echo "built: $HERE/make_fixture"
