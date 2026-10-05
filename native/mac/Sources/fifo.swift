import Foundation
import Darwin

/// FIFO への書き込み。IOProc からは enqueue するだけで、実際の write は専用スレッドが行う。
/// キューには上限があり、溢れたら古いレコードから捨てる（数は stats の audio_dropped）。
final class FifoWriter {
    let path: String
    private let maxBytes = 4 << 20     // 48kHz s16 ステレオで約 21 秒分
    private let cond = NSCondition()
    private var queue: [Data] = []
    private var head = 0
    private var queued = 0
    private var stopping = false
    private var drainDeadline = 0.0
    private var broken = false
    private let done = DispatchSemaphore(value: 0)
    private var started = false

    init(path: String) { self.path = path }

    func start() {
        started = true
        let th = Thread { [self] in run(); done.signal() }
        th.name = "mc.fifo.writer"
        th.start()
    }

    func enqueue(_ d: Data) {
        cond.lock()
        if broken || stopping { cond.unlock(); return }
        queue.append(d)
        queued += d.count
        while queued > maxBytes && queue.count - head > 1 {
            queued -= queue[head].count
            head += 1
            gStats.update { $0.audioDropped += 1 }
        }
        if head > 256 { queue.removeFirst(head); head = 0 }
        cond.signal()
        cond.unlock()
    }

    private func next() -> Data? {
        cond.lock(); defer { cond.unlock() }
        while true {
            if head < queue.count {
                let d = queue[head]; head += 1; queued -= d.count
                if head >= queue.count { queue.removeAll(keepingCapacity: true); head = 0 }
                return d
            }
            if stopping { return nil }
            _ = cond.wait(until: Date(timeIntervalSinceNow: 0.2))
        }
    }

    private func isStopping() -> Bool { cond.lock(); defer { cond.unlock() }; return stopping }
    private func pastDrain() -> Bool { cond.lock(); defer { cond.unlock() }; return stopping && nowSec() > drainDeadline }

    private func run() {
        // 読み側が開くまで待つ（O_NONBLOCK の書き込み open は読み側がいないと ENXIO）。
        var fd: Int32 = -1
        while !isStopping() {
            fd = open(path, O_WRONLY | O_NONBLOCK)
            if fd >= 0 { break }
            if errno != ENXIO && errno != EINTR {
                emitError("fifo_open", "open failed: \(String(cString: strerror(errno)))", code: Int(errno))
                markBroken(); return
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        if fd < 0 { return }
        defer { Darwin.close(fd) }
        while let rec = next() {
            if !writeAll(fd, rec) { markBroken(); return }
        }
    }

    private func writeAll(_ fd: Int32, _ d: Data) -> Bool {
        return d.withUnsafeBytes { raw -> Bool in
            var off = 0
            while off < raw.count {
                let n = Darwin.write(fd, raw.baseAddress! + off, raw.count - off)
                if n > 0 { off += n; continue }
                if n < 0 && errno == EINTR { continue }
                if n < 0 && errno == EAGAIN {
                    if pastDrain() { return false }
                    var pfd = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
                    _ = poll(&pfd, 1, 100)
                    continue
                }
                if n < 0 && errno != EPIPE {
                    emitError("fifo_write", "write failed: \(String(cString: strerror(errno)))", code: Int(errno))
                } else {
                    log("fifo reader closed")
                }
                return false
            }
            return true
        }
    }

    private func markBroken() {
        cond.lock(); broken = true; queue.removeAll(); head = 0; queued = 0; cond.unlock()
    }

    /// 残りを最大 1 秒かけて書き出してから閉じる。
    func stop() {
        guard started else { return }
        cond.lock()
        stopping = true
        drainDeadline = nowSec() + 1.0
        cond.broadcast()
        cond.unlock()
        _ = done.wait(timeout: .now() + 3)
    }
}
