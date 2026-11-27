"""ssh_bringup — host-side SSH bring-up for the vphone VM.

Why this lives on the host (not a guest LaunchDaemon):
  Boot launchd only auto-loads the read-only /System/Library/LaunchDaemons
  (dropbear + com.vphone.jb-setup). We can't add there. And the UDS shell
  runs in a non-system session, so `launchctl bootstrap system <plist>`
  fails with "cannot load in requested session" — nor can `launchctl list`
  in that session even SEE system-domain daemons. So sshd lifecycle can't
  be reliably managed from inside the guest; the host, which already
  orchestrates VM startup and owns the UDS shell channel (vphone.sock,
  works WITHOUT ssh), does it instead.

Guest :22 states observed across rigs (each VM image differs):
  a. launchd's socket-activated com.openssh.sshd holds :22/:2222 and its
     spawned sshd ACCEPTS password logins  → nothing to do;
  b. same, but logins are REJECTED (Permission denied on password) — and we
     can't unload it (session limits above), so :22 is unusable and a
     standalone sshd can't bind it either  → run standalone sshd on
     ALT_GUEST_PORT and point the host tunnel there;
  c. nothing usable on :22 (fresh image)  → start standalone sshd on :22.
  d. OpenSSH is not installed, but the image's native dropbear is usable on
     :22222 with persistent host keys → tunnel directly to dropbear.

So the strategy is probe-by-real-login: netstat/ps can't distinguish a usable
SSH server from one that rejects auth. The only trustworthy signal is an
actual password login through the tunnel.

What it does (idempotent):
  1. probe the existing SSH endpoints (:22, then native dropbear :22222);
  2. if neither works, ensure OpenSSH host keys exist in /var/jb/etc/ssh
     (persisted on
     /private/preboot, survives reboot — generated only if missing);
  3. start `iproxy [-u UDID] 2222:<guest port>` on the host;
  4. real-login probe; if it fails, start standalone sshd on :22, and if
     that can't come up (launchd holds the port), fall back to
     ALT_GUEST_PORT + re-point iproxy;
  5. verify a real ssh login over the tunnel (fail-closed).

Every login round-trip in steps 1/4/5 is retried (LOGIN_ATTEMPTS) before its
result is believed: right after boot the guest transiently denies password
auth, so a single denial can't tell a flake from a truly unusable endpoint.

Usage:
    python3 -m tools.phase1.ssh_bringup            # bring up + verify
    python3 -m tools.phase1.ssh_bringup --no-verify

Dropbear note: older images ran it with `-R` against a read-only default key
directory and died mid-KEX. Newer images pass persistent keys under
/var/dropbear, so only a real-login probe can decide whether it is usable.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

from autophone.device.ssh_auth import guest_ssh_password, run_password_command
from autophone.device.shell import run_shell, run_shell_bg

SSH_DIR = "/var/jb/etc/ssh"
SSHD = "/var/jb/usr/sbin/sshd"
KEYGEN = "/var/jb/usr/bin/ssh-keygen"
GUEST_PORT = 22
DROPBEAR_GUEST_PORT = 22222
# Fallback when launchd's socket-activated sshd holds :22 (and :2222) but
# rejects logins: we can't free the port from the UDS shell session, so we
# run our own standalone sshd here and tunnel to it instead.
ALT_GUEST_PORT = 22022
HOST_PORT = 2222
# Password auth is transiently rejected right after boot: a login probe can
# pass and the very next login be denied (observed 2026-09-03 — the dropbear
# probe succeeded, verify_login was denied 2 s later, and the same command
# succeeded on every retry afterwards). So each login round-trip is retried
# before its result is believed. This does not weaken fail-closed: a login
# that keeps failing still raises — retries only stop a single flake from
# deciding the outcome.
LOGIN_ATTEMPTS = 3
LOGIN_RETRY_DELAY_S = 1.0
EXPECTED_UDID = ""
IPROXY_PID_FILE: Path | None = None
REMOTE_PATH = (
    "/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:"
    "/iosbinpack64/bin:/iosbinpack64/usr/local/bin:"
    "/usr/bin:/bin:/usr/sbin:/sbin"
)


def _log(msg: str) -> None:
    print(f"[ssh_bringup] {msg}", flush=True)


def ensure_host_keys() -> None:
    """Generate OpenSSH host keys in the guest if missing (idempotent)."""
    r = run_shell(f"{SSHD} -t 2>&1; echo RC=$?")
    if "RC=0" in r.stdout:
        _log("host keys present (sshd -t ok)")
        return
    _log("no host keys — generating rsa/ecdsa/ed25519")
    for t in ("rsa", "ecdsa", "ed25519"):
        run_shell(
            f'test -f {SSH_DIR}/ssh_host_{t}_key || '
            f'{KEYGEN} -t {t} -f {SSH_DIR}/ssh_host_{t}_key -N "" ',
            timeout_ms=60_000,
        )
    r = run_shell(f"{SSHD} -t 2>&1; echo RC=$?")
    if "RC=0" not in r.stdout:
        raise RuntimeError(f"sshd config still invalid after keygen: {r.stdout!r}")
    _log("host keys generated, sshd -t ok")


def _listening_on(port: int) -> bool:
    r = run_shell(
        f"netstat -an 2>/dev/null | grep LISTEN | grep '\\.{port} ' || true")
    return f".{port} " in r.stdout


def _sshd_listening() -> bool:
    return _listening_on(GUEST_PORT)


def _standalone_sshd_listening() -> bool:
    r = run_shell("ps aux | grep 'sshd.*listener' | grep -v grep || true")
    return SSHD in r.stdout and "[listener]" in r.stdout


def start_sshd() -> None:
    """Start a standalone OpenSSH sshd in the guest on :22.

    Tries to unload launchd's socket-activated sshd first (best effort —
    the UDS shell session usually can't see system-domain daemons at all,
    in which case the port stays held and the standalone bind fails; the
    caller falls back to ALT_GUEST_PORT).
    """
    # First, unload launchd's sshd if it's visible (freeing :22 and :2222)
    r = run_shell("launchctl list | grep com.openssh.sshd || true")
    if "com.openssh.sshd" in r.stdout:
        _log("unloading launchd com.openssh.sshd (inetd mode)")
        run_shell("launchctl unload /var/jb/Library/LaunchDaemons/com.openssh.sshd.plist 2>&1 || true")
        time.sleep(0.5)

    if _standalone_sshd_listening() and _sshd_listening():
        _log("standalone sshd already running on guest :22")
        return

    _log("starting standalone sshd daemon")
    # sshd daemonizes itself; run_shell_bg guards the pipe just in case.
    run_shell_bg(SSHD)
    for _ in range(10):
        time.sleep(0.5)
        if _standalone_sshd_listening() and _sshd_listening():
            _log("sshd is up on guest :22")
            return
    raise RuntimeError("standalone sshd did not come up on guest :22")


def start_sshd_fallback_port() -> None:
    """Run standalone sshd on ALT_GUEST_PORT and re-point the host tunnel.

    Used when :22 is unusable: held by launchd's socket-activated sshd that
    rejects logins, and un-loadable from the UDS shell session.
    """
    if not _listening_on(ALT_GUEST_PORT):
        _log(f"starting standalone sshd on fallback port :{ALT_GUEST_PORT}")
        run_shell_bg(f"{SSHD} -p {ALT_GUEST_PORT}")
        for _ in range(10):
            time.sleep(0.5)
            if _listening_on(ALT_GUEST_PORT):
                break
        else:
            raise RuntimeError(
                f"standalone sshd did not come up on guest :{ALT_GUEST_PORT}")
    else:
        _log(f"sshd already listening on fallback port :{ALT_GUEST_PORT}")
    start_iproxy(ALT_GUEST_PORT)


def _iproxy_command(guest_port: int) -> list[str]:
    command = ["iproxy"]
    if EXPECTED_UDID:
        command.extend(["-u", EXPECTED_UDID])
    command.append(f"{HOST_PORT}:{guest_port}")
    return command


def _iproxy_processes() -> tuple[tuple[int, str, int, int], ...]:
    out = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return ()
    processes: list[tuple[int, str, int, int]] = []
    for line in out.stdout.splitlines():
        pid_text, separator, command = line.strip().partition(" ")
        if not separator or not pid_text.isdigit():
            continue
        try:
            arguments = shlex.split(command)
        except ValueError:
            continue
        if not arguments or Path(arguments[0]).name != "iproxy":
            continue
        udid = ""
        for index, argument in enumerate(arguments[:-1]):
            if argument in {"-u", "--udid"}:
                udid = arguments[index + 1]
                break
        endpoint = None
        for argument in arguments[1:]:
            host, colon, guest = argument.partition(":")
            if colon and host.isdigit() and guest.isdigit():
                endpoint = (int(host), int(guest))
                break
        # libimobiledevice 旧版形态：iproxy [options] HOST_PORT GUEST_PORT。
        if endpoint is None and len(arguments) >= 3:
            host, guest = arguments[-2:]
            if host.isdigit() and guest.isdigit():
                endpoint = (int(host), int(guest))
        if endpoint is not None:
            processes.append((int(pid_text), udid, endpoint[0], endpoint[1]))
    return tuple(processes)


def _iproxy_running(guest_port: int) -> bool:
    matches = [
        process
        for process in _iproxy_processes()
        if process[2] == HOST_PORT
        and process[3] == guest_port
        and process[1] == EXPECTED_UDID
    ]
    if matches and IPROXY_PID_FILE is not None:
        first_pid = str(matches[0][0])
        if first_pid:
            IPROXY_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            IPROXY_PID_FILE.write_text(first_pid + "\n", encoding="utf-8")
    return bool(matches)


def _stop_iproxy_on_host_port() -> tuple[int, ...]:
    stopped: list[int] = []
    for pid, _udid, host_port, _guest_port in _iproxy_processes():
        if host_port != HOST_PORT:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        stopped.append(pid)
    if stopped:
        time.sleep(0.2)
    return tuple(stopped)


def start_iproxy(guest_port: int = GUEST_PORT) -> None:
    if _iproxy_running(guest_port):
        _log(f"iproxy {HOST_PORT}->{guest_port} already running")
        return
    # A tunnel pinned to this exact host port is stale. Parse argv so port
    # 2222 never matches another Rig's 12222, and migrate the old two-port
    # iproxy command shape without process-name-wide mutation.
    _stop_iproxy_on_host_port()
    identity = f" udid={EXPECTED_UDID}" if EXPECTED_UDID else ""
    _log(f"starting iproxy {HOST_PORT}->{guest_port}{identity}")
    proc = subprocess.Popen(
        _iproxy_command(guest_port),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if IPROXY_PID_FILE is not None:
        IPROXY_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        IPROXY_PID_FILE.write_text(f"{proc.pid}\n", encoding="utf-8")
    time.sleep(1.0)
    if not _iproxy_running(guest_port):
        raise RuntimeError("iproxy failed to start")


def _ssh_cmd(remote_cmd: str) -> list[str]:
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=8",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "IdentitiesOnly=yes",
        "-o", "NumberOfPasswordPrompts=1",
        "-p", str(HOST_PORT), "mobile@127.0.0.1",
        remote_cmd,
    ]


def _retry_login(
    label: str,
    attempt_once: Callable[[], tuple[bool, str]],
) -> tuple[bool, str]:
    """Repeat a login round-trip until it succeeds or the attempts run out.

    Returns the last attempt's (ok, detail) — detail is the failure reason
    when ok is False, and whatever the round-trip reports when it is True.
    """
    ok, detail = False, ""
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        ok, detail = attempt_once()
        if ok:
            if attempt > 1:
                _log(f"{label} ok on attempt {attempt}/{LOGIN_ATTEMPTS}")
            return True, detail
        if attempt < LOGIN_ATTEMPTS:
            _log(f"{label} failed ({detail}) — retrying "
                 f"{attempt + 1}/{LOGIN_ATTEMPTS} in {LOGIN_RETRY_DELAY_S:g}s")
            time.sleep(LOGIN_RETRY_DELAY_S)
    return False, detail


def _login_probe_once() -> tuple[bool, str]:
    try:
        out = run_password_command(
            _ssh_cmd("echo AUTOPHONE_SSH_OK"), password=guest_ssh_password(),
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False, "ssh probe timed out"
    if out.returncode == 0 and "AUTOPHONE_SSH_OK" in out.stdout:
        return True, ""
    tail = (out.stderr.strip() or out.stdout.strip()).splitlines()
    return False, (tail[-1] if tail else f"rc={out.returncode}")


def login_usable() -> tuple[bool, str]:
    """Real host-side password login through the tunnel — the only
    trustworthy 'ssh is up' signal (see module docstring)."""
    return _retry_login("login probe", _login_probe_once)


def _verify_login_once() -> tuple[bool, str]:
    try:
        out = run_password_command(
            _ssh_cmd(f"export PATH={REMOTE_PATH}:$PATH; id; uname -a"), password=guest_ssh_password(),
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False, "ssh verify timed out"
    if out.returncode != 0 or "uid=" not in out.stdout:
        return False, (f"rc={out.returncode}: "
                       f"{out.stdout!r} {out.stderr!r}")
    return True, out.stdout.strip().splitlines()[0]


def verify_login() -> None:
    ok, detail = _retry_login("ssh login verify", _verify_login_once)
    if not ok:
        raise RuntimeError(
            f"ssh login verify failed after {LOGIN_ATTEMPTS} attempts: "
            f"{detail}")
    _log("ssh login OK: " + detail)


def main() -> int:
    global EXPECTED_UDID, HOST_PORT, IPROXY_PID_FILE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the ssh login round-trip check")
    ap.add_argument(
        "--host-port",
        type=int,
        default=int(os.environ.get("AUTOPHONE_SSH_PORT", str(HOST_PORT))),
        help="host TCP port for this Rig's SSH tunnel",
    )
    ap.add_argument(
        "--udid",
        default=os.environ.get("AUTOPHONE_EXPECTED_USB_UDID", ""),
        help="expected usbmux UDID; passed to iproxy -u",
    )
    ap.add_argument(
        "--pid-file",
        type=Path,
        default=None,
        help="write the selected iproxy PID for exact-target cleanup",
    )
    args = ap.parse_args()
    if not 1 <= args.host_port <= 65535:
        ap.error("--host-port must be in 1..65535")
    try:
        guest_ssh_password()
    except ValueError as exc:
        ap.error(str(exc))
    HOST_PORT = args.host_port
    EXPECTED_UDID = args.udid.strip()
    IPROXY_PID_FILE = args.pid_file

    start_iproxy()
    ok, err = login_usable()
    if ok:
        _log("guest :22 already serves password logins — no sshd action needed")
    else:
        _log(
            f"login via guest :22 unusable ({err}) — "
            f"probing native dropbear :{DROPBEAR_GUEST_PORT}"
        )
        start_iproxy(DROPBEAR_GUEST_PORT)
        ok, dropbear_err = login_usable()
        if ok:
            _log(
                f"native dropbear :{DROPBEAR_GUEST_PORT} serves "
                "password logins"
            )
        else:
            _log(
                f"native dropbear :{DROPBEAR_GUEST_PORT} unusable "
                f"({dropbear_err}) — bringing up OpenSSH"
            )
            ensure_host_keys()
            start_iproxy()
            try:
                start_sshd()
                ok, err = login_usable()
                if not ok:
                    _log(
                        "standalone sshd on :22 up but login still fails "
                        f"({err})"
                    )
            except RuntimeError as exc:
                _log(f"standalone sshd on :22 failed: {exc}")
                ok = False
        if not ok:
            _log(f"guest :22 unusable — falling back to :{ALT_GUEST_PORT}")
            start_sshd_fallback_port()
    if not args.no_verify:
        verify_login()
    _log(f"done — ssh -p {HOST_PORT} mobile@127.0.0.1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
