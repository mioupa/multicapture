import Foundation
import CoreAudio
import AudioToolbox
import AVFAudio

// MARK: Core Audio property helpers

func caAddr(_ sel: AudioObjectPropertySelector,
            scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
            elem: AudioObjectPropertyElement = kAudioObjectPropertyElementMain) -> AudioObjectPropertyAddress {
    return AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: elem)
}

func caScalar<T: BitwiseCopyable>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ def: T,
                                  scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> (T, OSStatus) {
    var a = caAddr(sel, scope: scope)
    var v = def
    var sz = UInt32(MemoryLayout<T>.size)
    let st = AudioObjectGetPropertyData(obj, &a, 0, nil, &sz, &v)
    return (v, st)
}

/// 出力デバイスで、IOProc に渡した音が実際に鳴るまでの遅れ（秒）。
/// デバイスの遅延・安全余裕・バッファ・ストリームの遅延の合計を、デバイスのレートで割る。
func caOutputLatency(_ dev: AudioDeviceID) -> Double {
    let out = kAudioObjectPropertyScopeOutput
    let (rate, _) = caScalar(dev, kAudioDevicePropertyNominalSampleRate, Float64(0))
    guard rate > 0 else { return 0 }
    let (lat, _) = caScalar(dev, kAudioDevicePropertyLatency, UInt32(0), scope: out)
    let (safety, _) = caScalar(dev, kAudioDevicePropertySafetyOffset, UInt32(0), scope: out)
    let (buffer, _) = caScalar(dev, kAudioDevicePropertyBufferFrameSize, UInt32(0), scope: out)
    var a = caAddr(kAudioDevicePropertyStreams, scope: out)
    var size: UInt32 = 0
    var streamLat: UInt32 = 0
    if AudioObjectGetPropertyDataSize(dev, &a, 0, nil, &size) == noErr, size >= UInt32(MemoryLayout<AudioStreamID>.size) {
        var streams = [AudioStreamID](repeating: 0, count: Int(size) / MemoryLayout<AudioStreamID>.size)
        if AudioObjectGetPropertyData(dev, &a, 0, nil, &size, &streams) == noErr, let s = streams.first {
            streamLat = caScalar(s, kAudioStreamPropertyLatency, UInt32(0)).0
        }
    }
    return Double(lat + safety + buffer + streamLat) / rate
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

func caDefaultOutput() -> AudioDeviceID {
    return caScalar(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice,
                    AudioDeviceID(kAudioObjectUnknown)).0
}

// MARK: process tree

func descendants(of root: Int32) -> [Int32] {
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0]
    var size = 0
    guard sysctl(&mib, 3, nil, &size, nil, 0) == 0 else { return [root] }
    var buf = [kinfo_proc](repeating: kinfo_proc(), count: size / MemoryLayout<kinfo_proc>.stride + 32)
    size = buf.count * MemoryLayout<kinfo_proc>.stride
    guard sysctl(&mib, 3, &buf, &size, nil, 0) == 0 else { return [root] }
    let n = size / MemoryLayout<kinfo_proc>.stride
    var children: [Int32: [Int32]] = [:]
    for i in 0..<n {
        children[Int32(buf[i].kp_eproc.e_ppid), default: []].append(Int32(buf[i].kp_proc.p_pid))
    }
    var seen = Set<Int32>()
    var order: [Int32] = []
    var stack = [root]
    while let p = stack.popLast() {
        if seen.contains(p) { continue }
        seen.insert(p); order.append(p)
        stack.append(contentsOf: children[p] ?? [])
    }
    return order
}

/// tree_pid とその子孫のうち、オーディオのプロセスオブジェクトを持つもの。
func resolveTargets(treePid: Int32) -> [(pid: Int32, obj: AudioObjectID)] {
    var out: [(Int32, AudioObjectID)] = []
    for p in descendants(of: treePid) {
        let o = caProcessObject(forPID: p)
        if o != AudioObjectID(kAudioObjectUnknown) { out.append((p, o)) }
    }
    return out.sorted { $0.1 < $1.1 }
}

// MARK: record format

/// FIFO のレコード：u32 magic "MCAU" / f64 timestamp / u32 frames / s16le×2ch×frames
func makeAudioRecord(timestamp: Double, frames: Int, payload: UnsafeRawPointer?) -> Data {
    var d = Data(count: 16 + frames * 4)
    d.withUnsafeMutableBytes { raw in
        raw.storeBytes(of: UInt32(0x5541434D).littleEndian, toByteOffset: 0, as: UInt32.self)
        raw.storeBytes(of: timestamp.bitPattern.littleEndian, toByteOffset: 4, as: UInt64.self)
        raw.storeBytes(of: UInt32(frames).littleEndian, toByteOffset: 12, as: UInt32.self)
        if let p = payload, frames > 0 { memcpy(raw.baseAddress! + 16, p, frames * 4) }
    }
    return d
}

