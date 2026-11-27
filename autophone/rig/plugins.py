"""Installed execution providers; Rig transport has no dependency on their implementations."""
from importlib.metadata import entry_points


def load_run_handlers():
    handlers = {}
    for entry in sorted(entry_points(group="autophone.run_handlers"), key=lambda e: e.name):
        for name, handler in entry.load()().items():
            if name in handlers:
                raise ValueError(f"duplicate Run handler: {name}")
            handlers[name] = handler
    return handlers


def _provider(group: str, **kwargs):
    entries = list(entry_points(group=group))
    if len(entries) != 1:
        raise RuntimeError(f"{group} requires exactly one installed provider; install autophone-domain or supply an explicit factory")
    return entries[0].load()(**kwargs)


def create_executor(**kwargs):
    return _provider("autophone.executor", **kwargs)


def create_device_session_ops(**kwargs):
    return _provider("autophone.device_session", **kwargs)


def validate_run_submission(spec):
    """Invoke installed admission providers without importing domain implementations."""
    from autophone.contracts import ContractError

    selector = spec.get("case_selector") or {}
    if not isinstance(selector, dict):
        raise ContractError("trial_confirmation_selector_invalid: case_selector must be an object")
    if not (spec.get("trial_confirmation") is not None or (
        spec.get("type") == "regression" and (
            "draft" in (selector.get("statuses") or []) or selector.get("trial_draft_recipes")
        )
    )):
        return
    entries = list(entry_points(group="autophone.run_admission"))
    if len(entries) != 1:
        raise ContractError("trial_confirmation_validator_unavailable: install one run admission provider")
    entries[0].load()(spec)
