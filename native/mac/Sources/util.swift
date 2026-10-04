import Foundation
import Darwin

let mcVersion = "0.1"

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

// MARK: output (stdout JSON Lines)

let outQueue = DispatchQueue(label: "mc.stdout")

func sanitize(_ v: Any) -> Any {
    if let d = v as? Double { return d.isFinite ? d : NSNull() }
    if let f = v as? Float { return f.isFinite ? f : NSNull() }
    if let dict = v as? [String: Any] { return dict.mapValues { sanitize($0) } }
    if let arr = v as? [Any] { return arr.map { sanitize($0) } }
    return v
}

/// イベントを 1 行の JSON にして標準出力へ。"t" は指定がなければ現在のホスト時刻。
func emit(_ ev: String, _ fields: [String: Any] = [:], id: Any? = nil) {
    var d = fields
    d["ev"] = ev
    if d["t"] == nil { d["t"] = nowSec() }
    if let i = id { d["id"] = i }
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

func log(_ s: String) { fputs("[mc-capture] \(s)\n", stderr) }

func emitError(_ whereStr: String, _ msg: String, code: Int = 0, fatal: Bool = false, id: Any? = nil) {
    log("error: \(whereStr): \(msg) (\(code))")
    emit("error", ["where": whereStr, "msg": msg, "code": code, "fatal": fatal], id: id)
}

func emitError(_ whereStr: String, _ err: Error, id: Any? = nil) {
    let ns = err as NSError
    emitError(whereStr, "\(ns.localizedDescription) [\(ns.domain)]", code: ns.code, id: id)
}

// MARK: stats

struct Counters {
    var complete = 0, idle = 0, other = 0
    var audioFrames = 0, audioCb = 0, audioZeroCb = 0, audioDropped = 0
    var shmSkipped = 0
    var unchanged = 0
}

final class Stats {
    private let lock = NSLock()
    private var interval = Counters()
    func update(_ f: (inout Counters) -> Void) {
        lock.lock(); defer { lock.unlock() }
        f(&interval)
    }
    func take() -> Counters {
        lock.lock(); defer { lock.unlock() }
        let c = interval; interval = Counters(); return c
    }
}

let gStats = Stats()

// MARK: small lock helper

final class Locked<T> {
    private let lock = NSLock()
    private var v: T
    init(_ v: T) { self.v = v }
    func get() -> T { lock.lock(); defer { lock.unlock() }; return v }
    func set(_ n: T) { lock.lock(); v = n; lock.unlock() }
    func with<R>(_ f: (inout T) -> R) -> R { lock.lock(); defer { lock.unlock() }; return f(&v) }
}
