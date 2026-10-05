import Foundation
import Darwin
import CoreMedia

// MARK: host time

let timebaseInfo: mach_timebase_info_data_t = {
    var t = mach_timebase_info_data_t()
    mach_timebase_info(&t)
    return t
}()

func ticksToSec(_ t: UInt64) -> Double {
    return Double(t) * Double(timebaseInfo.numer) / Double(timebaseInfo.denom) / 1e9
}

func nowSec() -> Double { return ticksToSec(mach_absolute_time()) }

// MARK: output

var gLabel: String? = nil
let outQueue = DispatchQueue(label: "mc.stdout")

func sanitize(_ v: Any) -> Any {
    if let d = v as? Double { return d.isFinite ? d : NSNull() }
    if let f = v as? Float { return f.isFinite ? f : NSNull() }
    if let dict = v as? [String: Any] { return dict.mapValues { sanitize($0) } }
    if let arr = v as? [Any] { return arr.map { sanitize($0) } }
    return v
}

func emit(_ ev: String, _ fields: [String: Any] = [:]) {
    var d = fields
    d["ev"] = ev
    if let l = gLabel { d["label"] = l }
    let clean = sanitize(d)
    outQueue.async {
        guard let data = try? JSONSerialization.data(withJSONObject: clean, options: [.sortedKeys]) else {
            fputs("emit: serialization failed for \(ev)\n", stderr)
            return
        }
        var out = data
        out.append(0x0A)
        out.withUnsafeBytes { p in
            _ = fwrite(p.baseAddress, 1, p.count, stdout)
        }
        fflush(stdout)
    }
}

func flushOut() { outQueue.sync {} }

func log(_ s: String) { fputs("[mc-spike] \(s)\n", stderr) }

/// 引数エラーなど、何も始まる前の致命的エラー。
func usageError(_ msg: String) -> Never {
    emit("error", ["where": "args", "msg": msg, "code": 2])
    flushOut()
    exit(1)
}

// MARK: simple locked line file

final class LineFile {
    private var fp: UnsafeMutablePointer<FILE>?
    private let lock = NSLock()
    init?(path: String) {
        guard let f = fopen(path, "w") else { return nil }
        fp = f
    }
    func write(_ s: String) {
        lock.lock(); defer { lock.unlock() }
        if let f = fp { fputs(s, f) }
    }
    func flush() {
        lock.lock(); defer { lock.unlock() }
        if let f = fp { fflush(f) }
    }
    func close() {
        lock.lock(); defer { lock.unlock() }
        if let f = fp { fclose(f); fp = nil }
    }
}

// MARK: argument parsing

struct Opts {
    var values: [String: [String]] = [:]
    var flags: Set<String> = []
    func one(_ k: String) -> String? { return values[k]?.last }
    func all(_ k: String) -> [String] { return values[k] ?? [] }
}

func prescanLabel(_ args: [String]) {
    if let i = args.firstIndex(of: "--label"), i + 1 < args.count { gLabel = args[i + 1] }
}

func parseOpts(_ args: [String], valueOpts: Set<String>, flagOpts: Set<String> = []) -> Opts {
    var o = Opts()
    var i = 0
    while i < args.count {
        let a = args[i]
        guard a.hasPrefix("--") else { usageError("unexpected argument: \(a)") }
        let k = String(a.dropFirst(2))
        if flagOpts.contains(k) {
            o.flags.insert(k); i += 1
        } else if valueOpts.contains(k) {
            guard i + 1 < args.count else { usageError("option --\(k) needs a value") }
            o.values[k, default: []].append(args[i + 1]); i += 2
        } else {
            usageError("unknown option: \(a)")
        }
    }
    return o
}

func parseInt(_ s: String, _ name: String, min: Int = 0) -> Int {
    guard let v = Int(s), v >= min else { usageError("--\(name): invalid integer '\(s)'") }
    return v
}
func parseDouble(_ s: String, _ name: String, min: Double = 0) -> Double {
    guard let v = Double(s), v.isFinite, v >= min else { usageError("--\(name): invalid number '\(s)'") }
    return v
}
func parseSize(_ s: String, _ name: String) -> (Int, Int) {
    let p = s.lowercased().split(separator: "x")
    guard p.count == 2, let w = Int(p[0]), let h = Int(p[1]), w > 0, h > 0, w % 2 == 0, h % 2 == 0 else {
        usageError("--\(name): expected WxH with even positive integers, got '\(s)'")
    }
    return (w, h)
}

