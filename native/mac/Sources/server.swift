import Foundation
import ScreenCaptureKit

final class Server {
    private let cmdQ = DispatchQueue(label: "mc.cmd")
    private var video: VideoSession?
    private var audio: AudioSession?
    private var shuttingDown = false
    private var statsTimer: DispatchSourceTimer?
    let disclaimed: Bool

    init(disclaimed: Bool) { self.disclaimed = disclaimed }

    func run() {
        emit("ready", ["version": mcVersion, "disclaimed": disclaimed])
        let t = DispatchSource.makeTimerSource(queue: cmdQ)
        t.schedule(deadline: .now() + 1, repeating: 1, leeway: .milliseconds(20))
        t.setEventHandler { [weak self] in self?.emitStats() }
        statsTimer = t
        t.resume()

        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        for sig in [SIGINT, SIGTERM] {
            let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            s.setEventHandler { [weak self] in log("signal \(sig)"); self?.shutdown(sayBye: false, id: nil) }
            s.resume()
            gSignalSources.append(s)
        }

        let th = Thread { [self] in
            while let line = readLine(strippingNewline: true) {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.isEmpty { continue }
                cmdQ.async { self.handle(trimmed) }
            }
            log("stdin EOF")
            shutdown(sayBye: false, id: nil)
        }
        th.name = "mc.stdin"
        th.start()
    }

    private var gSignalSources: [DispatchSourceSignal] = []

    // MARK: stats

    private func emitStats() {
        guard video != nil || audio != nil else { return }
        let c = gStats.take()
        emit("stats", [
            "complete": c.complete, "idle": c.idle, "other": c.other,
            "audio_frames": c.audioFrames, "audio_zero": c.audioCb > 0 && c.audioZeroCb == c.audioCb,
            "shm_skipped": c.shmSkipped, "unchanged": c.unchanged, "audio_dropped": c.audioDropped,
        ])
    }

    // MARK: shutdown

    func shutdown(sayBye: Bool, id: Any?) {
        cmdQ.async { [self] in
            if shuttingDown { return }
            shuttingDown = true
            statsTimer?.cancel()
            video?.stop(); video = nil
            audio?.stop(); audio = nil
            if sayBye { emit("bye", [:], id: id) }
            flushOut()
            exit(0)
        }
    }

    // MARK: command dispatch

    private func num(_ v: Any?) -> Double? {
        guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        let d = n.doubleValue
        return d.isFinite ? d : nil
    }

    private func crop(_ v: Any?) -> CGRect? {
        guard let a = v as? [Any], a.count == 4 else { return nil }
        let n = a.compactMap { num($0) }
        guard n.count == 4, n[2] > 0, n[3] > 0 else { return nil }
        return CGRect(x: n[0], y: n[1], width: n[2], height: n[3])
    }

    private func handle(_ line: String) {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let msg = obj as? [String: Any] else {
            emitError("command", "malformed JSON: expected one JSON object per line")
            return
        }
        let id = msg["id"]
        guard let cmd = (msg["cmd"] ?? msg["command"]) as? String else {
            emitError("command", "missing \"cmd\"", id: id)
            return
        }
        switch cmd {
        case "check_permission":
            emit("permission", permissionFields(), id: id)
        case "request_permission":
            DispatchQueue.global().async { emit("permission", requestPermissions(), id: id) }
        case "list_windows": listWindows(msg, id)
        case "start_video": startVideo(msg, id)
        case "update_video": updateVideo(msg, id)
        case "stop_video":
            video?.stop(); video = nil
            emit("video_stopped", [:], id: id)
        case "start_audio": startAudio(msg, id)
        case "stop_audio":
            audio?.stop(); audio = nil
            emit("audio_stopped", [:], id: id)
        case "quit":
            shutdown(sayBye: true, id: id)
        case "selftest_video": selftestVideo(msg, id)
        case "selftest_audio": selftestAudio(msg, id)
        default:
            emitError("command", "unknown command: \(cmd)", id: id)
        }
    }

