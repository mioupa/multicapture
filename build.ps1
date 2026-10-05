param(
    [string]$Python = "python",
    [string]$FFmpeg = "",
    [switch]$DownloadFFmpeg
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$build = Join-Path $root ".build"
$dist = Join-Path $root "dist"
$app = Join-Path $dist "MultiCapture"
$version = (Select-String -Path (Join-Path $root "multicapture\__init__.py") -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value

New-Item -ItemType Directory -Force $build | Out-Null
$venv = Join-Path $build "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $Python -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip pyinstaller

if ($DownloadFFmpeg -and -not $FFmpeg) {
    $zip = Join-Path $build "ffmpeg-essentials.zip"
    if (-not (Test-Path $zip)) {
        Invoke-WebRequest "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip
    }
    $extract = Join-Path $build "ffmpeg-essentials"
    if (-not (Test-Path $extract)) { Expand-Archive $zip $extract }
    $FFmpeg = (Get-ChildItem $extract -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName
}
if (-not $FFmpeg) {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) {
        $item = Get-Item $cmd.Source
        $FFmpeg = if ($item.Target) { $item.Target } else { $item.FullName }
        # A Scoop shim only works next to its .shim file, so copy the real executable it points to.
        $shim = [IO.Path]::ChangeExtension($FFmpeg, ".shim")
        if (Test-Path $shim) {
            $line = Select-String -Path $shim -Pattern '^\s*path\s*=\s*"?([^"]+?)"?\s*$' | Select-Object -First 1
            if ($line) { $FFmpeg = $line.Matches[0].Groups[1].Value }
        }
    }
}
if (-not $FFmpeg -or -not (Test-Path $FFmpeg)) {
    throw "ffmpeg.exe not found. Pass -FFmpeg <path> or -DownloadFFmpeg."
}

if (Test-Path $app) { Remove-Item $app -Recurse -Force }
& $py -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name MultiCapture `
    --distpath $dist --workpath (Join-Path $build "work") --specpath $build `
    (Join-Path $root "MultiCapture.pyw")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$ffdir = Join-Path $app "ffmpeg"
New-Item -ItemType Directory -Force $ffdir | Out-Null
Copy-Item $FFmpeg $ffdir
$ffroot = Split-Path (Split-Path $FFmpeg)
foreach ($f in "LICENSE", "LICENSE.txt", "README.txt") {
    $p = Join-Path $ffroot $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $ffdir "FFMPEG_$f") }
}
Copy-Item (Join-Path $root "README.md") $app

$zipOut = Join-Path $dist "MultiCapture-$version-win64.zip"
if (Test-Path $zipOut) { Remove-Item $zipOut }
Compress-Archive -Path $app -DestinationPath $zipOut -CompressionLevel Optimal
"Built: $zipOut ({0:N1} MB)" -f ((Get-Item $zipOut).Length / 1MB)
