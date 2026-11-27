"""Autophone platform package."""

from autophone import _source_digest  # noqa: F401 - capture the process source baseline first
from autophone.version import get_version

__all__ = ["__version__"]

__version__ = get_version()
