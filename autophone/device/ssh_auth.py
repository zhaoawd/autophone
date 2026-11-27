"""Password input for OpenSSH without a pseudo-terminal or password arguments."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

_ASKPASS = '''#!/bin/sh
[ "${AUTOPHONE_SSH_PASSWORD+x}" = x ] || exit 1
printf '%s\\n' "$AUTOPHONE_SSH_PASSWORD"
'''


def guest_ssh_password(value: str | None = None) -> str:
    password = os.environ.get("AUTOPHONE_SSH_PASSWORD") if value is None else value
    if not password:
        raise ValueError("a non-empty guest SSH password is required (AUTOPHONE_SSH_PASSWORD)")
    return password


def run_password_command(
    command: Sequence[str], *, password: str,
    env: Mapping[str, str] | None = None, **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run OpenSSH with an askpass helper; leave stdin available for payloads.

    The helper contains no password and is removed after every outcome. A separate
    session and DISPLAY also support clients predating SSH_ASKPASS_REQUIRE.
    """
    child_env = dict(os.environ if env is None else env)
    with tempfile.TemporaryDirectory(prefix="autophone-ssh-askpass-") as directory:
        helper = Path(directory) / "askpass"
        helper.write_text(_ASKPASS, encoding="utf-8")
        helper.chmod(0o700)
        child_env.update(
            SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE="force",
            AUTOPHONE_SSH_PASSWORD=password,
        )
        child_env.setdefault("DISPLAY", "autophone:0")
        kwargs.setdefault("start_new_session", True)
        return subprocess.run(list(command), env=child_env, **kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-env", default="AUTOPHONE_SSH_PASSWORD")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("an OpenSSH command is required")
    if not os.environ.get(args.password_env):
        parser.error(f"{args.password_env} is unset or empty")
    result = run_password_command(args.command, password=os.environ[args.password_env])
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
