import Foundation
import CoreGraphics
import CoreMedia
import ScreenCaptureKit
import AppKit

// MARK: perm

typealias TCCPreflightFn = @convention(c) (CFString, CFDictionary?) -> Int
typealias TCCRequestFn = @convention(c) (CFString, CFDictionary?, @escaping @convention(block) (Bool) -> Void) -> Void

func tccHandle() -> UnsafeMutableRawPointer? {
    return dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)
}

func audioPermission() -> String {
    guard let h = tccHandle(), let sym = dlsym(h, "TCCAccessPreflight") else { return "unknown" }
    let f = unsafeBitCast(sym, to: TCCPreflightFn.self)
    switch f("kTCCServiceAudioCapture" as CFString, nil) {
    case 0: return "granted"
    case 1: return "denied"
    default: return "unknown"
    }
}

func cmdPerm(_ args: [String]) {
    let o = parseOpts(args, valueOpts: ["label"], flagOpts: ["request"])
    _ = o
    func report() { emit("perm", ["screen": CGPreflightScreenCaptureAccess(), "audio": audioPermission()]) }
    if !o.flags.contains("request") { report(); return }
    let deadline = nowSec() + 60
    if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
    if audioPermission() != "granted", let h = tccHandle(), let sym = dlsym(h, "TCCAccessRequest") {
        let f = unsafeBitCast(sym, to: TCCRequestFn.self)
        let sem = DispatchSemaphore(value: 0)
        f("kTCCServiceAudioCapture" as CFString, nil) { _ in sem.signal() }
        _ = sem.wait(timeout: .now() + max(1, deadline - nowSec()))
    }
    while !CGPreflightScreenCaptureAccess() && nowSec() < deadline { Thread.sleep(forTimeInterval: 0.5) }
    report()
}

// MARK: clock

func cmdClock(_ args: [String]) {
    _ = parseOpts(args, valueOpts: ["label"])
    let a = mach_absolute_time()
    let b = CMClockGetTime(CMClockGetHostTimeClock()).seconds
    let c = mach_continuous_time()
    let u = ProcessInfo.processInfo.systemUptime
    emit("clock", ["mach_abs": ticksToSec(a), "cm_host": b, "mach_cont": ticksToSec(c), "uptime": u])
}

// MARK: windows

func cmdWindows(_ args: [String]) {
    let o = parseOpts(args, valueOpts: ["label", "pid"])
    let pids = Set(o.all("pid").map { Int32(parseInt($0, "pid")) })
    let sem = DispatchSemaphore(value: 0)
    var result: SCShareableContent?
    var error: Error?
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { c, e in
        result = c; error = e; sem.signal()
    }
    sem.wait()
    guard let content = result else {
        emit("error", ["where": "shareable_content", "msg": "\(error.map { "\($0)" } ?? "unknown")", "code": (error as NSError?)?.code ?? 0])
        flushOut(); exit(1)
    }
    var n = 0
    for w in content.windows {
        let pid = w.owningApplication?.processID ?? 0
        if !pids.isEmpty && !pids.contains(pid) { continue }
        emit("window", [
            "window_id": Int(w.windowID), "pid": Int(pid),
            "bundle_id": w.owningApplication?.bundleIdentifier ?? "",
            "app": w.owningApplication?.applicationName ?? "",
            "title": w.title ?? "",
            "frame": [w.frame.origin.x, w.frame.origin.y, w.frame.size.width, w.frame.size.height],
            "on_screen": w.isOnScreen, "layer": w.windowLayer, "active": w.isActive,
        ])
        n += 1
    }
    emit("done", ["count": n])
}

func cmdAudioProcs(_ args: [String]) {
    _ = parseOpts(args, valueOpts: ["label"])
    listAudioProcs()
}

// MARK: shm-selftest

func cmdSelftest(_ args: [String]) {
    let o = parseOpts(args, valueOpts: ["label", "shm", "size", "fps", "duration", "stats-interval"])
    guard let path = o.one("shm") else { usageError("--shm is required") }
    guard let sz = o.one("size") else { usageError("--size is required") }
    let (w, h) = parseSize(sz, "size")
    let fps = parseDouble(o.one("fps") ?? "30", "fps", min: 1)
    let duration = parseDouble(o.one("duration") ?? "0", "duration")
    let si = parseDouble(o.one("stats-interval") ?? "1.0", "stats-interval", min: 0.05)
    let ring: ShmRing
    do { ring = try ShmRing(path: path, pixFmt: 0, width: w, height: h) }
    catch { usageError("shm: \(error.localizedDescription)") }
    gShm = ring
    let st = ShmSelftest(ring: ring, w: w, h: h, fps: fps)
    gSelftest = st
    gStartT = nowSec()
    installSessionWatchers()
    st.start()
    emit("started", ["t": gStartT, "kind": "shm-selftest", "w": w, "h": h, "fps": fps, "shm": path])
    startStatsTimer(interval: si)
    startDurationTimer(duration)
}

