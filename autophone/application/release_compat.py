"""Read-only compatibility checks for staged Autophone releases."""

from __future__ import annotations

import importlib.metadata
import platform
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from autophone.contracts import validate_or_raise
from autophone.device.uds import command_inventory, get_capabilities

CompatibilityProbe = Callable[[], Any]

_PYTHON_CLAUSE = re.compile(r"^(>=|<=|==|>|<)([0-9]+(?:\.[0-9]+){1,2})$")
_FRIDA_VERSION = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")
_COMMIT_HEX = re.compile(r"[0-9a-f]{7,40}")


def _failure_status(level: str) -> str:
    return "block" if level == "apply" else "warn"


def _check(
    name: str,
    status: str,
    *,
    expected: Any = None,
    actual: Any = None,
    detail: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "status": status,
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }
    result.update(extra)
    return result


def _numeric_version(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(".")
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        raise ValueError(f"invalid numeric version: {value!r}")
    numbers = [int(part) for part in parts]
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def _python_matches(version: str, spec: str) -> bool:
    actual = _numeric_version(version)
    comparisons: dict[str, Callable[[tuple[int, ...], tuple[int, ...]], bool]] = {
        ">=": lambda left, right: left >= right,
        "<=": lambda left, right: left <= right,
        ">": lambda left, right: left > right,
        "<": lambda left, right: left < right,
        "==": lambda left, right: left == right,
    }
    clauses = spec.split(",")
    if not clauses:
        raise ValueError("empty Python version spec")
    for clause in clauses:
        match = _PYTHON_CLAUSE.fullmatch(clause)
        if match is None:
            raise ValueError(f"unsupported Python version clause: {clause!r}")
        operator, expected_text = match.groups()
        expected = _numeric_version(expected_text)
        if not comparisons[operator](actual, expected):
            return False
    return True


def _normalize_frida_version(value: Any) -> str:
    match = _FRIDA_VERSION.search(str(value).strip())
    if match is None:
        raise ValueError(f"Frida version not found in {value!r}")
    return match.group(1)


def _default_host_frida_probe() -> str:
    try:
        return importlib.metadata.version("frida")
    except importlib.metadata.PackageNotFoundError:
        try:
            import frida  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("host Frida package is not installed") from exc
        version = getattr(frida, "__version__", None)
        if not version:
            raise RuntimeError("host Frida package does not expose a version")
        return str(version)


_GUEST_FRIDA_PROBES = (
    "dpkg-query -W -f='${Version}\\n' re.frida.server 2>/dev/null",
    "/var/jb/usr/sbin/frida-server --version 2>/dev/null",
)


def _default_guest_frida_probe() -> str:
    """Read the guest frida-server version, trying each source in turn.

    A non-empty version decides whether a probe worked -- not its exit code.
    frida-server is commonly deployed by hand, which leaves dpkg holding an
    "install ok not-installed" record: dpkg-query then exits 0 and prints
    nothing, so an ``a || b`` chain would never reach the binary that can
    actually answer.
    """
    from autophone.device.shell import run_shell

    failures: list[str] = []
    for command in _GUEST_FRIDA_PROBES:
        source = command.split()[0]
        try:
            result = run_shell(command, timeout_ms=5_000, check=False)
        except Exception as exc:  # guest transport boundary
            failures.append(f"{source}: {type(exc).__name__}: {exc}")
            continue
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines:
            return lines[-1]
        failures.append(f"{source}: no version (exit {result.code})")
    raise RuntimeError("guest Frida probe returned no version: " + "; ".join(failures))


def _commands_from_manifest(compat: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    required = set(_string_sequence(compat.get("uds_commands")))
    optional = set(_string_sequence(compat.get("optional_uds_commands")))
    uds = compat.get("uds")
    if isinstance(uds, Mapping):
        required.update(_string_sequence(uds.get("required")))
        optional.update(_string_sequence(uds.get("optional")))
    optional.difference_update(required)
    return sorted(required), sorted(optional)


def _string_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item) for item in value]
    return []


def _available_commands(capabilities: Mapping[str, Any]) -> list[str] | None:
    # vphone-cli reports ``commands`` as {name: bool}; command_inventory keeps
    # only names whose value is ``true`` and still accepts a list of names.
    for key in ("commands", "available_commands", "uds_commands"):
        inventory = command_inventory(capabilities.get(key))
        if inventory is not None:
            return inventory

    for key in ("uds", "capabilities", "host"):
        nested = capabilities.get(key)
        if not isinstance(nested, Mapping):
            continue
        for command_key in ("commands", "available_commands", "uds_commands"):
            inventory = command_inventory(nested.get(command_key))
            if inventory is not None:
                return inventory
    return None


def _vphone_commit(capabilities: Mapping[str, Any]) -> str | None:
    for key in ("vphone_cli_commit", "build_commit", "commit"):
        value = capabilities.get(key)
        if isinstance(value, str) and value:
            return value

    for key in ("vphone_cli", "host", "build"):
        nested = capabilities.get(key)
        if not isinstance(nested, Mapping):
            continue
        for commit_key in ("commit", "build_commit"):
            value = nested.get(commit_key)
            if isinstance(value, str) and value:
                return value
    return None


