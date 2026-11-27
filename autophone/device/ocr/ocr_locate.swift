// ocr_locate.swift — pure host-side OCR. Image in, JSON text+boxes out.
//
// Part of the semantic-targeting spike (docs/phase1b-semantic-targeting.md):
// run macOS Vision text recognition on a vphone screenshot so automation can
// tap a button by its text ("发送") instead of a calibrated pixel coordinate.
//
// Stays dumb on purpose: no coordinate conversion, no matching, no device
// contact — just recognized text with bounding boxes in IMAGE PIXEL space
// (top-left origin). ocr_target.py does selection + native-coord mapping.
//
// Usage:  ocr_locate <image_path>
// Stdout: JSON [{text, confidence, x, y, w, h}]  (x,y = top-left, pixels)
// Exit:   0 ok · 2 bad args · 3 image load failed · 4 recognition failed

import Foundation
import Vision
import CoreGraphics
import ImageIO

func die(_ msg: String, _ code: Int32) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

guard CommandLine.arguments.count >= 2 else {
    die("usage: ocr_locate <image_path>", 2)
}
let path = CommandLine.arguments[1]

guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
      let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    die("error: cannot load image \(path)", 3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// Lock languages: UI is Simplified Chinese + some English/Latin.
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]

let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([request])
} catch {
    die("error: recognition failed: \(error)", 4)
}

struct Box: Codable {
    let text: String
    let confidence: Float
    let x: Double   // top-left origin, image pixels
    let y: Double
    let w: Double
    let h: Double
}

struct Result: Codable {
    let image_w: Int      // so the consumer can map image px -> native px
    let image_h: Int      // without assuming a fixed screenshot size
    let boxes: [Box]
}

let W = Double(cg.width), H = Double(cg.height)
var boxes: [Box] = []
for obs in (request.results ?? []) {
    guard let top = obs.topCandidates(1).first else { continue }
    // Vision boundingBox is normalized with origin BOTTOM-LEFT; flip Y to the
    // top-left pixel space the screenshot (and the tap socket) use.
    let bb = obs.boundingBox
    boxes.append(Box(
        text: top.string,
        confidence: top.confidence,
        x: bb.minX * W,
        y: (1.0 - bb.maxY) * H,
        w: bb.width * W,
        h: bb.height * H
    ))
}

let enc = JSONEncoder()
enc.outputFormatting = [.prettyPrinted, .sortedKeys]
let result = Result(image_w: cg.width, image_h: cg.height, boxes: boxes)
FileHandle.standardOutput.write(try! enc.encode(result))
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
