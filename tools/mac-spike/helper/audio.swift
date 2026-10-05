import Foundation
import CoreAudio
import AudioToolbox

// MARK: Core Audio property helpers

func caAddr(_ sel: AudioObjectPropertySelector,
            scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
            elem: AudioObjectPropertyElement = kAudioObjectPropertyElementMain) -> AudioObjectPropertyAddress {
    return AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: elem)
}

func caScalar<T: BitwiseCopyable>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ def: T) -> (T, OSStatus) {
    var a = caAddr(sel)
    var v = def
    var sz = UInt32(MemoryLayout<T>.size)
    let st = AudioObjectGetPropertyData(obj, &a, 0, nil, &sz, &v)
    return (v, st)
}

func caString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var a = caAddr(sel)
    var cf: Unmanaged<CFString>? = nil
    var sz = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let st = AudioObjectGetPropertyData(obj, &a, 0, nil, &sz, &cf)
    guard st == noErr, let c = cf else { return nil }
    return c.takeRetainedValue() as String
}

func caArray<T: BitwiseCopyable>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ def: T) -> [T] {
    var a = caAddr(sel)
    var sz: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &a, 0, nil, &sz) == noErr, sz > 0 else { return [] }
    var arr = [T](repeating: def, count: Int(sz) / MemoryLayout<T>.stride)
    var sz2 = UInt32(arr.count * MemoryLayout<T>.stride)
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &sz2, &arr) == noErr else { return [] }
    return arr
}