// MARK: capture

func cmdCapture(_ args: [String]) {
    let o = parseOpts(args, valueOpts: [
        "label", "window-id", "size", "fps", "crop", "pixfmt", "capture-resolution", "queue-depth",
        "frame-log", "dump-dir", "dump-every", "dump-seqs", "shm", "watchdog",
        "audio-pid", "audio-tree", "audio-wait", "mute", "audio-out", "audio-log",
        "duration", "stats-interval",
    ])
    let duration = parseDouble(o.one("duration") ?? "0", "duration")
    let si = parseDouble(o.one("stats-interval") ?? "1.0", "stats-interval", min: 0.05)

    var vcfg: VideoConfig? = nil
    if let wid = o.one("window-id") {
        var c = VideoConfig()
        guard let w = UInt32(wid) else { usageError("--window-id: invalid '\(wid)'") }
        c.windowID = w
        guard let sz = o.one("size") else { usageError("--size is required with --window-id") }
        (c.width, c.height) = parseSize(sz, "size")
        c.fps = parseDouble(o.one("fps") ?? "30", "fps", min: 1)
        if let cr = o.one("crop") {
            let p = cr.split(separator: ",").compactMap { Double($0) }
            guard p.count == 4, p[2] > 0, p[3] > 0 else { usageError("--crop: expected x,y,w,h") }
            c.crop = CGRect(x: p[0], y: p[1], width: p[2], height: p[3])
        }
        switch o.one("pixfmt") ?? "nv12" {
        case "nv12": c.bgra = false
        case "bgra": c.bgra = true
        default: usageError("--pixfmt: expected nv12 or bgra")
        }
        let res = o.one("capture-resolution") ?? "automatic"
        guard ["automatic", "best", "nominal"].contains(res) else { usageError("--capture-resolution: expected automatic|best|nominal") }
        c.resolution = res
        c.queueDepth = parseInt(o.one("queue-depth") ?? "6", "queue-depth", min: 1)
        c.frameLog = o.one("frame-log")
        c.dumpDir = o.one("dump-dir")
        if let e = o.one("dump-every") { c.dumpEvery = parseInt(e, "dump-every", min: 1) }
        if let s = o.one("dump-seqs") {
            for part in s.split(separator: ",") { c.dumpSeqs.insert(UInt64(parseInt(String(part), "dump-seqs", min: 1))) }
        }
        if (c.dumpEvery > 0 || !c.dumpSeqs.isEmpty) && c.dumpDir == nil { usageError("--dump-every/--dump-seqs need --dump-dir") }
        if c.dumpDir != nil && c.dumpEvery == 0 && c.dumpSeqs.isEmpty { usageError("--dump-dir needs --dump-every or --dump-seqs") }
        c.shmPath = o.one("shm")
        c.watchdog = parseDouble(o.one("watchdog") ?? "2.0", "watchdog")
        vcfg = c
    } else {
        for k in ["size", "crop", "pixfmt", "capture-resolution", "queue-depth", "frame-log", "dump-dir", "dump-every", "dump-seqs", "shm"] where o.one(k) != nil {
            usageError("--\(k) requires --window-id")
        }
    }

    var acfg: AudioConfig? = nil
    if !o.all("audio-pid").isEmpty || !o.all("audio-tree").isEmpty {
        var c = AudioConfig()
        c.pids = o.all("audio-pid").map { Int32(parseInt($0, "audio-pid", min: 1)) }
        c.trees = o.all("audio-tree").map { Int32(parseInt($0, "audio-tree", min: 1)) }
        c.wait = parseDouble(o.one("audio-wait") ?? "10", "audio-wait")
        let m = o.one("mute") ?? "unmuted"
        guard ["unmuted", "muted", "mutedWhenTapped"].contains(m) else { usageError("--mute: expected unmuted|muted|mutedWhenTapped") }
        c.mute = m
        c.outPath = o.one("audio-out")
        c.logPath = o.one("audio-log")
        acfg = c
    } else {
        for k in ["audio-wait", "mute", "audio-out", "audio-log"] where o.one(k) != nil {
            usageError("--\(k) requires --audio-pid or --audio-tree")
        }
    }

    if vcfg == nil && acfg == nil { usageError("capture needs --window-id and/or --audio-pid/--audio-tree") }

    // ここまで API は一切呼んでいない。ファイルを開いてから SCK / Core Audio に触る。
    let video = vcfg.map { VideoCapture(cfg: $0) }
    let audio = acfg.map { AudioCapture(cfg: $0) }
    video?.prepareFiles()
    audio?.prepareFiles()
    gVideo = video
    gAudio = audio
    gStartT = nowSec()
    installSessionWatchers()

    let group = DispatchGroup()
    if let v = video { group.enter(); v.start { group.leave() } }
    if let a = audio { group.enter(); a.start { group.leave() } }
    group.notify(queue: .main) {
        gStartT = nowSec()
        var d: [String: Any] = ["t": gStartT, "kind": "capture"]
        if let v = video { d["video"] = v.startInfo }
        if let a = audio { d["audio"] = a.startInfo }
        emit("started", d)
        gStats.resetInterval()
        startStatsTimer(interval: si)
        startDurationTimer(duration)
    }
}

