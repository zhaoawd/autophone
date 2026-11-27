// Read-only VPHONE geometry and passive pointer observation. Never inject input.
import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

struct ProbeError: Error { let code: String }
func emit(_ value: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data + Data([10]))
}
func fail(_ code: String) -> Never { emit(["type": "error", "code": code]); exit(2) }
let args = Array(CommandLine.arguments.dropFirst())
func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}
guard let config = option("--config") else { fail("config_required") }
let configPath = URL(fileURLWithPath: config).standardizedFileURL.resolvingSymlinksInPath().path
let selectedWindow = option("--window-id").flatMap(UInt32.init)
let parent = getppid()

func processConfig(_ pid: pid_t) -> String? {
    var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
    var length = 0
    guard sysctl(&mib, 3, nil, &length, nil, 0) == 0, length > 4 else { return nil }
    var bytes = [UInt8](repeating: 0, count: length)
    guard sysctl(&mib, 3, &bytes, &length, nil, 0) == 0 else { return nil }
    let count = bytes.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }
    var pos = 4
    while pos < length, bytes[pos] != 0 { pos += 1 }
    while pos < length, bytes[pos] == 0 { pos += 1 }
    var arguments: [String] = []
    for _ in 0..<max(0, count) {
        let start = pos
        while pos < length, bytes[pos] != 0 { pos += 1 }
        arguments.append(String(decoding: bytes[start..<pos], as: UTF8.self)); pos += 1
    }
    guard let i = arguments.firstIndex(of: "--config"), i + 1 < arguments.count else { return nil }
    return URL(fileURLWithPath: arguments[i + 1]).standardizedFileURL.resolvingSymlinksInPath().path
}
func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}
func rect(_ element: AXUIElement) -> CGRect? {
    guard let p = attribute(element, "AXPosition"), let s = attribute(element, "AXSize"),
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero; var size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}
func values(_ r: CGRect) -> [Double] { [r.minX, r.minY, r.width, r.height] }
func same(_ a: CGRect, _ b: CGRect) -> Bool { zip(values(a), values(b)).allSatisfy { abs($0 - $1) < 1 } }
func windows() -> [[String: Any]] {
    CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
}
func bounds(_ w: [String: Any]) -> CGRect? {
    guard let d = w[kCGWindowBounds as String] as? NSDictionary else { return nil }
    return CGRect(dictionaryRepresentation: d)
}
func display(_ r: CGRect) -> [String: Any] {
    let screen = NSScreen.screens.max { a, b in
        func area(_ s: NSScreen) -> CGFloat {
            let id = (s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as! NSNumber).uint32Value
            let overlap = CGDisplayBounds(id).intersection(r)
            return overlap.isNull ? 0 : overlap.width * overlap.height
        }
        return area(a) < area(b)
    }
    guard let screen else { return [:] }
    let id = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as! NSNumber).uint32Value
    let mode = CGDisplayCopyDisplayMode(id)
    return ["id": id, "backing_scale": screen.backingScaleFactor,
            "bounds": values(CGDisplayBounds(id)), "mode_id": mode?.ioDisplayModeID ?? 0,
            "mode_size": [mode?.width ?? 0, mode?.height ?? 0],
            "pixel_size": [mode?.pixelWidth ?? 0, mode?.pixelHeight ?? 0]]
}
func snapshot() throws -> [String: Any] {
    guard AXIsProcessTrusted() else { throw ProbeError(code: "accessibility_permission_required") }
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.vphone.cli")
        .filter { processConfig($0.processIdentifier) == configPath }
    guard apps.count == 1 else { throw ProbeError(code: "vm_process_not_unique") }
    let pid = apps[0].processIdentifier
    let candidates = windows().filter {
        ($0[kCGWindowOwnerPID as String] as? Int32) == pid &&
        String(describing: $0[kCGWindowName as String] ?? "").hasPrefix("VPHONE [connected]") &&
        (selectedWindow == nil || ($0[kCGWindowNumber as String] as? UInt32) == selectedWindow)
    }
    guard candidates.count == 1, let w = candidates.first, let frame = bounds(w),
          let id = w[kCGWindowNumber as String] as? UInt32 else { throw ProbeError(code: "connected_window_not_unique") }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.2)
    let axWindows = attribute(app, "AXWindows") as? [AXUIElement] ?? []
    let matching = axWindows.filter { rect($0).map { same($0, frame) } ?? false }
    guard matching.count == 1 else { throw ProbeError(code: "window_geometry_unverified") }
    let children = attribute(matching[0], "AXChildren") as? [AXUIElement] ?? []
    let toolbars = children.filter { (attribute($0, "AXRole") as? String) == "AXToolbar" }
    guard toolbars.count == 1, let toolbar = rect(toolbars[0]),
          abs(toolbar.minX - frame.minX) < 1, abs(toolbar.minY - frame.minY) < 1,
          abs(toolbar.width - frame.width) < 1, toolbar.height > 0 else {
        throw ProbeError(code: "content_boundary_unverified")
    }
    let content = CGRect(x: frame.minX, y: toolbar.maxY, width: frame.width, height: frame.maxY - toolbar.maxY)
    guard content.width >= 86, content.height >= 186,
          abs(content.width / content.height / (430.0 / 932.0) - 1) < 0.003 else {
        throw ProbeError(code: "content_aspect_mismatch")
    }
    return ["pid": pid, "window_id": id, "config": configPath, "window": values(frame),
            "content": values(content), "content_source": "ax_toolbar_boundary",
            "display": display(content), "uptime": ProcessInfo.processInfo.systemUptime]
}
func exposed(_ point: CGPoint, _ geometry: [String: Any]) -> Bool {
    // AX hit testing ignores click-through cursor/annotation windows. Bounding
    // rectangles alone cannot tell whether an overlay accepts mouse input.
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 0.2)
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(system, Float(point.x), Float(point.y), &hit) == .success,
          let hit else { return false }
    var pid: pid_t = 0
    guard AXUIElementGetPid(hit, &pid) == .success, pid == geometry["pid"] as? Int32 else { return false }
    let window = (attribute(hit, "AXRole") as? String) == "AXWindow" ? hit : attribute(hit, "AXWindow") as! AXUIElement?
    guard let window, let frame = rect(window), let expected = geometry["window"] as? [Double] else { return false }
    return zip(values(frame), expected).allSatisfy { abs($0 - $1) < 1 }
}
var firstSnapshot: [String: Any] = [:]
do { firstSnapshot = try snapshot() }
catch let e as ProbeError { fail(e.code) } catch { fail("probe_failed") }
if !args.contains("--listen") {
    emit(["type": "ready", "snapshot": firstSnapshot, "listen_access": CGPreflightListenEventAccess()]); exit(0)
}
guard CGPreflightListenEventAccess() else { fail("listen_permission_required") }
var stopRequested: Int32 = 0
signal(SIGUSR1) { _ in stopRequested = 1 }
var down: [String: Any]?
var initial: [String: Any]?
var points: [[Double]] = []
var maximumDistance = 0.0
var tap: CFMachPort?
let mask = (1 << CGEventType.leftMouseDown.rawValue) | (1 << CGEventType.leftMouseDragged.rawValue) | (1 << CGEventType.leftMouseUp.rawValue)
tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .tailAppendEventTap, options: .listenOnly,
                       eventsOfInterest: CGEventMask(mask), callback: { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput { fail("event_tap_disabled") }
    let p = event.location
    do {
        if type == .leftMouseDown {
            down = nil; initial = nil; points = []; maximumDistance = 0
            let s = try snapshot(); let c = s["content"] as! [Double]
            let r = CGRect(x: c[0], y: c[1], width: c[2], height: c[3])
            guard r.contains(p), exposed(p, s) else { return Unmanaged.passUnretained(event) }
            down = ["point": [Double(p.x), Double(p.y)], "uptime": Double(event.timestamp) / 1e9]
            initial = s; points = [[p.x, p.y]]
        } else if let start = down, let geometry = initial {
            let origin = start["point"] as! [Double]
            maximumDistance = max(maximumDistance, hypot(p.x - origin[0], p.y - origin[1]))
            if points.count < 512 { points.append([p.x, p.y]) } else { fail("gesture_sample_limit") }
            if type == .leftMouseUp {
                defer { down = nil; initial = nil; points = [] }
                let current = try snapshot()
                guard current["pid"] as? Int32 == geometry["pid"] as? Int32,
                      current["window_id"] as? UInt32 == geometry["window_id"] as? UInt32,
                      current["content"] as? [Double] == geometry["content"] as? [Double],
                      exposed(p, current) else { fail("gesture_geometry_changed") }
                let c = current["content"] as! [Double]
                guard CGRect(x:c[0],y:c[1],width:c[2],height:c[3]).contains(p) else { fail("gesture_left_content") }
                emit(["type": "gesture", "down": start, "up": ["point": [Double(p.x),Double(p.y)], "uptime": Double(event.timestamp)/1e9],
                      "snapshot": geometry, "samples": points, "max_distance": maximumDistance,
                      "source": "cgeventtap_listen_only"])
            }
        }
    } catch let e as ProbeError { fail(e.code) } catch { fail("observation_failed") }
    return Unmanaged.passUnretained(event)
}, userInfo: nil)
guard let tap, let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0) else { fail("event_tap_creation_failed") }
CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)
emit(["type": "ready", "snapshot": firstSnapshot, "listen_access": true, "source": "cgeventtap_listen_only"])
var lastGeometryCheck = ProcessInfo.processInfo.systemUptime
let timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
    if getppid() != parent { exit(3) }
    if stopRequested != 0 {
        CGEvent.tapEnable(tap: tap, enable: false)
        if down != nil { emit(["type": "error", "code": "incomplete_gesture"]) }
        emit(["type": "stopped"])
        exit(0)
    }
    let now = ProcessInfo.processInfo.systemUptime
    if now - lastGeometryCheck >= 1 {
        do {
            let current = try snapshot()
            guard current["pid"] as? Int32 == firstSnapshot["pid"] as? Int32,
                  current["window_id"] as? UInt32 == firstSnapshot["window_id"] as? UInt32 else {
                fail("recording_window_changed")
            }
        } catch let error as ProbeError { fail(error.code) } catch { fail("observation_failed") }
        lastGeometryCheck = now
        emit(["type": "heartbeat", "uptime": now])
    }
}
withExtendedLifetime(timer) { CFRunLoopRun() }