struct CAFailure: Error { let whereStr: String; let msg: String; let code: Int }

// MARK: pipeline = tap + aggregate device + IOProc + converter（作り直しの単位）

final class Pipeline {
    let pids: [Int32]
    let objs: [AudioObjectID]
    let outDev: AudioDeviceID
    let outName: String
    /// タップの音は出力デバイスで鳴る前のもの。Chrome は鳴る時刻に映像を合わせるので、この分を足す。
    let outputLatency: Double
    private(set) var rate = 0.0
    let startT = nowSec()
    private let fifo: FifoWriter

    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private var started = false
    private let ioQ = DispatchQueue(label: "mc.audio.io", qos: .userInteractive)
    private var rateListener: AudioObjectPropertyListenerBlock?
    private let listenerQ: DispatchQueue

    private let nzLock = NSLock()
    private var lastNonZero = nowSec()

    // IO スレッドだけが触る
    private var inFmt: AVAudioFormat!
    private var outFmt: AVAudioFormat!
    private var conv: AVAudioConverter!
    private var inBuf: AVAudioPCMBuffer!
    private var outBuf: AVAudioPCMBuffer!
    private var inBytesPerFrame = 8
    private var convErrReported = false

    init(pids: [Int32], objs: [AudioObjectID], outDev: AudioDeviceID, outName: String,
         fifo: FifoWriter, listenerQ: DispatchQueue) {
        self.pids = pids; self.objs = objs; self.outDev = outDev; self.outName = outName
        self.outputLatency = caOutputLatency(outDev)
        self.fifo = fifo; self.listenerQ = listenerQ
    }

    var silentSince: Double { nzLock.lock(); defer { nzLock.unlock() }; return max(lastNonZero, startT) }

    /// 失敗したら、作りかけのものを片付けて throw する。
    static func build(targets: [(pid: Int32, obj: AudioObjectID)], mute: String, fifo: FifoWriter,
                      listenerQ: DispatchQueue, onRateChange: @escaping () -> Void) throws -> Pipeline {
        let outDev = caDefaultOutput()
        guard outDev != AudioDeviceID(kAudioObjectUnknown), let outUID = caString(outDev, kAudioDevicePropertyDeviceUID) else {
            throw CAFailure(whereStr: "default_output", msg: "no default output device", code: 0)
        }
        let outName = caString(outDev, kAudioObjectPropertyName) ?? ""
        let objs = targets.map { $0.obj }

        let desc = CATapDescription(stereoMixdownOfProcesses: objs)
        desc.uuid = UUID()
        desc.isPrivate = true
        switch mute {
        case "muted": desc.muteBehavior = .muted
        case "mutedWhenTapped": desc.muteBehavior = .mutedWhenTapped
        default: desc.muteBehavior = .unmuted
        }
        let p = Pipeline(pids: targets.map { $0.pid }, objs: objs, outDev: outDev, outName: outName,
                         fifo: fifo, listenerQ: listenerQ)
        do {
            var st = AudioHardwareCreateProcessTap(desc, &p.tapID)
            guard st == noErr else { throw CAFailure(whereStr: "create_process_tap", msg: "AudioHardwareCreateProcessTap failed", code: Int(st)) }

            let dict: [String: Any] = [
                kAudioAggregateDeviceNameKey: "MultiCapture-Tap",
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
            st = AudioHardwareCreateAggregateDevice(dict as CFDictionary, &p.aggID)
            guard st == noErr else { throw CAFailure(whereStr: "create_aggregate", msg: "AudioHardwareCreateAggregateDevice failed", code: Int(st)) }

            // 集約デバイスの実際の入力形式。レートは主デバイスの公称レート（IOProc はそのレートで呼ばれる）。
            var asbd = AudioStreamBasicDescription()
            var a = caAddr(kAudioDevicePropertyStreamFormat, scope: kAudioObjectPropertyScopeInput)
            var sz = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
            st = AudioObjectGetPropertyData(p.aggID, &a, 0, nil, &sz, &asbd)
            if st != noErr || asbd.mSampleRate <= 0 || asbd.mChannelsPerFrame == 0 {
                var fa = caAddr(kAudioTapPropertyFormat)
                var fsz = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
                st = AudioObjectGetPropertyData(p.tapID, &fa, 0, nil, &fsz, &asbd)
                guard st == noErr else { throw CAFailure(whereStr: "input_format", msg: "reading input stream format failed", code: Int(st)) }
            }
            let (nominal, nst) = caScalar(p.aggID, kAudioDevicePropertyNominalSampleRate, Float64(0))
            if nst == noErr, nominal > 0 {
                if asbd.mSampleRate != nominal { log("input format rate \(asbd.mSampleRate) != nominal \(nominal); using nominal") }
                asbd.mSampleRate = nominal
            }
            guard asbd.mFormatID == kAudioFormatLinearPCM, let inFmt = AVAudioFormat(streamDescription: &asbd),
                  let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 48000, channels: 2, interleaved: true),
                  let conv = AVAudioConverter(from: inFmt, to: outFmt) else {
                throw CAFailure(whereStr: "converter", msg: "cannot create converter for input format (rate \(asbd.mSampleRate), ch \(asbd.mChannelsPerFrame), flags \(asbd.mFormatFlags))", code: 0)
            }
            p.inFmt = inFmt; p.outFmt = outFmt; p.conv = conv
            p.inBytesPerFrame = max(1, Int(asbd.mBytesPerFrame))
            var bufFrames = UInt32(0)
            let (bf, bst) = caScalar(p.aggID, kAudioDevicePropertyBufferFrameSize, UInt32(0))
            if bst == noErr { bufFrames = bf }
            try p.ensureCapacity(frames: max(16384, Int(bufFrames) * 4))

            let rate = asbd.mSampleRate
            let q = p.ioQ
            st = AudioDeviceCreateIOProcIDWithBlock(&p.procID, p.aggID, q) { [unowned p] inNow, inInput, inInputTime, _, _ in
                p.ioProc(now: inNow.pointee, input: inInput, inTime: inInputTime.pointee)
            }
            guard st == noErr, p.procID != nil else { throw CAFailure(whereStr: "create_ioproc", msg: "AudioDeviceCreateIOProcIDWithBlock failed", code: Int(st)) }

            // 主デバイスのレート変更を監視
            var ra = caAddr(kAudioDevicePropertyNominalSampleRate)
            let lb: AudioObjectPropertyListenerBlock = { _, _ in onRateChange() }
            p.rateListener = lb
            AudioObjectAddPropertyListenerBlock(outDev, &ra, listenerQ, lb)

            st = AudioDeviceStart(p.aggID, p.procID)
            guard st == noErr else { throw CAFailure(whereStr: "device_start", msg: "AudioDeviceStart failed", code: Int(st)) }
            p.started = true
            p.rate = rate
            return p
        } catch {
            p.teardown()
            throw error
        }
    }

