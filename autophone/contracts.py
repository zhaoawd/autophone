from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from referencing.exceptions import NoSuchResource

from autophone.util import JsonWriteError, write_json_atomic

# schemas 随包发布（autophone/_data/schemas/），只读、运行时从不写。
# 走包内相对路径（对齐 static/ 的取法），不依赖仓库根 ROOT，wheel/便携包里都成立。
_SCHEMA_DIR = Path(__file__).resolve().parent / "_data" / "schemas"


class ContractError(ValueError):
    pass


def _reject_non_finite_numbers(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        location = "/".join(path) or "<root>"
        raise ContractError(
            f"contract contains a non-finite JSON number at {location}",
        )
    if isinstance(value, dict):
        for key, nested in value.items():
            _reject_non_finite_numbers(nested, (*path, str(key)))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_non_finite_numbers(nested, (*path, str(index)))


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = _SCHEMA_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"schema not found: {path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _retrieve_schema(uri: str) -> Resource:
    """Resolve a cross-file ``$ref`` against the packaged schema directory.

    Schemas keep their existing self-declared ``$id`` spellings (some are bare
    filenames, some are ``autophone/schemas/...`` paths), so a relative ref is
    resolved by basename.  Anything not present on disk stays unresolvable —
    validation fails closed rather than silently skipping the subschema.
    """
    name = Path(uri).name
    if not name or name != uri.rsplit("/", 1)[-1]:
        raise NoSuchResource(ref=uri)
    try:
        schema = load_schema(name)
    except FileNotFoundError as exc:
        raise NoSuchResource(ref=uri) from exc
    return Resource.from_contents(schema, default_specification=DRAFT202012)


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    registry = Registry(retrieve=_retrieve_schema)
    return Draft202012Validator(load_schema(schema_name), registry=registry)


def validate_or_raise(schema_name: str, instance: dict[str, Any]) -> None:
    _reject_non_finite_numbers(instance)
    validator = _validator(schema_name)
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    if not errors:
        return
    detail = "\n".join(
        f"- {'/'.join(map(str, error.path)) or '<root>'}: {error.message}"
        for error in errors
    )
    raise ContractError(f"{schema_name} validation failed:\n{detail}")


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    _reject_non_finite_numbers(payload)
    try:
        return write_json_atomic(path, payload)
    except JsonWriteError as exc:
        raise ContractError(f"contract JSON write failed: {exc}") from exc
