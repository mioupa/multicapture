import importlib
import re
import sys
import unittest

MODULES = [
    "multicapture.config", "multicapture.ffmpeg", "multicapture.cdp", "multicapture.player",
    "multicapture.browser", "multicapture.browser_common", "multicapture.recorder", "multicapture.splitjob",
    "multicapture.platform", "multicapture.platform.mac", "multicapture.capacity",
]


class ImportTests(unittest.TestCase):
    def test_import_all(self):
        for name in MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)
        try:
            import tkinter  # noqa: F401
        except ImportError:
            pass
        else:
            importlib.import_module("multicapture.gui")

    def test_windows_package_not_loaded(self):
        for name in MODULES:
            importlib.import_module(name)
        loaded = [m for m in sys.modules if m.startswith("multicapture.platform.windows")]
        self.assertEqual(loaded, [] if sys.platform != "win32" else loaded)

    def test_contract_names(self):
        import multicapture.platform as osp
        from multicapture.platform import base

        doc = base.__doc__
        block = doc.split("Module-level names every implementation exports:")[1]
        names = set()
        for line in block.splitlines():
            m = re.match(r"\s{4}([A-Za-z_][A-Za-z_0-9, ]*?)(?:\(|\s{2,}|$)", line)
            if m and line.startswith("    ") and not line.startswith("     "):
                names.update(n.strip() for n in m.group(1).split(",") if n.strip())
        self.assertIn("clock", names)
        self.assertIn("SCHEDULE_SUPPORTED", names)
        self.assertGreater(len(names), 25)
        missing = [n for n in sorted(names) if not hasattr(osp, n)]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