    private func ensureCapacity(frames: Int) throws {
        guard let i = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: AVAudioFrameCount(frames)) else {
            throw CAFailure(whereStr: "converter", msg: "cannot allocate input buffer", code: 0)
        }
        let outFrames = Int((Double(frames) * 48000 / max(1, inFmt.sampleRate)).rounded(.up)) + 256
        guard let o = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: AVAudioFrameCount(outFrames)) else {
            throw CAFailure(whereStr: "converter", msg: "cannot allocate output buffer", code: 0)
        }
        inBuf = i; outBuf = o
    }

    // MARK: IO thread

    private func ioProc(now: AudioTimeStamp, input: UnsafePointer<AudioBufferList>, inTime: AudioTimeStamp) {
        let src = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard src.count > 0 else { return }
        let frames = Int(src[0].mDataByteSize) / inBytesPerFrame
        guard frames > 0 else { return }
        if frames > Int(inBuf.frameCapacity) {
            do { try ensureCapacity(frames: frames * 2) } catch { return }
        }
        inBuf.frameLength = AVAudioFrameCount(frames)
        let dst = UnsafeMutableAudioBufferListPointer(inBuf.mutableAudioBufferList)
        for i in 0..<min(src.count, dst.count) {
            guard let s = src[i].mData, let d = dst[i].mData else { continue }
            memcpy(d, s, min(Int(src[i].mDataByteSize), Int(dst[i].mDataByteSize)))
        }

        var supplied = false
        var err: NSError?
        outBuf.frameLength = 0
        let status = conv.convert(to: outBuf, error: &err) { [inBuf] _, outStatus in
            if supplied { outStatus.pointee = .noDataNow; return nil }
            supplied = true
            outStatus.pointee = .haveData
            return inBuf
        }
        if status == .error {
            if !convErrReported {
                convErrReported = true
                emitError("convert", err?.localizedDescription ?? "AVAudioConverter failed", code: err?.code ?? 0)
            }
            return
        }
        let outFrames = Int(outBuf.frameLength)
        guard outFrames > 0, let data = outBuf.audioBufferList.pointee.mBuffers.mData else { return }

        // 非ゼロ判定（出力側の s16 で）
        var nonZero = false
        let words = data.assumingMemoryBound(to: UInt64.self)
        let nWords = outFrames * 4 / 8
        for i in 0..<nWords where words[i] != 0 { nonZero = true; break }
        if !nonZero && (outFrames * 4) % 8 != 0 {
            if data.load(fromByteOffset: nWords * 8, as: UInt32.self) != 0 { nonZero = true }
        }
        if nonZero { nzLock.lock(); lastNonZero = nowSec(); nzLock.unlock() }

        gStats.update {
            $0.audioFrames += outFrames
            $0.audioCb += 1
            if !nonZero { $0.audioZeroCb += 1 }
        }
        var ts = ticksToSec(inTime.mHostTime)
        if inTime.mFlags.rawValue & AudioTimeStampFlags.hostTimeValid.rawValue == 0 { ts = ticksToSec(now.mHostTime) }
        fifo.enqueue(makeAudioRecord(timestamp: ts + outputLatency, frames: outFrames, payload: data))
    }

    // MARK: teardown

    func teardown() {
        if let l = rateListener {
            var ra = caAddr(kAudioDevicePropertyNominalSampleRate)
            AudioObjectRemovePropertyListenerBlock(outDev, &ra, listenerQ, l)
            rateListener = nil
        }
        if aggID != AudioObjectID(kAudioObjectUnknown), let pid = procID {
            if started { AudioDeviceStop(aggID, pid) }
            AudioDeviceDestroyIOProcID(aggID, pid)
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
        started = false
    }
}

