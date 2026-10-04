import Foundation
import Darwin

/// PROTOCOL.md「映像：共有メモリのリング」の書き手（NV12 のみ）。
final class ShmRing {
    static let slotCount = 6
    static let headerSize = 64
    let width: Int, height: Int
    let slotSize: Int
    private let fd: Int32
    private let base: UnsafeMutableRawPointer
    private let totalSize: Int
    private let plane0Bytes: Int
    private let plane1Bytes: Int
    private var lastSeq: UInt64 = 0

    init(path: String, width: Int, height: Int) throws {
        self.width = width; self.height = height
        plane0Bytes = width * height
        plane1Bytes = width * ((height + 1) / 2)
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
        base.storeBytes(of: UInt32(0), toByteOffset: 24, as: UInt32.self)   // pixfmt 0 = nv12
        base.storeBytes(of: UInt32(width), toByteOffset: 28, as: UInt32.self)
        base.storeBytes(of: UInt32(height), toByteOffset: 32, as: UInt32.self)
        base.storeBytes(of: UInt64(0), toByteOffset: 40, as: UInt64.self)
        OSMemoryBarrier()
    }

    /// 直前に書いたコマと画素がまったく同じか。違う行が見つかった時点で打ち切る。
    func sameAsLatest(plane0: UnsafeRawPointer, stride0: Int, plane1: UnsafeRawPointer, stride1: Int) -> Bool {
        guard lastSeq > 0 else { return false }
        let d0 = base + ShmRing.headerSize + Int(lastSeq % UInt64(ShmRing.slotCount)) * slotSize + ShmRing.headerSize
        for y in 0..<height where memcmp(d0 + y * width, plane0 + y * stride0, width) != 0 { return false }
        let d1 = d0 + plane0Bytes
        for y in 0..<((height + 1) / 2) where memcmp(d1 + y * width, plane1 + y * stride1, width) != 0 { return false }
        return true
    }

    /// stride はソース側の bytesPerRow。plane0_off / plane1_off はスロット先頭からのバイト数。
    func write(seq: UInt64, pts: Double, plane0: UnsafeRawPointer, stride0: Int, plane1: UnsafeRawPointer, stride1: Int) {
        let slot = base + ShmRing.headerSize + Int(seq % UInt64(ShmRing.slotCount)) * slotSize
        let lockPtr = slot.assumingMemoryBound(to: UInt64.self)
        let cur = lockPtr.pointee
        lockPtr.pointee = cur &+ 1          // 奇数 = 書き込み中
        OSMemoryBarrier()
        slot.storeBytes(of: seq, toByteOffset: 8, as: UInt64.self)
        slot.storeBytes(of: pts, toByteOffset: 16, as: Double.self)
        slot.storeBytes(of: UInt32(width), toByteOffset: 24, as: UInt32.self)
        slot.storeBytes(of: UInt32(height), toByteOffset: 28, as: UInt32.self)
        slot.storeBytes(of: UInt32(width), toByteOffset: 32, as: UInt32.self)
        slot.storeBytes(of: UInt32(width), toByteOffset: 36, as: UInt32.self)
        slot.storeBytes(of: UInt32(ShmRing.headerSize), toByteOffset: 40, as: UInt32.self)
        slot.storeBytes(of: UInt32(ShmRing.headerSize + plane0Bytes), toByteOffset: 44, as: UInt32.self)
        slot.storeBytes(of: UInt32(plane0Bytes + plane1Bytes), toByteOffset: 48, as: UInt32.self)
        let d0 = slot + ShmRing.headerSize
        for y in 0..<height {
            memcpy(d0 + y * width, plane0 + y * stride0, width)
        }
        let d1 = d0 + plane0Bytes
        for y in 0..<((height + 1) / 2) {
            memcpy(d1 + y * width, plane1 + y * stride1, width)
        }
        OSMemoryBarrier()
        lockPtr.pointee = cur &+ 2          // 偶数 = 完了
        OSMemoryBarrier()
        base.storeBytes(of: seq, toByteOffset: 40, as: UInt64.self)
        OSMemoryBarrier()
        lastSeq = seq
    }

    func close() {
        munmap(base, totalSize)
        Darwin.close(fd)
    }
}