func caProcessObject(forPID pid: Int32) -> AudioObjectID {
    var a = caAddr(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var q = pid
    var obj = AudioObjectID(kAudioObjectUnknown)
    var sz = UInt32(MemoryLayout<AudioObjectID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a,
                                        UInt32(MemoryLayout<pid_t>.size), &q, &sz, &obj)
    return st == noErr ? obj : AudioObjectID(kAudioObjectUnknown)
}

func caAllProcessObjects() -> [AudioObjectID] {
    return caArray(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList, AudioObjectID(0))
}

func caProcessPID(_ obj: AudioObjectID) -> Int32 {
    return caScalar(obj, kAudioProcessPropertyPID, Int32(-1)).0
}

func listAudioProcs() {
    var n = 0
    for o in caAllProcessObjects() {
        emit("audio_proc", [
            "object_id": Int(o), "pid": Int(caProcessPID(o)),
            "bundle_id": caString(o, kAudioProcessPropertyBundleID) ?? "",
            "running_output": caScalar(o, kAudioProcessPropertyIsRunningOutput, UInt32(0)).0 != 0,
            "running_input": caScalar(o, kAudioProcessPropertyIsRunningInput, UInt32(0)).0 != 0,
        ])
        n += 1
    }
    emit("done", ["count": n])
}

// MARK: process tree

func descendants(of roots: [Int32]) -> [Int32] {
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
    var size = 0
    guard sysctl(&mib, 3, nil, &size, nil, 0) == 0 else { return roots }
    var buf = [kinfo_proc](repeating: kinfo_proc(), count: size / MemoryLayout<kinfo_proc>.stride + 32)
    size = buf.count * MemoryLayout<kinfo_proc>.stride
    guard sysctl(&mib, 3, &buf, &size, nil, 0) == 0 else { return roots }
    let n = size / MemoryLayout<kinfo_proc>.stride
    var children: [Int32: [Int32]] = [:]
    for i in 0..<n {
        children[Int32(buf[i].kp_eproc.e_ppid), default: []].append(Int32(buf[i].kp_proc.p_pid))
    }
    var seen = Set<Int32>()
    var order: [Int32] = []
    var stack = roots
    while let p = stack.popLast() {
        if seen.contains(p) { continue }
        seen.insert(p); order.append(p)
        stack.append(contentsOf: children[p] ?? [])
    }
    return order
}

// MARK: capture

struct AudioConfig {
    var pids: [Int32] = []
    var trees: [Int32] = []
    var wait = 10.0
    var mute = "unmuted"
    var outPath: String? = nil
    var logPath: String? = nil
}

final class AudioCapture {
    let cfg: AudioConfig
    private var wav: UnsafeMutablePointer<FILE>?
    private var csv: LineFile?
    private let writeQ = DispatchQueue(label: "mc.audio.write")
    private let ioQ = DispatchQueue(label: "mc.audio.io", qos: .userInteractive)
    private var dataBytes: UInt64 = 0

    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private var started = false
    private var stopped = false
    private var devListener: AudioObjectPropertyListenerBlock?
    private let devListenerQ = DispatchQueue(label: "mc.audio.devchange")

    private var channels = 2
    private var sampleRate = 48000.0
    private var scratch = [Int16](repeating: 0, count: 8192 * 2)
    // IO スレッドだけが触る
    private var silentSince: Double? = nil
    private var silentReported = false
    private(set) var startInfo: [String: Any] = [:]

    init(cfg: AudioConfig) { self.cfg = cfg }

    func prepareFiles() {
        if let p = cfg.outPath {
            guard let f = fopen(p, "wb") else { usageError("cannot open --audio-out \(p)") }
            wav = f
        }
        if let p = cfg.logPath {
            guard let f = LineFile(path: p) else { usageError("cannot open --audio-log \(p)") }
            f.write("host_time,sample_time,frames,now,peak\n")
            csv = f
        }
    }

    private func resolveObjects() -> (pids: [Int32], objs: [AudioObjectID]) {
        var pidList = cfg.pids
        if !cfg.trees.isEmpty { pidList.append(contentsOf: descendants(of: cfg.trees)) }
        var seen = Set<Int32>()
        var pids: [Int32] = [], objs: [AudioObjectID] = []
        for p in pidList where !seen.contains(p) {
            seen.insert(p)
            let o = caProcessObject(forPID: p)
            if o != AudioObjectID(kAudioObjectUnknown) { pids.append(p); objs.append(o) }
        }
        return (pids, objs)
    }

    func start(done: @escaping () -> Void) {
        let th = Thread { [self] in
            let deadline = nowSec() + cfg.wait
            var r = resolveObjects()
            while r.objs.isEmpty && nowSec() < deadline && !gShuttingDown {
                Thread.sleep(forTimeInterval: 0.25)
                r = resolveObjects()
            }
            if gShuttingDown { return }
            if r.objs.isEmpty {
                fatal("audio_objects", "no audio process objects for the requested pids within \(cfg.wait)s", code: 0)
                return
            }
            if setup(pids: r.pids, objs: r.objs) { done() }
        }
        th.name = "mc.audio.setup"
        th.start()
    }

    private func setup(pids: [Int32], objs: [AudioObjectID]) -> Bool {
        let desc = CATapDescription(stereoMixdownOfProcesses: objs)
        desc.uuid = UUID()
        desc.isPrivate = true
        switch cfg.mute {
        case "muted": desc.muteBehavior = CATapMuteBehavior.muted
        case "mutedWhenTapped": desc.muteBehavior = CATapMuteBehavior.mutedWhenTapped
        default: desc.muteBehavior = CATapMuteBehavior.unmuted
        }
        var st = AudioHardwareCreateProcessTap(desc, &tapID)
        guard st == noErr else { fatal("create_process_tap", "AudioHardwareCreateProcessTap failed", code: Int(st)); return false }

        var asbd = AudioStreamBasicDescription()
        var fa = caAddr(kAudioTapPropertyFormat)
        var fsz = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        st = AudioObjectGetPropertyData(tapID, &fa, 0, nil, &fsz, &asbd)
        guard st == noErr else { fatal("tap_format", "reading kAudioTapPropertyFormat failed", code: Int(st)); return false }
        sampleRate = asbd.mSampleRate
        channels = max(1, Int(asbd.mChannelsPerFrame))

        let (outDev, st1) = caScalar(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, AudioDeviceID(kAudioObjectUnknown))
        guard st1 == noErr, outDev != AudioDeviceID(kAudioObjectUnknown), let outUID = caString(outDev, kAudioDevicePropertyDeviceUID) else {
            fatal("default_output", "no default output device", code: Int(st1)); return false
        }
        let outName = caString(outDev, kAudioObjectPropertyName) ?? ""

        let dict: [String: Any] = [
            kAudioAggregateDeviceNameKey: "MCSpike-Tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapDriftCompensationKey: true,
                kAudioSubTapUIDKey: desc.uuid.uuidString,
            ]],
        ]
        st = AudioHardwareCreateAggregateDevice(dict as CFDictionary, &aggID)
        guard st == noErr else { fatal("create_aggregate", "AudioHardwareCreateAggregateDevice failed", code: Int(st)); return false }

        // IOProc はタップの形式ではなく集約デバイス（主デバイス）のレートで呼ばれる
        let tapRate = sampleRate
        let (aggRate, rst) = caScalar(aggID, kAudioDevicePropertyNominalSampleRate, Float64(0))
        if rst == noErr, aggRate > 0 { sampleRate = aggRate }

        writeWavHeader()

        st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQ) { [weak self] inNow, inInput, inInputTime, _, _ in
            self?.ioProc(now: inNow.pointee, input: inInput, inTime: inInputTime.pointee)
        }
        guard st == noErr, procID != nil else { fatal("create_ioproc", "AudioDeviceCreateIOProcIDWithBlock failed", code: Int(st)); return false }

        // 既定出力デバイスの変更を監視する
        var la = caAddr(kAudioHardwarePropertyDefaultOutputDevice)
        let lb: AudioObjectPropertyListenerBlock = { _, _ in
            let (d, _) = caScalar(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, AudioDeviceID(kAudioObjectUnknown))
            emit("audio_device_changed", ["t": nowSec(), "device": caString(d, kAudioObjectPropertyName) ?? "",
                                          "uid": caString(d, kAudioDevicePropertyDeviceUID) ?? ""])
        }
        devListener = lb
        AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &la, devListenerQ, lb)

        startInfo = [
            "pids": pids.map { Int($0) }, "object_ids": objs.map { Int($0) },
            "sample_rate": sampleRate, "tap_rate": tapRate, "channels": channels, "output_device": outName,
            "format_flags": Int(asbd.mFormatFlags), "bits": Int(asbd.mBitsPerChannel),
            "mute": cfg.mute,
        ]
        st = AudioDeviceStart(aggID, procID)
        guard st == noErr else { fatal("device_start", "AudioDeviceStart failed", code: Int(st)); return false }
        started = true
        return true
    }

    // MARK: IO thread

    private func ioProc(now: AudioTimeStamp, input: UnsafePointer<AudioBufferList>, inTime: AudioTimeStamp) {
        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard abl.count > 0 else { return }
        let interleaved = abl.count == 1
        let nch = interleaved ? max(1, Int(abl[0].mNumberChannels)) : abl.count
        let frames = Int(abl[0].mDataByteSize) / (4 * (interleaved ? nch : 1))
        let outCh = channels
        let need = frames * outCh
        if scratch.count < need { scratch = [Int16](repeating: 0, count: need) }

        var peak: Float = 0
        var anyNonZero = false
        scratch.withUnsafeMutableBufferPointer { dst in
            if interleaved {
                guard let p = abl[0].mData?.assumingMemoryBound(to: Float.self) else { return }
                for f in 0..<frames {
                    for c in 0..<outCh {
                        var s: Float = c < nch ? p[f * nch + c] : 0
                        if s.isNaN { s = 0 }
                        if s != 0 { anyNonZero = true }
                        let a = abs(s); if a > peak { peak = a }
                        dst[f * outCh + c] = Int16((max(-1, min(1, s)) * 32767).rounded())
                    }
                }
            } else {
                for c in 0..<outCh {
                    guard c < nch, let p = abl[c].mData?.assumingMemoryBound(to: Float.self) else {
                        for f in 0..<frames { dst[f * outCh + c] = 0 }
                        continue
                    }
                    let nf = min(frames, Int(abl[c].mDataByteSize) / 4)
                    for f in 0..<nf {
                        var s = p[f]
                        if s.isNaN { s = 0 }
                        if s != 0 { anyNonZero = true }
                        let a = abs(s); if a > peak { peak = a }
                        dst[f * outCh + c] = Int16((max(-1, min(1, s)) * 32767).rounded())
                    }
                    if nf < frames { for f in nf..<frames { dst[f * outCh + c] = 0 } }
                }
            }
        }

        let hostT = ticksToSec(inTime.mHostTime)
        let nowT = ticksToSec(now.mHostTime)
        let sampleT = inTime.mSampleTime
        gStats.update {
            $0.audioCb += 1
            $0.audioFrames += frames
            if !anyNonZero { $0.audioZeroCb += 1 }
            $0.audioPeak = max($0.audioPeak, Double(peak))
        }

        // 無音検出（IO スレッドのみ）
        if anyNonZero {
            silentSince = nil
            if silentReported { silentReported = false; emit("audio_unsilent", ["t": nowT]) }
        } else {
            if silentSince == nil { silentSince = nowT }
            if !silentReported, let s = silentSince, nowT - s >= 5.0 {
                silentReported = true
                emit("audio_silent", ["t": nowT, "since": s])
            }
        }

        let bytes = frames * outCh * 2
        let data = scratch.withUnsafeBytes { Data(bytes: $0.baseAddress!, count: bytes) }
        let peakD = Double(peak)
        writeQ.async { [self] in
            if let w = wav {
                data.withUnsafeBytes { _ = fwrite($0.baseAddress, 1, $0.count, w) }
                dataBytes += UInt64(data.count)
            }
            csv?.write("\(hostT),\(sampleT),\(frames),\(nowT),\(peakD)\n")
        }
    }

    // MARK: WAV

    private func le32(_ v: UInt32) -> [UInt8] { return [UInt8(v & 255), UInt8((v >> 8) & 255), UInt8((v >> 16) & 255), UInt8((v >> 24) & 255)] }
    private func le16(_ v: UInt16) -> [UInt8] { return [UInt8(v & 255), UInt8(v >> 8)] }

    private func writeWavHeader() {
        guard let w = wav else { return }
        var h: [UInt8] = []
        h += Array("RIFF".utf8) + le32(36) + Array("WAVE".utf8) + Array("fmt ".utf8) + le32(16)
        h += le16(1) + le16(UInt16(channels)) + le32(UInt32(sampleRate))
        h += le32(UInt32(sampleRate) * UInt32(channels) * 2) + le16(UInt16(channels * 2)) + le16(16)
        h += Array("data".utf8) + le32(0)
        fwrite(h, 1, h.count, w)
    }

    private func finalizeWav() {
        guard let w = wav else { return }
        let ds = UInt32(truncatingIfNeeded: dataBytes)
        fseek(w, 4, SEEK_SET); fwrite(le32(36 &+ ds), 1, 4, w)
        fseek(w, 40, SEEK_SET); fwrite(le32(ds), 1, 4, w)
        fclose(w)
        wav = nil
    }

    func flushLogs() {
        csv?.flush()
        writeQ.async { [self] in if let w = wav { fflush(w) } }
    }

    // MARK: stop

    func stop() {
        if stopped { return }
        stopped = true
        if let l = devListener {
            var la = caAddr(kAudioHardwarePropertyDefaultOutputDevice)
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &la, devListenerQ, l)
        }
        if aggID != AudioObjectID(kAudioObjectUnknown), let p = procID {
            if started { AudioDeviceStop(aggID, p) }
            AudioDeviceDestroyIOProcID(aggID, p)
            procID = nil
        }
        if aggID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyAggregateDevice(aggID)
            aggID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        ioQ.sync {}
        writeQ.sync { finalizeWav() }
        csv?.close()
    }
}
