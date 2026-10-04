import Foundation
import CoreGraphics
import CoreMedia
import CoreVideo
import ScreenCaptureKit
import AppKit

/// SCStream が WindowServer 接続を要求する（CGS_REQUIRE_INIT）ので、初回に NSApplication を初期化する。
/// メインスレッドで行う必要がある。
private var gAppKitReady = false
func ensureAppKit() {
    let work = {
        if gAppKitReady { return }
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        gAppKitReady = true
    }
    if Thread.isMainThread { work() } else { DispatchQueue.main.sync(execute: work) }
}

protocol VideoSession: AnyObject {
    /// 異常終了（開始失敗、ストリームが死んだ）を知らせる。
    var onEnded: (() -> Void)? { get set }
    func start(id: Any?)
    func update(crop: CGRect, id: Any?)
    func stop()
}

struct VideoParams {
    var windowID: UInt32
    var width: Int, height: Int
    var crop: CGRect
    var fps: Double
    var shm: String
}

final class VideoCapture: NSObject, SCStreamOutput, SCStreamDelegate, VideoSession {
    let p: VideoParams
    var onEnded: (() -> Void)?
    private let lock = NSLock()
    private var stream: SCStream?
    private var curConfig: SCStreamConfiguration?
    private var stopping = false
    private var ended = false
    private var started = false
    private let frameQ = DispatchQueue(label: "mc.video.frames", qos: .userInteractive)
    private var ring: ShmRing?
    private var seq: UInt64 = 0
    private var firstFrameDone = false

    // watchdog
    private let wdLock = NSLock()
    private var lastAlive = nowSec()
    private var stalled = false
    private var wdTimer: DispatchSourceTimer?

    init(params: VideoParams) {
        p = params
        super.init()
    }

    private func failStart(_ whereStr: String, _ msg: String, code: Int, id: Any?) {
        emitError(whereStr, msg, code: code, id: id)
        endAbnormally()
    }

    private func endAbnormally() {
        lock.lock()
        let already = ended || stopping
        ended = true
        lock.unlock()
        if already { return }
        cleanup()
        onEnded?()
    }

    func start(id: Any?) {
        do { ring = try ShmRing(path: p.shm, width: p.width, height: p.height) }
        catch {
            failStart("shm", (error as NSError).localizedDescription, code: (error as NSError).code, id: id)
            return
        }
        ensureAppKit()
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { [self] content, err in
            guard let content = content else {
                let ns = err as NSError?
                failStart("shareable_content", err.map { "\($0)" } ?? "no content", code: ns?.code ?? 0, id: id)
                return
            }
            guard let win = content.windows.first(where: { $0.windowID == p.windowID }) else {
                failStart("window", "window id \(p.windowID) not found", code: 0, id: id)
                return
            }
            let filter = SCContentFilter(desktopIndependentWindow: win)
            let c = SCStreamConfiguration()
            c.width = p.width
            c.height = p.height
            let rate = min(2 * p.fps, 120)
            c.minimumFrameInterval = CMTime(value: 1000, timescale: CMTimeScale((rate * 1000).rounded()))
            c.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
            c.showsCursor = false
            c.queueDepth = 6
            c.scalesToFit = true
            c.captureResolution = .nominal
            c.sourceRect = p.crop
            c.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2
            let s = SCStream(filter: filter, configuration: c, delegate: self)
            do {
                try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: frameQ)
            } catch {
                failStart("add_stream_output", "\(error)", code: (error as NSError).code, id: id)
                return
            }
            lock.lock(); stream = s; curConfig = c; lock.unlock()
            s.startCapture { [self] error in
                if let error = error {
                    failStart("start_capture", "\(error)", code: (error as NSError).code, id: id)
                    return
                }
                wdLock.lock(); lastAlive = nowSec(); wdLock.unlock()
                lock.lock()
                let stop = stopping
                started = true
                lock.unlock()
                if stop { return }
                startWatchdog()
                emit("video_started", ["window_id": Int(p.windowID), "pid": Int(win.owningApplication?.processID ?? 0),
                                       "w": p.width, "h": p.height, "fps": p.fps], id: id)
            }
        }
    }

    func update(crop: CGRect, id: Any?) {
        lock.lock()
        let s = stream
        let newCfg = curConfig?.copy() as? SCStreamConfiguration
        lock.unlock()
        guard started, let s = s, let cfg = newCfg else {
            emitError("update_video", "video is not running", id: id)
            return
        }
        cfg.sourceRect = crop
        s.updateConfiguration(cfg) { [self] error in
            if let error = error {
                emitError("update_video", error, id: id)
                return
            }
            lock.lock(); curConfig = cfg; lock.unlock()
            emit("video_updated", ["crop": [crop.origin.x, crop.origin.y, crop.size.width, crop.size.height]], id: id)
        }
    }

    private func startWatchdog() {
        let t = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "mc.video.watchdog"))
        t.schedule(deadline: .now() + 0.25, repeating: 0.25)
        t.setEventHandler { [weak self] in
            guard let self = self else { return }
            self.wdLock.lock()
            let gap = nowSec() - self.lastAlive
            let fire = gap > 2.0 && !self.stalled
            if fire { self.stalled = true }
            self.wdLock.unlock()
            if fire { emit("stall", ["gap": gap]) }
        }
        wdTimer = t
        t.resume()
    }

    // MARK: SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid else { return }
        let arrival = nowSec()
        guard let arr = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let info = arr.first,
              let raw = info[.status] as? Int, let status = SCFrameStatus(rawValue: raw) else { return }
        gStats.update {
            switch status {
            case .complete: $0.complete += 1
            case .idle: $0.idle += 1
            default: $0.other += 1
            }
        }
        // 静止画面では complete が来ないので、idle も「生きている」印として扱う。
        if status == .complete || status == .idle {
            wdLock.lock()
            let gap = arrival - lastAlive
            lastAlive = arrival
            let resumed = stalled
            stalled = false
            wdLock.unlock()
            if resumed { emit("resume", ["gap": gap]) }
        }
        guard status == .complete, let pb = CMSampleBufferGetImageBuffer(sb), let ring = ring else { return }

        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
        guard CVPixelBufferIsPlanar(pb), CVPixelBufferGetPlaneCount(pb) == 2, w == ring.width, h == ring.height else {
            gStats.update { $0.shmSkipped += 1 }
            return
        }
        seq += 1
        let mySeq = seq
        let pts = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sb))
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
        guard let p0 = CVPixelBufferGetBaseAddressOfPlane(pb, 0), let p1 = CVPixelBufferGetBaseAddressOfPlane(pb, 1) else {
            gStats.update { $0.shmSkipped += 1 }
            return
        }
        ring.write(seq: mySeq, pts: pts, plane0: p0, stride0: CVPixelBufferGetBytesPerRowOfPlane(pb, 0),
                   plane1: p1, stride1: CVPixelBufferGetBytesPerRowOfPlane(pb, 1))

        if !firstFrameDone {
            firstFrameDone = true
            var rect = CGRect.zero
            if let d = info[.contentRect] as? NSDictionary, let r = CGRect(dictionaryRepresentation: d as CFDictionary) { rect = r }
            emit("first_frame", [
                "w": w, "h": h,
                "content_rect": [rect.origin.x, rect.origin.y, rect.size.width, rect.size.height],
                "content_scale": (info[.contentScale] as? Double) ?? Double.nan,
                "scale_factor": (info[.scaleFactor] as? Double) ?? Double.nan,
                "pts": pts,
            ])
        }
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock()
        let skip = stopping || ended
        lock.unlock()
        if skip { return }
        emitError("stream_stopped", error)
        let wasStarted = started
        endAbnormally()
        if wasStarted { emit("video_stopped", [:]) }
    }

    private func cleanup() {
        wdTimer?.cancel(); wdTimer = nil
        lock.lock(); let s = stream; stream = nil; lock.unlock()
        if let s = s {
            let sem = DispatchSemaphore(value: 0)
            s.stopCapture { _ in sem.signal() }
            _ = sem.wait(timeout: .now() + 5)
        }
        frameQ.sync {}
        ring?.close()
        ring = nil
    }

    func stop() {
        lock.lock()
        let already = stopping || ended
        stopping = true
        lock.unlock()
        if already { return }
        cleanup()
    }
}