    private func listWindows(_ msg: [String: Any], _ id: Any?) {
        let pid = num(msg["pid"]).map { Int32($0) }
        ensureAppKit()
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, err in
            guard let content = content else {
                emitError("list_windows", err ?? NSError(domain: "mc", code: 0), id: id)
                return
            }
            var out: [[String: Any]] = []
            for w in content.windows {
                let wp = w.owningApplication?.processID ?? 0
                if let pid = pid, wp != pid { continue }
                out.append([
                    "window_id": Int(w.windowID), "pid": Int(wp), "title": w.title ?? "",
                    "frame": [w.frame.origin.x, w.frame.origin.y, w.frame.size.width, w.frame.size.height],
                    "on_screen": w.isOnScreen, "layer": w.windowLayer,
                ])
            }
            emit("windows", ["windows": out], id: id)
        }
    }

    private func attach(_ v: VideoSession) {
        video = v
        v.onEnded = { [weak self, weak v] in
            self?.cmdQ.async { if let s = self, let v = v, s.video === v { s.video = nil } }
        }
    }

    private func startVideo(_ m: [String: Any], _ id: Any?) {
        if video != nil { emitError("start_video", "video already running", id: id); return }
        guard let wid = num(m["window_id"]), wid >= 0, let w = num(m["width"]), let h = num(m["height"]),
              w > 0, h > 0, Int(w) % 2 == 0, Int(h) % 2 == 0,
              let cr = crop(m["crop"]), let fps = num(m["fps"]), fps > 0,
              let shm = m["shm"] as? String, !shm.isEmpty else {
            emitError("start_video", "invalid arguments (need window_id, even width/height, crop[x,y,w,h], fps>0, shm)", id: id)
            return
        }
        let v = VideoCapture(params: VideoParams(windowID: UInt32(wid), width: Int(w), height: Int(h), crop: cr, fps: fps, shm: shm))
        attach(v)
        v.start(id: id)
    }

    private func updateVideo(_ m: [String: Any], _ id: Any?) {
        guard let v = video else { emitError("update_video", "video is not running", id: id); return }
        guard let cr = crop(m["crop"]) else { emitError("update_video", "invalid crop", id: id); return }
        v.update(crop: cr, id: id)
    }

    private func startAudio(_ m: [String: Any], _ id: Any?) {
        if audio != nil { emitError("start_audio", "audio already running", id: id); return }
        let mute = (m["mute"] as? String) ?? "unmuted"
        guard let tp = num(m["tree_pid"]), tp > 0, ["unmuted", "muted", "mutedWhenTapped"].contains(mute),
              let fifo = m["fifo"] as? String, !fifo.isEmpty else {
            emitError("start_audio", "invalid arguments (need tree_pid, fifo, mute in unmuted|muted|mutedWhenTapped)", id: id)
            return
        }
        let a = AudioCapture(treePid: Int32(tp), mute: mute, fifo: fifo)
        audio = a
        a.start(id: id)
    }

    // MARK: test commands

    private func selftestVideo(_ m: [String: Any], _ id: Any?) {
        if video != nil { emitError("selftest_video", "video already running", id: id); return }
        guard let w = num(m["width"]), let h = num(m["height"]), w > 0, h > 0, Int(w) % 2 == 0, Int(h) % 2 == 0,
              let shm = m["shm"] as? String, !shm.isEmpty else {
            emitError("selftest_video", "invalid arguments (need shm, even width/height; optional fps)", id: id)
            return
        }
        let fps = num(m["fps"]) ?? 30
        guard fps > 0 else { emitError("selftest_video", "invalid fps", id: id); return }
        let v = SyntheticVideo(width: Int(w), height: Int(h), fps: fps, shm: shm)
        attach(v)
        v.start(id: id)
    }

    private func selftestAudio(_ m: [String: Any], _ id: Any?) {
        if audio != nil { emitError("selftest_audio", "audio already running", id: id); return }
        guard let fifo = m["fifo"] as? String, !fifo.isEmpty else {
            emitError("selftest_audio", "invalid arguments (need fifo; optional freq)", id: id)
            return
        }
        let a = SyntheticAudio(fifo: fifo, freq: num(m["freq"]) ?? 1000)
        audio = a
        a.start(id: id)
    }
}
