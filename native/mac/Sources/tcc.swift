import Foundation
import CoreGraphics

typealias TCCPreflightFn = @convention(c) (CFString, CFDictionary?) -> Int
typealias TCCRequestFn = @convention(c) (CFString, CFDictionary?, @escaping @convention(block) (Bool) -> Void) -> Void

func tccHandle() -> UnsafeMutableRawPointer? {
    return dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_NOW)
}

func audioPermission() -> String {
    guard let h = tccHandle(), let sym = dlsym(h, "TCCAccessPreflight") else { return "unknown" }
    let f = unsafeBitCast(sym, to: TCCPreflightFn.self)
    switch f("kTCCServiceAudioCapture" as CFString, nil) {
    case 0: return "granted"
    case 1: return "denied"
    default: return "unknown"
    }
}

func permissionFields() -> [String: Any] {
    return ["screen": CGPreflightScreenCaptureAccess(), "audio": audioPermission()]
}

/// ダイアログを出して最大 60 秒、画面収録の許可を待つ。ブロックするので呼び出し側でバックグラウンドに置くこと。
func requestPermissions() -> [String: Any] {
    let deadline = nowSec() + 60
    if !CGPreflightScreenCaptureAccess() { _ = CGRequestScreenCaptureAccess() }
    if audioPermission() != "granted", let h = tccHandle(), let sym = dlsym(h, "TCCAccessRequest") {
        let f = unsafeBitCast(sym, to: TCCRequestFn.self)
        let sem = DispatchSemaphore(value: 0)
        f("kTCCServiceAudioCapture" as CFString, nil) { _ in sem.signal() }
        _ = sem.wait(timeout: .now() + max(1, deadline - nowSec()))
    }
    while !CGPreflightScreenCaptureAccess() && nowSec() < deadline { Thread.sleep(forTimeInterval: 0.5) }
    return permissionFields()
}
