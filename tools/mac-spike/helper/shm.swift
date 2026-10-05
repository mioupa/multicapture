import Foundation
import Darwin

/// README「共有メモリのリング」の書き手。
final class ShmRing {
    static let slotCount = 6
    static let headerSize = 64
    let width: Int, height: Int, pixFmt: Int
    let slotSize: Int
    private let fd: Int32
    private let base: UnsafeMutableRawPointer
    private let totalSize: Int
    private let plane0Bytes: Int
    private let plane1Bytes: Int

    init(path: String, pixFmt: Int, width: Int, height: Int) throws {
        self.width = width; self.height = height; self.pixFmt = pixFmt
        if pixFmt == 0 {
            plane0Bytes = width * height
            plane1Bytes = width * ((height + 1) / 2)
        } else {
            plane0Bytes = width * height * 4
            plane1Bytes = 0
        }
        let dataBytes = plane0Bytes + plane1Bytes
        slotSize = (ShmRing.headerSize + dataBytes + 63) / 64 * 64
        totalSize = ShmRing.headerSize + slotSize * ShmRing.slotCount
        fd = open(path, O_CREAT | O_RDWR | O_TRUNC, 0o600)
        if fd < 0 { throw NSError(domain: "shm", code: Int(errno), userInfo: [NSLocalizedDescriptionKey: "open failed: \(String(cString: strerror(errno)))"]) }
        if ftruncate(fd, off_t(totalSize)) != 0 {
            let e = errno; Darwin.close(fd)
            throw NSError(domain: "shm", code: Int(e), userInfo: [NSLocalizedDescriptionKey: "ftruncate failed: \(String(cString: strerror(e)))"])
        }
        guard let m = mmap(nil, totalSize, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0), m != MAP_FAILED else {
            let e = errno; Darwin.close(fd)
            throw NSError(domain: "shm", code: Int(e), userInfo: [NSLocalizedDescriptionKey: "mmap failed: \(String(cString: strerror(e)))"])
        }
        base = m
        memset(base, 0, ShmRing.headerSize)
        let magic: [UInt8] = Array("MCRING01".utf8)
        for (i, b) in magic.enumerated() { base.storeBytes(of: b, toByteOffset: i, as: UInt8.self) }
        base.storeBytes(of: UInt32(1), toByteOffset: 8, as: UInt32.self)
        base.storeBytes(of: UInt32(ShmRing.slotCount), toByteOffset: 12, as: UInt32.self)
        base.storeBytes(of: UInt32(slotSize), toByteOffset: 16, as: UInt32.self)
        base.storeBytes(of: UInt32(ShmRing.headerSize), toByteOffset: 20, as: UInt32.self)
        base.storeBytes(of: UInt32(pixFmt), toByteOffset: 24, as: UInt32.self)
        base.storeBytes(of: UInt32(width), toByteOffset: 28, as: UInt32.self)
        base.storeBytes(of: UInt32(height), toByteOffset: 32, as: UInt32.self)
        base.storeBytes(of: UInt64(0), toByteOffset: 40, as: UInt64.self)
        OSMemoryBarrier()
    }

    /// plane1 は nv12 のときだけ使う。stride はソース側の bytesPerRow。
    /// plane0_off / plane1_off はスロット先頭（スロットヘッダーの先頭）からのバイト数。
    func write(seq: UInt64, pts: Double, plane0: UnsafeRawPointer, stride0: Int, plane1: UnsafeRawPointer?, stride1: Int) {
        let slot = base + ShmRing.headerSize + Int(seq % UInt64(ShmRing.slotCount)) * slotSize
        let lockPtr = slot.assumingMemoryBound(to: UInt64.self)
        let cur = lockPtr.pointee
        lockPtr.pointee = cur &+ 1          // 奇数
        OSMemoryBarrier()
        slot.storeBytes(of: seq, toByteOffset: 8, as: UInt64.self)
        slot.storeBytes(of: pts, toByteOffset: 16, as: Double.self)
        slot.storeBytes(of: UInt32(width), toByteOffset: 24, as: UInt32.self)
        slot.storeBytes(of: UInt32(height), toByteOffset: 28, as: UInt32.self)
        let dstStride0 = pixFmt == 0 ? width : width * 4
        let dstStride1 = pixFmt == 0 ? width : 0
        slot.storeBytes(of: UInt32(dstStride0), toByteOffset: 32, as: UInt32.self)
        slot.storeBytes(of: UInt32(dstStride1), toByteOffset: 36, as: UInt32.self)
        slot.storeBytes(of: UInt32(ShmRing.headerSize), toByteOffset: 40, as: UInt32.self)
        slot.storeBytes(of: UInt32(pixFmt == 0 ? ShmRing.headerSize + plane0Bytes : 0), toByteOffset: 44, as: UInt32.self)
        slot.storeBytes(of: UInt32(plane0Bytes + plane1Bytes), toByteOffset: 48, as: UInt32.self)
        let d0 = slot + ShmRing.headerSize
        for y in 0..<height {
            memcpy(d0 + y * dstStride0, plane0 + y * stride0, dstStride0)
        }
        if pixFmt == 0, let p1 = plane1 {
            let d1 = d0 + plane0Bytes
            for y in 0..<((height + 1) / 2) {
                memcpy(d1 + y * dstStride1, p1 + y * stride1, dstStride1)
            }
        }
        OSMemoryBarrier()
        lockPtr.pointee = cur &+ 2          // 偶数
        OSMemoryBarrier()
        base.storeBytes(of: seq, toByteOffset: 40, as: UInt64.self)
        OSMemoryBarrier()
    }

    func close() {
        munmap(base, totalSize)
        Darwin.close(fd)
    }
}

/// 許可なしで共有メモリを試すための合成 nv12 書き込み。
final class ShmSelftest {
    private let ring: ShmRing
    private let w: Int, h: Int, fps: Double
    private let q = DispatchQueue(label: "mc.selftest")
    private var timer: DispatchSourceTimer?
    private var buf: UnsafeMutableRawPointer
    private var seq: UInt64 = 0
    private var stopped = false

    init(ring: ShmRing, w: Int, h: Int, fps: Double) {
        self.ring = ring; self.w = w; self.h = h; self.fps = fps
        buf = UnsafeMutableRawPointer.allocate(byteCount: w * h * 3 / 2, alignment: 64)
    }

    func start() {
        let t = DispatchSource.makeTimerSource(queue: q)
        t.schedule(deadline: .now(), repeating: 1.0 / fps, leeway: .milliseconds(1))
        t.setEventHandler { [weak self] in self?.tick() }
        timer = t
        t.resume()
    }

    private func tick() {
        if stopped { return }
        seq += 1
        memset(buf, Int32(seq % 256), w * h)
        memset(buf + w * h, 128, w * h / 2)
        ring.write(seq: seq, pts: nowSec(), plane0: buf, stride0: w, plane1: buf + w * h, stride1: w)
        gStats.update { $0.complete += 1 }
    }

    func stop() {
        timer?.cancel()
        q.sync { stopped = true }
        ring.close()
    }
}
