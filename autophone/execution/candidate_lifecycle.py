"""Optional installed candidate lifecycle providers for generic Run execution.

Domain execution owns this interface. Product activation and candidate Evidence
validation belong to installed providers, selected by distribution entry points.
Ordinary Runs do not require or load a provider.
"""
from contextlib import nullcontext
from importlib.metadata import entry_points
from typing import Protocol, Any

from autophone.device.runtime_probe import RuntimeAdmissionError


class CandidateLifecycle(Protocol):
    def assignment_scope(self, assignment, context, device, capabilities) -> Any: ...
    def activate_run(self, spec, profile, run_dir) -> Any: ...
    def reprobe(self, profile, run_dir, run_id) -> Any: ...
    def validate_result(self, run_dir, spec, activation, result) -> None: ...


def _provider() -> CandidateLifecycle:
    entries = list(entry_points(group='autophone.candidate_lifecycle'))
    if len(entries) != 1:
        raise RuntimeAdmissionError(['candidate_lifecycle_provider_unavailable'])
    return entries[0].load()()


def candidate_assignment_scope(assignment, context, device, capabilities):
    if (assignment.get('run_spec') or {}).get('candidate_ref') is None:
        return nullcontext()
    return _provider().assignment_scope(assignment, context, device, capabilities)


def activate_run_candidate(spec, profile, run_dir):
    if spec.get('candidate_ref') is None:
        return None
    return _provider().activate_run(spec, profile, run_dir)


def reprobe_run_candidate(profile, run_dir, run_id):
    return _provider().reprobe(profile, run_dir, run_id)


def validate_result(run_dir, spec, activation, result):
    if spec.get('candidate_ref') is not None:
        _provider().validate_result(run_dir, spec, activation, result)
