"""Lazy loader for the optional ``psycopg`` PostgreSQL driver."""
from __future__ import annotations

import importlib
import sys
from typing import Any

POSTGRES_INSTALL_HINT = (
    "PostgreSQL Control Plane support requires the optional 'psycopg' driver. "
    "Install it with `pip install 'autophone[postgres]'` "
    "(or `uv sync --extra postgres` in a source checkout)."
)


class PostgresDriverUnavailable(ImportError):
    """Raised when PostgreSQL support is requested without ``psycopg`` installed."""


def require_psycopg() -> Any:
    """Import and return ``psycopg``, or fail closed with install instructions."""
    try:
        return importlib.import_module("psycopg")
    except ImportError as exc:
        missing = (getattr(exc, "name", None) or "").split(".", 1)[0]
        message = str(exc)
        if missing in {"psycopg", "psycopg_binary", "psycopg_c"} or (
            not missing and ("psycopg" in message or "pq wrapper" in message)
        ):
            raise PostgresDriverUnavailable(POSTGRES_INSTALL_HINT) from exc
        raise


def require_psycopg_components() -> tuple[Any, Any, Any]:
    """Return ``(psycopg, psycopg.sql, psycopg.rows.dict_row)`` lazily."""
    psycopg = require_psycopg()
    sql = importlib.import_module("psycopg.sql")
    rows = importlib.import_module("psycopg.rows")
    return psycopg, sql, rows.dict_row


def jsonb(value: Any) -> Any:
    """Wrap ``value`` in ``psycopg.types.json.Jsonb`` via the lazy driver loader."""
    require_psycopg()
    types_json = importlib.import_module("psycopg.types.json")
    return types_json.Jsonb(value)


def _loaded_psycopg() -> Any | None:
    return sys.modules.get("psycopg")


def _loaded_psycopg_errors() -> Any | None:
    errors_mod = sys.modules.get("psycopg.errors")
    if errors_mod is not None:
        return errors_mod
    pg = _loaded_psycopg()
    return getattr(pg, "errors", None) if pg is not None else None


def psycopg_error_types() -> tuple[type[BaseException], ...]:
    """Return ``(psycopg.Error,)`` if ``psycopg`` is already loaded, else ``()``."""
    pg = _loaded_psycopg()
    err = getattr(pg, "Error", None) if pg is not None else None
    return (err,) if isinstance(err, type) and issubclass(err, BaseException) else ()


def psycopg_errors_by_name(*names: str) -> tuple[type[BaseException], ...]:
    """Return matching exception classes from ``psycopg.errors`` if loaded, else ``()``."""
    errors_mod = _loaded_psycopg_errors()
    if errors_mod is None:
        return ()
    found: list[type[BaseException]] = []
    for name in names:
        cls = getattr(errors_mod, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)
