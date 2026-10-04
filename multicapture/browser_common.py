import os
import shutil

RENDER_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-media-suspend",
    "--disable-sync",
    "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,msImplicitSignin",
    "--force-device-scale-factor=1",
]


PROFILE_SKIP = {
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache",
    "GraphiteDawnCache", "ShaderCache", "component_crx_cache", "extensions_crx_cache", "Crashpad", "BrowserMetrics",
    "CacheStorage", "ScriptCache", "Safe Browsing", "optimization_guide_model_store", "OptimizationHints",
    "lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort", "BrowserMetrics-spare.pma",
}


def clone_profile(src, dst):
    shutil.rmtree(dst, ignore_errors=True)
    if not os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        return
    shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names if n in PROFILE_SKIP], dirs_exist_ok=True,
                    ignore_dangling_symlinks=True)
