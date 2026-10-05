"""OS selection. See `base.py` for the interface every implementation provides."""
import sys

if sys.platform == "win32":
    from .windows import *  # noqa: F401,F403
elif sys.platform == "darwin":
    from .mac import *  # noqa: F401,F403
else:
    raise ImportError(f"MultiCapture does not support this OS ({sys.platform})")