// MARK: TCC responsibility

// Started from a terminal (or an SSH session), TCC checks the terminal's permission, not ours.
// Re-spawn ourselves with responsibility disclaimed so that MC Spike's own grant applies.
// MCSPIKE_NO_DISCLAIM=1 keeps the parent responsible (S8: helper inside an app bundle).
var gChild: pid_t = 0

func relaunchDisclaimed() -> Never {
    typealias Disclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    guard let sym = dlsym(dlopen(nil, RTLD_NOW), "responsibility_spawnattrs_setdisclaim") else {
        fputs("mc-spike: responsibility_spawnattrs_setdisclaim not found\n", stderr); exit(1)
    }
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    _ = unsafeBitCast(sym, to: Disclaim.self)(&attr, 1)
    setenv("MCSPIKE_DISCLAIMED", "1", 1)
    let path = Bundle.main.executablePath ?? CommandLine.arguments[0]
    var cargs: [UnsafeMutablePointer<CChar>?] = CommandLine.arguments.map { strdup($0) } + [nil]
    let rc = posix_spawn(&gChild, path, nil, &attr, &cargs, environ)
    posix_spawnattr_destroy(&attr)
    if rc != 0 { fputs("mc-spike: disclaimed spawn failed: \(rc)\n", stderr); exit(1) }
    for s in [SIGINT, SIGTERM, SIGHUP] { signal(s) { sig in kill(gChild, sig) } }
    var status: Int32 = 0
    while waitpid(gChild, &status, 0) < 0 && errno == EINTR {}
    if status & 0x7f == 0 { exit((status >> 8) & 0xff) }
    exit(128 + (status & 0x7f))
}

// MARK: entry

let argv = Array(CommandLine.arguments.dropFirst())
let env = ProcessInfo.processInfo.environment
if ["perm", "windows", "capture"].contains(argv.first ?? ""),
   env["MCSPIKE_DISCLAIMED"] == nil, env["MCSPIKE_NO_DISCLAIM"] == nil {
    relaunchDisclaimed()
}
guard let command = argv.first else { prescanLabel(argv); usageError("usage: mc-spike <perm|clock|windows|audio-procs|capture|shm-selftest> [options]") }
let rest = Array(argv.dropFirst())
prescanLabel(rest)

let known: Set<String> = ["perm", "clock", "windows", "audio-procs", "capture", "shm-selftest"]
guard known.contains(command) else { usageError("unknown command: \(command)") }

// SCStream needs an initialized WindowServer connection (CGS_REQUIRE_INIT assertion otherwise).
if command == "capture" || command == "windows" {
    _ = NSApplication.shared
    NSApp.setActivationPolicy(.prohibited)
}

DispatchQueue.global().async {
    switch command {
    case "perm": cmdPerm(rest); flushOut(); exit(0)
    case "clock": cmdClock(rest); flushOut(); exit(0)
    case "windows": cmdWindows(rest); flushOut(); exit(0)
    case "audio-procs": cmdAudioProcs(rest); flushOut(); exit(0)
    case "capture": cmdCapture(rest)
    case "shm-selftest": cmdSelftest(rest)
    default: break
    }
}
dispatchMain()