/// 許可なしで共有メモリへの書き込みを試すための合成 NV12。
final class SyntheticVideo: VideoSession {
    var onEnded: (() -> Void)?
    private let w: Int, h: Int, fps: Double, shm: String
    private var ring: ShmRing?
    private let q = DispatchQueue(label: "mc.selftest.video")
    private var timer: DispatchSourceTimer?
    private var buf: UnsafeMutableRawPointer
    private var seq: UInt64 = 0
    private var stopped = false

    init(width: Int, height: Int, fps: Double, shm: String) {
        w = width; h = height; self.fps = fps; self.shm = shm
        buf = UnsafeMutableRawPointer.allocate(byteCount: w * h * 3 / 2, alignment: 64)
    }
    deinit { buf.deallocate() }

    func start(id: Any?) {
        do { ring = try ShmRing(path: shm, width: w, height: h) }
        catch {
            emitError("shm", (error as NSError).localizedDescription, code: (error as NSError).code, id: id)
            onEnded?()
            return
        }
        emit("video_started", ["window_id": 0, "pid": 0, "w": w, "h": h, "fps": fps], id: id)
        let t = DispatchSource.makeTimerSource(queue: q)
        t.schedule(deadline: .now(), repeating: 1.0 / fps, leeway: .milliseconds(1))
        t.setEventHandler { [weak self] in self?.tick() }
        timer = t
        t.resume()
    }

    func update(crop: CGRect, id: Any?) {
        emit("video_updated", ["crop": [crop.origin.x, crop.origin.y, crop.size.width, crop.size.height]], id: id)
    }

    private func tick() {
        guard !stopped, let ring = ring else { return }
        seq += 1
        // Y[x,y] = (seq*7 + x + y) & 255、UV は 128 / 64（seq の下位バイトは Y 先頭で読める）
        let y = buf.assumingMemoryBound(to: UInt8.self)
        let base = Int(seq &* 7)
        for r in 0..<h {
            let row = y + r * w
            for c in 0..<w { row[c] = UInt8(truncatingIfNeeded: base + r + c) }
        }
        let uv = y + w * h
        for i in 0..<(w * h / 2) { uv[i] = (i & 1) == 0 ? 128 : 64 }
        ring.write(seq: seq, pts: nowSec(), plane0: buf, stride0: w, plane1: buf + w * h, stride1: w)
        gStats.update { $0.complete += 1 }
        if seq == 1 {
            emit("first_frame", ["w": w, "h": h, "content_rect": [0, 0, w, h], "content_scale": 1.0,
                                 "scale_factor": 1.0, "pts": nowSec()])
        }
    }

    func stop() {
        timer?.cancel()
        q.sync { stopped = true }
        ring?.close(); ring = nil
    }
}
