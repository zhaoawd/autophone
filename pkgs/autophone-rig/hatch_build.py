"""Load the shared build hook, also bundled in each component sdist."""
from pathlib import Path
_source = Path(__file__).with_name("build_support.py")
if not _source.is_file():
    _source = Path(__file__).resolve().parents[2] / "packaging_support.py"
exec(compile(_source.read_text(), str(_source), "exec"))
