import Foundation
import Darwin

// MARK: TCC responsibility
// ターミナルや SSH から起動すると TCC は親（ターミナル）の許可を見る。
// 自分を「責任プロセス」にして起動し直すと、補助プログラムのバンドル自身の許可で判定される。

var gChild: pid_t = 0

func relaunchDisclaimed() -> Never {
    typealias Disclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    guard let sym = dlsym(dlopen(nil, RTLD_NOW), "responsibility_spawnattrs_setdisclaim") else {
        fputs("mc-capture: responsibility_spawnattrs_setdisclaim not found\n", stderr); exit(1)
    }
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    _ = unsafeBitCast(sym, to: Disclaim.self)(&attr, 1)
    setenv("MC_CAPTURE_DISCLAIMED", "1", 1)
    let path = Bundle.main.executablePath ?? CommandLine.arguments[0]
    var cargs: [UnsafeMutablePointer<CChar>?] = CommandLine.arguments.map { strdup($0) } + [nil]
    let rc = posix_spawn(&gChild, path, nil, &attr, &cargs, environ)
    posix_spawnattr_destroy(&attr)
    if rc != 0 { fputs("mc-capture: disclaimed spawn failed: \(rc)\n", stderr); exit(1) }
    for s in [SIGINT, SIGTERM, SIGHUP] { signal(s) { sig in kill(gChild, sig) } }
    var status: Int32 = 0
    while waitpid(gChild, &status, 0) < 0 && errno == EINTR {}
    if status & 0x7f == 0 { exit((status >> 8) & 0xff) }
    exit(128 + (status & 0x7f))
}

// MARK: entry

let args = Array(CommandLine.arguments.dropFirst())
let wantDisclaim = args.contains("--disclaim")
let positional = args.filter { $0 != "--disclaim" }
guard positional == ["serve"] else {
    fputs("usage: mc-capture [--disclaim] serve\n", stderr)
    exit(2)
}
let isDisclaimed = ProcessInfo.processInfo.environment["MC_CAPTURE_DISCLAIMED"] != nil
if wantDisclaim && !isDisclaimed { relaunchDisclaimed() }

signal(SIGPIPE, SIG_IGN)
let gServer = Server(disclaimed: isDisclaimed)
gServer.run()
dispatchMain()