protocol AudioSession: AnyObject {
    func start(id: Any?)
    func stop()
}

// MARK: audio session (targets, rebuild, FIFO)

final class AudioCapture: AudioSession {
    let treePid: Int32
    let mute: String
    private let fifo: FifoWriter
    private let q = DispatchQueue(label: "mc.audio.ctl")
    private let listenerQ = DispatchQueue(label: "mc.audio.listeners")
    private var timer: DispatchSourceTimer?
    private var pipeline: Pipeline?
    private var startId: Any?
    private var startedEmitted = false
    private var notFoundReported = false
    private var deadline = 0.0
    private var nextTry = 0.0
    private var tick = 0
    private var pendingReason: String?
    private var runningSince: Double?
    private var silentRebuilds: [Double] = []
    private var lastErrorT: [String: Double] = [:]
    private var stopped = false
    private var devListener: AudioObjectPropertyListenerBlock?
    private var devDebounce: DispatchWorkItem?

    init(treePid: Int32, mute: String, fifo: String) {
        self.treePid = treePid; self.mute = mute
        self.fifo = FifoWriter(path: fifo)
    }

    func start(id: Any?) {
        fifo.start()
        q.async { [self] in
            startId = id
            deadline = nowSec() + 15
            var la = caAddr(kAudioHardwarePropertyDefaultOutputDevice)
            let lb: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.deviceChanged() }
            devListener = lb
            AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &la, listenerQ, lb)
            let t = DispatchSource.makeTimerSource(queue: q)
            t.schedule(deadline: .now(), repeating: 0.25, leeway: .milliseconds(20))
            t.setEventHandler { [weak self] in self?.onTick() }
            timer = t
            t.resume()
        }
    }

    // MARK: ticks

    private func onTick() {
        if stopped { return }
        tick += 1
        let now = nowSec()
        guard let p = pipeline else {
            if now >= nextTry { tryBuild() }
            return
        }
        if tick % 8 == 0 {      // 2 秒ごとにプロセスを調べ直す
            let cur = resolveTargets(treePid: treePid).map { $0.obj }
            if cur != p.objs { rebuild("process_changed"); return }
        }
        if tick % 4 == 0 { silentCheck(p, now) }
    }

    private func silentCheck(_ p: Pipeline, _ now: Double) {
        let running = p.objs.contains { caScalar($0, kAudioProcessPropertyIsRunningOutput, UInt32(0)).0 != 0 }
        if running { if runningSince == nil { runningSince = now } } else { runningSince = nil; return }
        guard let rs = runningSince, now - rs >= 5, now - p.silentSince >= 5 else { return }
        silentRebuilds.removeAll { now - $0 > 60 }
        if silentRebuilds.count >= 3 { return }
        silentRebuilds.append(now)
        rebuild("silent_tap")
    }

    private func deviceChanged() {
        devDebounce?.cancel()
        let w = DispatchWorkItem { [weak self] in
            guard let self = self, !self.stopped else { return }
            if let p = self.pipeline, p.outDev == caDefaultOutput() { return }
            self.rebuild("device_changed")
        }
        devDebounce = w
        q.asyncAfter(deadline: .now() + 0.3, execute: w)
    }

    private func rateChanged(_ pipe: Pipeline) {
        q.async { [self] in
            guard !stopped, pipeline === pipe else { return }
            rebuild("rate_changed")
        }
    }

    // MARK: build / rebuild

    private func rebuild(_ reason: String) {
        if let p = pipeline { p.teardown(); pipeline = nil }
        runningSince = nil
        pendingReason = reason
        nextTry = 0
        tryBuild()
    }

    private func rateLimitedError(_ whereStr: String, _ msg: String, code: Int) {
        let now = nowSec()
        if let l = lastErrorT[whereStr], now - l < 10 { return }
        lastErrorT[whereStr] = now
        emitError(whereStr, msg, code: code, id: startedEmitted ? nil : startId)
    }

    private func tryBuild() {
        let now = nowSec()
        let targets = resolveTargets(treePid: treePid)
        if targets.isEmpty {
            if now >= deadline {
                if !notFoundReported && !startedEmitted {
                    notFoundReported = true
                    emitError("audio_objects", "no audio process objects under pid \(treePid) within 15s; still waiting", id: startId)
                }
                nextTry = now + 2
            } else {
                nextTry = now + 0.25
            }
            return
        }
        do {
            var holder: Pipeline?
            let p = try Pipeline.build(targets: targets, mute: mute, fifo: fifo, listenerQ: listenerQ) { [weak self] in
                if let h = holder { self?.rateChanged(h) }
            }
            holder = p
            pipeline = p
            if !startedEmitted {
                startedEmitted = true
                emit("audio_started", ["pids": p.pids.map { Int($0) }, "device": p.outName, "device_rate": p.rate,
                                        "output_latency": p.outputLatency], id: startId)
                pendingReason = nil
            } else if let r = pendingReason {
                emit("audio_rebuilt", ["reason": r, "device": p.outName, "output_latency": p.outputLatency])
                pendingReason = nil
            }
        } catch let f as CAFailure {
            rateLimitedError(f.whereStr, f.msg, code: f.code)
            nextTry = now + 2
        } catch {
            rateLimitedError("audio_build", "\(error)", code: 0)
            nextTry = now + 2
        }
    }

    // MARK: stop

    func stop() {
        q.sync { [self] in
            if stopped { return }
            stopped = true
            timer?.cancel(); timer = nil
            devDebounce?.cancel()
            if let l = devListener {
                var la = caAddr(kAudioHardwarePropertyDefaultOutputDevice)
                AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &la, listenerQ, l)
                devListener = nil
            }
            pipeline?.teardown(); pipeline = nil
        }
        fifo.stop()
    }
}