// MARK: stats / session

struct Counters {
    var complete = 0, idle = 0, blank = 0, suspended = 0, started = 0, stopped = 0
    var audioCb = 0, audioFrames = 0, audioZeroCb = 0
    var audioPeak = 0.0
    var stalls = 0
}

final class Stats {
    private let lock = NSLock()
    private var interval = Counters()
    private var total = Counters()
    func update(_ f: (inout Counters) -> Void) {
        lock.lock(); defer { lock.unlock() }
        f(&interval); f(&total)
    }
    func takeInterval() -> Counters {
        lock.lock(); defer { lock.unlock() }
        let c = interval; interval = Counters(); return c
    }
    func resetInterval() { lock.lock(); interval = Counters(); lock.unlock() }
    func totals() -> Counters { lock.lock(); defer { lock.unlock() }; return total }
}

let gStats = Stats()
var gShuttingDown = false
var gStatsTimer: DispatchSourceTimer?
var gDurationTimer: DispatchSourceTimer?
var gSignalSources: [DispatchSourceSignal] = []
var gStartT = 0.0
var gVideo: VideoCapture?
var gAudio: AudioCapture?
var gSelftest: ShmSelftest?
var gShm: ShmRing?

func countersDict(_ c: Counters) -> [String: Any] {
    return ["complete": c.complete, "idle": c.idle, "blank": c.blank, "suspended": c.suspended,
            "started": c.started, "stopped": c.stopped, "audio_cb": c.audioCb,
            "audio_frames": c.audioFrames, "audio_zero_cb": c.audioZeroCb, "audio_peak": c.audioPeak]
}

func startStatsTimer(interval: Double) {
    let t = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "mc.stats"))
    t.schedule(deadline: .now() + interval, repeating: interval)
    t.setEventHandler {
        var d = countersDict(gStats.takeInterval())
        d["t"] = nowSec()
        emit("stats", d)
        gVideo?.flushLogs()
        gAudio?.flushLogs()
    }
    gStatsTimer = t
    t.resume()
}

func fatal(_ whereStr: String, _ msg: String, code: Int = 0) {
    emit("error", ["where": whereStr, "msg": msg, "code": code])
    log("fatal: \(whereStr): \(msg) (\(code))")
    shutdown(1)
}

func shutdown(_ code: Int32) {
    DispatchQueue.main.async { doShutdown(code) }
}

private func doShutdown(_ code: Int32) {
    if gShuttingDown { return }
    gShuttingDown = true
    gStatsTimer?.cancel()
    gDurationTimer?.cancel()
    gVideo?.stop()
    gAudio?.stop()
    gSelftest?.stop()
    if code == 0 {
        var t = countersDict(gStats.totals())
        t["stalls"] = gStats.totals().stalls
        t["duration"] = nowSec() - gStartT
        emit("stopped", ["totals": t])
    }
    flushOut()
    exit(code)
}

/// 標準入力 EOF、シグナル、--duration の監視を始める。
func installSessionWatchers() {
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    for sig in [SIGINT, SIGTERM] {
        let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        s.setEventHandler { log("signal \(sig)"); shutdown(0) }
        s.resume()
        gSignalSources.append(s)
    }
    let th = Thread {
        var buf = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = read(0, &buf, buf.count)
            if n > 0 { continue }
            if n < 0 && errno == EINTR { continue }
            break
        }
        log("stdin EOF")
        shutdown(0)
    }
    th.name = "mc.stdin"
    th.start()
}

func startDurationTimer(_ sec: Double) {
    guard sec > 0 else { return }
    let t = DispatchSource.makeTimerSource(queue: .main)
    t.schedule(deadline: .now() + sec)
    t.setEventHandler { shutdown(0) }
    gDurationTimer = t
    t.resume()
}
