"""Registration hooks provided by installed optional distributions."""
from importlib.metadata import entry_points
import os


def register_extensions(name, *args):
    if os.environ.get("AUTOPHONE_DISABLE_EXTENSIONS", "").strip().lower() in {"1", "true", "yes"}:
        return
    for extension in entry_points(group="autophone.console_extensions", name=name):
        extension.load()(*args)
