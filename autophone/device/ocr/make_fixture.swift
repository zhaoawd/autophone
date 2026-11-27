// make_fixture.swift — TEST FIXTURE generator (not production).
//
// Draws known Chinese labels at known spots on a 430×932 canvas (the preview
// screenshot size) so ocr_target.py's `probe` can verify the full pipeline —
// OCR + Y-flip + coordinate mapping — deterministically, with no VM and no
// real app screenshot.
//
// Usage: make_fixture <out.png>
//
// Layout (top-left origin, the space the assertions use):
//   "群聊名称"  near top-center      (conversation title)
//   "相册"      lower-left           (album entry)
//   "发送"      lower-right          (send button)

import Foundation
import AppKit

guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write("usage: make_fixture <out.png>\n".data(using: .utf8)!)
    exit(2)
}
let outPath = CommandLine.arguments[1]

let W = 430, H = 932
// Draw into an EXPLICIT 430×932 bitmap, not NSImage.lockFocus — the latter
// captures the Retina backing (2×) and would emit an 860×1864 PNG, unfaithful
// to the real 1× vphone screenshot. This pins the fixture to true preview size.
guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: W, pixelsHigh: H,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else {
    FileHandle.standardError.write("error: cannot allocate bitmap\n".data(using: .utf8)!)
    exit(3)
}
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSColor.white.setFill()
NSRect(x: 0, y: 0, width: W, height: H).fill()

// AppKit's drawing origin is BOTTOM-LEFT. We pass bottom-left y here; the
// comments give the resulting TOP-LEFT region the OCR/assertions will see.
func draw(_ s: String, blX: CGFloat, blY: CGFloat, size: CGFloat = 22) {
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.boldSystemFont(ofSize: size),
        .foregroundColor: NSColor.black,
    ]
    s.draw(at: NSPoint(x: blX, y: blY), withAttributes: attrs)
}

draw("群聊名称", blX: 150, blY: CGFloat(H - 55))  // → top-center
draw("相册",     blX: 55,  blY: 190)              // → lower-left
draw("发送",     blX: 350, blY: 55)               // → lower-right

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else {
    FileHandle.standardError.write("error: failed to encode PNG\n".data(using: .utf8)!)
    exit(3)
}
try! png.write(to: URL(fileURLWithPath: outPath))
print("wrote \(outPath) (\(W)x\(H))")
