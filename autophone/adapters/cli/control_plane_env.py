"""CLI 兼容入口；控制面环境解析由 Rig 层共用实现提供。"""
from autophone.rig.control_plane_env import (
    ControlPlaneEnv,
    control_plane_env_path,
    load_control_plane_env,
    read_control_plane_env,
)

__all__ = [
    "ControlPlaneEnv",
    "control_plane_env_path",
    "load_control_plane_env",
    "read_control_plane_env",
]
