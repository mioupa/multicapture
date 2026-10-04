import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreVideo
import CoreImage
import ImageIO
import UniformTypeIdentifiers

struct VideoConfig {
    var windowID: UInt32 = 0
    var width = 0, height = 0
    var fps = 30.0
    var crop: CGRect? = nil
    var bgra = false
    var resolution = "automatic"
    var queueDepth = 6
    var frameLog: String? = nil
    var dumpDir: String? = nil
    var dumpEvery = 0
    var dumpSeqs: Set<UInt64> = []
    var shmPath: String? = nil
    var watchdog = 2.0
}

final class VideoCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    let cfg: VideoConfig
    private var stream: SCStream?
    private let frameQ = DispatchQueue(label: "mc.video.frames", qos: .userInteractive)
    private let dumpQ = DispatchQueue(label: "mc.video.dump")
    private var frameLog: LineFile?
    private var ring: ShmRing?
    private var ciContext: CIContext?
    private var seq: UInt64 = 0
    private var firstFrameDone = false
    private var stopping = false
    private(set) var startInfo: [String: Any] = [:]

    // watchdog (lock 保護)
    private let wdLock = NSLock()
    private var lastComplete: Double = nowSec()
    private var stalled = false
    private var wdTimer: DispatchSourceTimer?

    init(cfg: VideoConfig) {
        self.cfg = cfg
        super.init()
    }

    /// API 呼び出しより前に、ファイル類を開く。失敗は usageError 相当。
    func prepareFiles() {
        if let p = cfg.frameLog {
            guard let f = LineFile(path: p) else { usageError("cannot open --frame-log \(p)") }
            f.write("seq,pts,display_time,arrival,w,h,stride0,cx,cy,cw,ch,content_scale,scale_factor,luma\n")
            frameLog = f
        }
        if let d = cfg.dumpDir {
            do { try FileManager.default.createDirectory(atPath: d, withIntermediateDirectories: true) }
            catch { usageError("cannot create --dump-dir \(d): \(error)") }
            ciContext = CIContext()
        }
        if let p = cfg.shmPath {
            do {
                let r = try ShmRing(path: p, pixFmt: cfg.bgra ? 1 : 0, width: cfg.width, height: cfg.height)
                ring = r
            } catch { usageError("shm: \(error.localizedDescription)") }
        }
    }

    func start(done: @escaping () -> Void) {
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { [self] content, err in
            guard let content = content else {
                let ns = err as NSError?
                fatal("shareable_content", err.map { "\($0)" } ?? "no content", code: ns?.code ?? 0)
                return
            }
            guard let win = content.windows.first(where: { $0.windowID == cfg.windowID }) else {
                fatal("window", "window id \(cfg.windowID) not found", code: 0)
                return
            }
            let filter = SCContentFilter(desktopIndependentWindow: win)
            let c = SCStreamConfiguration()
            c.width = cfg.width
            c.height = cfg.height
            c.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(max(1, Int32(cfg.fps.rounded()))))
            if cfg.fps != cfg.fps.rounded() {
                c.minimumFrameInterval = CMTime(seconds: 1.0 / cfg.fps, preferredTimescale: 600)
            }
            c.pixelFormat = cfg.bgra ? kCVPixelFormatType_32BGRA : kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
            c.showsCursor = false
            c.queueDepth = cfg.queueDepth
            c.scalesToFit = true
            if let r = cfg.crop { c.sourceRect = r }
            switch cfg.resolution {
            case "best": c.captureResolution = .best
            case "nominal": c.captureResolution = .nominal
            default: c.captureResolution = .automatic
            }
            c.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2
            let s = SCStream(filter: filter, configuration: c, delegate: self)
            do {
                try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: frameQ)
            } catch {
                fatal("add_stream_output", "\(error)", code: (error as NSError).code)
                return
            }
            stream = s
            startInfo = [
                "window_id": Int(cfg.windowID), "pid": Int(win.owningApplication?.processID ?? 0),
                "app": win.owningApplication?.applicationName ?? "", "title": win.title ?? "",
                "w": cfg.width, "h": cfg.height, "fps": cfg.fps, "pixfmt": cfg.bgra ? "bgra" : "nv12",
                "window_frame": [win.frame.origin.x, win.frame.origin.y, win.frame.size.width, win.frame.size.height],
            ]
            s.startCapture { [self] error in
                if let error = error {
                    fatal("start_capture", "\(error)", code: (error as NSError).code)
                    return
                }
                lastComplete = nowSec()
                startWatchdog()
                done()
            }
        }
    }

    private func startWatchdog() {
        guard cfg.watchdog > 0 else { return }
        let t = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "mc.video.watchdog"))
        t.schedule(deadline: .now() + 0.25, repeating: 0.25)
        t.setEventHandler { [weak self] in
            guard let self = self else { return }
            self.wdLock.lock()
            let gap = nowSec() - self.lastComplete
            let fire = gap > self.cfg.watchdog && !self.stalled
            if fire { self.stalled = true }
            self.wdLock.unlock()
            if fire {
                gStats.update { $0.stalls += 1 }
                emit("stall", ["t": nowSec(), "gap": gap])
            }
        }
        wdTimer = t
        t.resume()
    }

    // MARK: SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid else { return }
        let arrival = nowSec()
        guard let arr = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let info = arr.first else { return }
        guard let raw = info[.status] as? Int, let status = SCFrameStatus(rawValue: raw) else { return }
        gStats.update {
            switch status {
            case .complete: $0.complete += 1
            case .idle: $0.idle += 1
            case .blank: $0.blank += 1
            case .suspended: $0.suspended += 1
            case .started: $0.started += 1
            case .stopped: $0.stopped += 1
            @unknown default: break
            }
        }
        guard status == .complete, let pb = CMSampleBufferGetImageBuffer(sb) else { return }

        // watchdog
        wdLock.lock()
        let gap = arrival - lastComplete
        lastComplete = arrival
        let resumed = stalled
        stalled = false
        wdLock.unlock()
        if resumed { emit("resume", ["t": arrival, "gap": gap]) }

        seq += 1
        let mySeq = seq
        let pts = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sb))
        let displayTime: Double = (info[.displayTime] as? UInt64).map { ticksToSec($0) } ?? Double.nan
        var rect = CGRect.zero
        if let d = info[.contentRect] as? NSDictionary, let r = CGRect(dictionaryRepresentation: d as CFDictionary) { rect = r }
        let contentScale = (info[.contentScale] as? Double) ?? Double.nan
        let scaleFactor = (info[.scaleFactor] as? Double) ?? Double.nan

        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
        let planar = CVPixelBufferIsPlanar(pb)

        CVPixelBufferLockBaseAddress(pb, .readOnly)
        let stride0 = planar ? CVPixelBufferGetBytesPerRowOfPlane(pb, 0) : CVPixelBufferGetBytesPerRow(pb)
        let p0 = planar ? CVPixelBufferGetBaseAddressOfPlane(pb, 0) : CVPixelBufferGetBaseAddress(pb)
        var luma = Double.nan
        if let p0 = p0 {
            luma = computeLuma(p0, w: w, h: h, stride: stride0, bgra: !planar)
            if let ring = ring {
                if w == ring.width && h == ring.height && (planar == !cfg.bgra) {
                    if planar {
                        let p1 = CVPixelBufferGetBaseAddressOfPlane(pb, 1)
                        ring.write(seq: mySeq, pts: pts, plane0: p0, stride0: stride0, plane1: p1,
                                   stride1: CVPixelBufferGetBytesPerRowOfPlane(pb, 1))
                    } else {
                        ring.write(seq: mySeq, pts: pts, plane0: p0, stride0: stride0, plane1: nil, stride1: 0)
                    }
                } else {
                    log("frame \(mySeq): size/format mismatch \(w)x\(h) planar=\(planar); not written to shm")
                }
            }
        }
        CVPixelBufferUnlockBaseAddress(pb, .readOnly)

        if !firstFrameDone {
            firstFrameDone = true
            func att(_ k: CFString) -> String {
                if let v = CVBufferCopyAttachment(pb, k, nil) { return "\(v)" }
                return ""
            }
            emit("first_frame", [
                "w": w, "h": h, "stride0": stride0,
                "content_rect": [rect.origin.x, rect.origin.y, rect.size.width, rect.size.height],
                "content_scale": contentScale, "scale_factor": scaleFactor,
                "matrix": att(kCVImageBufferYCbCrMatrixKey),
                "primaries": att(kCVImageBufferColorPrimariesKey),
                "transfer": att(kCVImageBufferTransferFunctionKey),
                "pts": pts, "arrival": arrival,
            ])
        }

        if let f = frameLog {
            f.write("\(mySeq),\(pts),\(displayTime),\(arrival),\(w),\(h),\(stride0),\(rect.origin.x),\(rect.origin.y),\(rect.size.width),\(rect.size.height),\(contentScale),\(scaleFactor),\(luma)\n")
        }

        if let dir = cfg.dumpDir, let ctx = ciContext {
            let want = (cfg.dumpEvery > 0 && mySeq % UInt64(cfg.dumpEvery) == 0) || cfg.dumpSeqs.contains(mySeq)
            if want {
                dumpQ.async {
                    let ci = CIImage(cvPixelBuffer: pb)
                    guard let cg = ctx.createCGImage(ci, from: ci.extent) else { log("dump \(mySeq): createCGImage failed"); return }
                    let url = URL(fileURLWithPath: dir).appendingPathComponent("\(mySeq).png")
                    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else { return }
                    CGImageDestinationAddImage(dest, cg, nil)
                    if !CGImageDestinationFinalize(dest) { log("dump \(mySeq): finalize failed") }
                }
            }
        }
    }

    private func computeLuma(_ p: UnsafeMutableRawPointer, w: Int, h: Int, stride: Int, bgra: Bool) -> Double {
        var sum = 0.0
        var n = 0
        let bytes = p.assumingMemoryBound(to: UInt8.self)
        var y = 0
        while y < h {
            let row = bytes + y * stride
            var x = 0
            while x < w {
                if bgra {
                    let b = Double(row[x * 4]), g = Double(row[x * 4 + 1]), r = Double(row[x * 4 + 2])
                    sum += 0.2126 * r + 0.7152 * g + 0.0722 * b
                } else {
                    sum += Double(row[x])
                }
                n += 1
                x += 16
            }
            y += 16
        }
        return n > 0 ? sum / Double(n) : Double.nan
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        if stopping || gShuttingDown { return }
        fatal("stream_stopped", "\(error)", code: (error as NSError).code)
    }

    func flushLogs() { frameLog?.flush() }

    func stop() {
        stopping = true
        wdTimer?.cancel()
        if let s = stream {
            let sem = DispatchSemaphore(value: 0)
            s.stopCapture { _ in sem.signal() }
            _ = sem.wait(timeout: .now() + 5)
        }
        frameQ.sync {}
        dumpQ.sync {}
        frameLog?.close()
        ring?.close()
        ring = nil
    }
}
