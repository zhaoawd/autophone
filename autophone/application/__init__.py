"""Lazy compatibility exports; importing a protocol does not load application services."""
from importlib import import_module

_EXPORTS = {'AiSceneApplication': 'ai_scenes', 'AiSceneApplicationError': 'ai_scenes', 'CaseImportApplication': 'cases', 'CaseImportApplicationError': 'cases', 'CaseLifecycleApplication': 'cases', 'CaseLifecycleApplicationError': 'cases', 'CaseRecipeApplication': 'cases', 'CaseRecipeApplicationError': 'cases', 'DeviceApplication': 'device', 'DeviceApplicationError': 'device', 'ForegroundApplication': 'device', 'ForegroundApplicationError': 'device', 'LocationApplicationError': 'location', 'UnifiedLocationApplication': 'location', 'OperationApplication': 'operations', 'OperationApplicationError': 'operations', 'PackageInstallApplication': 'package_install', 'UploadApplication': 'uploads', 'UploadApplicationError': 'uploads'}
__all__ = list(_EXPORTS)

def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module("." + _EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
