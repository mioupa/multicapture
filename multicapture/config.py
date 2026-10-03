import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def data_dir():
    portable = os.path.join(app_dir(), "data")
    if _writable(portable):
        return portable
    fallback = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MultiCapture")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def default_output_dir():
    return os.path.join(os.path.expanduser("~"), "Videos", "MultiCapture")


def new_slot_id():
    return uuid.uuid4().hex[:8]


@dataclass
class Slot:
    name: str = "dashboard"
    url: str = "https://example.com/"
    width: int = 1920
    height: int = 1080
    enabled: bool = True
    id: str = field(default_factory=new_slot_id)


@dataclass
class Settings:
    output_dir: str = field(default_factory=default_output_dir)
    browser: str = "edge"
    fps: int = 30
    encoder: str = "auto"
    keep_display_on: bool = False
    split_url: str = ""
    split_count: int = 4
    split_size: str = "1920x1080"
    split_pin_video: bool = True
    mute_speakers: bool = True
    slots: list = field(default_factory=lambda: [
        Slot("dashboard1", "https://example.com/"),
        Slot("dashboard2", "https://example.org/"),
    ])

    @property
    def path(self):
        return os.path.join(data_dir(), "config.json")

    @classmethod
    def load(cls):
        settings = cls()
        try:
            with open(settings.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return settings
        for key in ("output_dir", "browser", "encoder", "split_url", "split_size"):
            if isinstance(raw.get(key), str):
                setattr(settings, key, raw[key])
        for key in ("keep_display_on", "split_pin_video", "mute_speakers"):
            if isinstance(raw.get(key), bool):
                setattr(settings, key, raw[key])
        if isinstance(raw.get("split_count"), int):
            settings.split_count = max(1, min(8, raw["split_count"]))
        if isinstance(raw.get("fps"), int):
            settings.fps = max(1, min(60, raw["fps"]))
        if isinstance(raw.get("slots"), list):
            slots = []
            for item in raw["slots"]:
                if isinstance(item, dict):
                    known = {k: v for k, v in item.items() if k in Slot.__dataclass_fields__}
                    slots.append(Slot(**known))
            settings.slots = slots
        return settings

    def save(self):
        data = asdict(self)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


def profile_dir(slot):
    return os.path.join(data_dir(), "profiles", slot.id)


def login_profile_dir():
    return os.path.join(data_dir(), "profiles", "login")


def work_dir():
    path = os.path.join(data_dir(), "work")
    os.makedirs(path, exist_ok=True)
    return path


def log_dir():
    path = os.path.join(data_dir(), "logs")
    os.makedirs(path, exist_ok=True)
    return path
