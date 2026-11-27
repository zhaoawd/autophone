"""Stage the optional region probe for a single Rig Agent process."""
from contextlib import ExitStack
from pathlib import Path
import shlex
import sys
import uuid

from autophone.rig.target import DeviceTarget
from autophone.device.shell import ShellError, run_shell
from autophone.device.springboard_residue import REGION_PROBE_ENV
from autophone.device.uds import ProtocolError, request


def stage_region_probe(stack: ExitStack, target: DeviceTarget, probe: Path) -> dict[str, str]:
    remote = f"/private/var/tmp/autophone-springboard-regions-{uuid.uuid4().hex}"
    transport = {"sock_path": str(target.socket), "protocol": target.protocol}

    def cleanup():
        try:
            result = run_shell(f"rm -f {shlex.quote(remote)}", timeout_ms=5000, **transport)
            if result.code != 0:
                raise ShellError(f"exit {result.code}")
        except (OSError, ShellError, ProtocolError) as exc:
            print(f"Rig {target.rig_id}: could not remove region probe {remote}: {exc}", file=sys.stderr)

    stack.callback(cleanup)
    result, _ = request(
        {"t": "file_put", "load": str(probe), "path": remote, "perm": "700", "screen": False},
        timeout=30, parse_image=False, **transport,
    )
    if result.get("ok") is not True:
        raise RuntimeError(f"Rig {target.rig_id}: region probe upload failed: {result.get('error')}")
    return {REGION_PROBE_ENV: remote}