/// 許可なしで FIFO を試すための 1kHz サイン波（48kHz s16 ステレオ、10ms ごとに 1 レコード）。
final class SyntheticAudio: AudioSession {
    private let fifo: FifoWriter
    private let freq: Double
    private let q = DispatchQueue(label: "mc.selftest.audio")
    private var timer: DispatchSourceTimer?
    private var t0 = 0.0
    private var produced = 0
    private var stopped = false

    init(fifo: String, freq: Double) { self.fifo = FifoWriter(path: fifo); self.freq = freq }

    func start(id: Any?) {
        fifo.start()
        t0 = nowSec()
        emit("audio_started", ["pids": [Int](), "device": "selftest", "device_rate": 48000.0], id: id)
        let t = DispatchSource.makeTimerSource(queue: q)
        t.schedule(deadline: .now(), repeating: 0.01, leeway: .milliseconds(1))
        t.setEventHandler { [weak self] in self?.tick() }
        timer = t
        t.resume()
    }

    private func tick() {
        if stopped { return }
        let want = Int((nowSec() - t0) * 48000) / 480 * 480
        while produced < want {
            var samples = [Int16](repeating: 0, count: 480 * 2)
            for i in 0..<480 {
                let v = Int16((sin(2 * Double.pi * freq * Double(produced + i) / 48000) * 0.5 * 32767).rounded())
                samples[i * 2] = v; samples[i * 2 + 1] = v
            }
            let ts = t0 + Double(produced) / 48000
            samples.withUnsafeBytes { fifo.enqueue(makeAudioRecord(timestamp: ts, frames: 480, payload: $0.baseAddress)) }
            gStats.update { $0.audioFrames += 480; $0.audioCb += 1 }
            produced += 480
        }
    }

    func stop() {
        timer?.cancel()
        q.sync { stopped = true }
        fifo.stop()
    }
}