def _same_commit(actual: str, expected: str) -> bool:
    """Compare two git commits that may be abbreviated to different lengths.

    Release manifests record an abbreviation (``30d1f47``) while a VM is free to
    report the full SHA, so equality alone would never match.  The shorter value
    must be a prefix of the longer one; this is identity, not ordering, and it
    never infers that one commit precedes another.
    """
    left = str(actual).strip().lower()
    right = str(expected).strip().lower()
    if not left or not right:
        return False
    if not _COMMIT_HEX.fullmatch(left) or not _COMMIT_HEX.fullmatch(right):
        return left == right
    shorter, longer = sorted((left, right), key=len)
    return longer.startswith(shorter)


def _probe_error(exc: Exception) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def check_release_compatibility(
    manifest: Mapping[str, Any],
    *,
    state_schema: int,
    level: str = "apply",
    python_version: str | None = None,
    python_version_probe: CompatibilityProbe | None = None,
    python_source: str | None = None,
    capabilities_probe: CompatibilityProbe | None = None,
    host_frida_probe: CompatibilityProbe | None = None,
    guest_frida_probe: CompatibilityProbe | None = None,
) -> dict[str, Any]:
    """Evaluate one release manifest without changing host or guest state.

    ``stage`` reports incompatible observations as warnings. ``apply`` turns
    required failures into blockers; a confirmed offline VM remains a warning
    and skips guest checks. Every environmental read has an injectable seam so
    callers and tests can run without a VM or a Frida installation.
    """
    if level not in {"stage", "apply"}:
        raise ValueError(f"unsupported compatibility level: {level!r}")

    payload = dict(manifest)
    validate_or_raise("release-manifest.v1.json", payload)
    compat = payload["compat"]
    expected_python = str(compat["python"])
    expected_frida = str(compat["frida"])
    required_commands, optional_commands = _commands_from_manifest(compat)
    failure = _failure_status(level)
    checks: list[dict[str, Any]] = []

    try:
        if python_version is not None:
            actual_python = str(python_version)
        elif python_version_probe is not None:
            actual_python = str(python_version_probe())
        else:
            actual_python = platform.python_version()
        python_ok = _python_matches(actual_python, expected_python)
    except Exception as exc:  # probe boundary: return a structured blocker
        checks.append(
            _check(
                "python",
                failure,
                expected=expected_python,
                actual=locals().get("actual_python"),
                detail=_probe_error(exc),
                source=python_source or (
                    "staged" if python_version_probe is not None else "manager"
                ),
            )
        )
    else:
        checks.append(
            _check(
                "python",
                "pass" if python_ok else failure,
                expected=expected_python,
                actual=actual_python,
                detail=None if python_ok else "Python version is outside the release spec",
                source=python_source or (
                    "staged" if python_version_probe is not None else "manager"
                ),
            )
        )

    target_state_schema = int(payload["state_schema"])
    state_valid = (
        isinstance(state_schema, int)
        and not isinstance(state_schema, bool)
        and state_schema >= 0
    )
    state_ok = state_valid and state_schema <= target_state_schema
    checks.append(
        _check(
            "state_schema",
            "pass" if state_ok else failure,
            expected={"maximum": target_state_schema},
            actual=state_schema,
            detail=(
                None
                if state_ok
                else "local state schema is invalid or newer than the release schema"
            ),
        )
    )

    host_probe = host_frida_probe or _default_host_frida_probe
    try:
        host_raw = host_probe()
        host_version = _normalize_frida_version(host_raw)
    except Exception as exc:  # probe boundary: return a structured blocker
        checks.append(
            _check(
                "frida.host",
                failure,
                expected=expected_frida,
                actual=None,
                detail=_probe_error(exc),
            )
        )
    else:
        host_ok = host_version == expected_frida
        checks.append(
            _check(
                "frida.host",
                "pass" if host_ok else failure,
                expected=expected_frida,
                actual=host_version,
                detail=None if host_ok else "host Frida version does not match",
                observed=str(host_raw),
            )
        )

    capability_reader = capabilities_probe or get_capabilities
    capabilities: Mapping[str, Any] | None = None
    vm_online: bool | None
    capability_state: str
    vm_reason: str | None
    try:
        observed_capabilities = capability_reader()
    except (OSError, TimeoutError) as exc:
        vm_online = False
        capability_state = "unavailable"
        vm_reason = _probe_error(exc)
    except Exception as exc:  # protocol and injected probe boundary
        vm_online = None
        capability_state = "probe_error"
        vm_reason = _probe_error(exc)
    else:
        if isinstance(observed_capabilities, Mapping):
            capabilities = observed_capabilities
            if observed_capabilities.get("available") is True:
                vm_online = True
                capability_state = "available"
                vm_reason = None
            else:
                capability_state = str(
                    observed_capabilities.get("reason") or "unavailable"
                )
                error = observed_capabilities.get("error")
                vm_reason = str(error) if error else None
                vm_online = True if capability_state == "unsupported" else None
        else:
            vm_online = None
            capability_state = "invalid_response"
            vm_reason = (
                "capabilities probe returned "
                f"{type(observed_capabilities).__name__}, expected a mapping"
            )

    vm_ok = vm_online is True
    vm_status = "pass" if vm_ok else ("warn" if vm_online is False else failure)
    checks.append(
        _check(
            "vm",
            vm_status,
            expected="online",
            actual=vm_online,
            detail=vm_reason,
        )
    )
    vm = {
        "online": vm_online,
        "capabilities": capability_state,
        "reason": vm_reason,
    }

    command_inventory = (
        _available_commands(capabilities)
        if capabilities is not None and capability_state == "available"
        else None
    )
    if vm_online is not True:
        checks.extend(
            [
                _check(
                    "uds.required",
                    "skip",
                    expected=required_commands,
                    actual=None,
                    detail="VM is offline; required UDS commands were not probed",
                    missing=None,
                ),
                _check(
                    "uds.optional",
                    "skip",
                    expected=optional_commands,
                    actual=None,
                    detail="VM is offline; optional UDS commands were not probed",
                    missing=None,
                ),
            ]
        )
    elif command_inventory is None:
        checks.extend(
            [
                _check(
                    "uds.required",
                    failure,
                    expected=required_commands,
                    actual=None,
                    detail="UDS capability inventory is unavailable",
                    missing=None,
                ),
                _check(
                    "uds.optional",
                    "skip",
                    expected=optional_commands,
                    actual=None,
                    detail="UDS capability inventory is unavailable",
                    missing=None,
                ),
            ]
        )
    else:
        available = set(command_inventory)
        missing_required = sorted(set(required_commands) - available)
        missing_optional = sorted(set(optional_commands) - available)
        checks.extend(
            [
                _check(
                    "uds.required",
                    failure if missing_required else "pass",
                    expected=required_commands,
                    actual=command_inventory,
                    detail=(
                        "required UDS commands are missing" if missing_required else None
                    ),
                    missing=missing_required,
                ),
                _check(
                    "uds.optional",
                    "warn" if missing_optional else "pass",
                    expected=optional_commands,
                    actual=command_inventory,
                    detail=(
                        "optional UDS commands are missing" if missing_optional else None
                    ),
                    missing=missing_optional,
                ),
            ]
        )

    if vm_online is not True:
        checks.append(
            _check(
                "frida.guest",
                "skip",
                expected=expected_frida,
                actual=None,
                detail="VM is offline; guest Frida was not probed",
            )
        )
    else:
        guest_probe = guest_frida_probe or _default_guest_frida_probe
        try:
            guest_raw = guest_probe()
            guest_version = _normalize_frida_version(guest_raw)
        except Exception as exc:  # guest read boundary
            checks.append(
                _check(
                    "frida.guest",
                    failure,
                    expected=expected_frida,
                    actual=None,
                    detail=_probe_error(exc),
                )
            )
        else:
            guest_ok = guest_version == expected_frida
            checks.append(
                _check(
                    "frida.guest",
                    "pass" if guest_ok else failure,
                    expected=expected_frida,
                    actual=guest_version,
                    detail=None if guest_ok else "guest Frida version does not match",
                    observed=str(guest_raw),
                )
            )

    expected_commit = compat.get("vphone_cli_commit") or compat.get(
        "vphone_cli_min_commit"
    )
    actual_commit = _vphone_commit(capabilities) if capabilities is not None else None
    if expected_commit is None:
        commit_status = "skip"
        commit_detail = "release does not declare a tested vphone-cli commit"
    elif actual_commit is None:
        commit_status = "skip"
        commit_detail = "vphone-cli commit is unavailable"
    elif _same_commit(actual_commit, expected_commit):
        commit_status = "pass"
        commit_detail = None
    else:
        commit_status = "warn"
        commit_detail = "vphone-cli commit differs from the tested commit"
    checks.append(
        _check(
            "vphone_cli.commit",
            commit_status,
            expected=expected_commit,
            actual=actual_commit,
            detail=commit_detail,
            diagnostic_only=True,
        )
    )

    blockers = [check["name"] for check in checks if check["status"] == "block"]
    warnings = [check["name"] for check in checks if check["status"] == "warn"]
    status = "blocked" if blockers else "warning" if warnings else "passed"
    return {
        "schema_version": "v1",
        "level": level,
        "ok": not blockers,
        "status": status,
        "release": {
            "version": payload["version"],
            "commit": payload["commit"],
            "state_schema": target_state_schema,
        },
        "vm": vm,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }


# Short alias for callers that already operate in a release-scoped module.
check_compatibility = check_release_compatibility

__all__ = ["check_compatibility", "check_release_compatibility"]
