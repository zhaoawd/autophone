"""Smoke for the UDS {"t":"shell"} command (vphone-cli ffc8f55 + 1a94fdf).

Run: python3 -m tools.phase1.shell_smoke

Exercises the raw UDS contract (not the vphone_shell adapter) so it doubles
as a regression check on the upstream surface. Baseline 11/11 since upstream
30d1f47 (concurrent UDS clients) + 0ebb87f (structured cwd error).
"""
import sys
import threading
import time

from autophone.device.uds import request
from autophone.device.shell import resolve_sock

SOCK = resolve_sock()
results = []


def sh(cmd, *, cwd=None, timeout_ms=None, screen=False, sock_timeout=None):
    msg = {"t": "shell", "cmd": cmd, "screen": screen}
    if cwd:
        msg["cwd"] = cwd
    if timeout_ms:
        msg["timeout_ms"] = timeout_ms
    if sock_timeout is None:
        sock_timeout = (timeout_ms or 30000) / 1000 + 20
    t0 = time.perf_counter()
    resp, _ = request(msg, sock_path=SOCK, timeout=sock_timeout)
    wall = time.perf_counter() - t0
    return resp, wall


def check(name, cond, detail):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}: {detail}")


def main():
    # 1. basic: UTF-8 roundtrip, exit code 0, identity
    r, w = sh("export PATH=/var/jb/usr/bin:/var/jb/bin:$PATH; "
              "echo 中文测试OK; uname -a; id")
    check("basic", r.get("ok") and r.get("code") == 0
          and "中文测试OK" in r.get("stdout", ""),
          f"code={r.get('code')} wall={w:.2f}s out={r.get('stdout','')[:120]!r}")

    # 2. exit code propagation
    r, w = sh("exit 7")
    check("exit-code", r.get("code") == 7, f"code={r.get('code')}")

    # 3. cwd folding
    r, w = sh("pwd", cwd="/var/mobile")
    check("cwd", r.get("stdout", "").strip() == "/var/mobile",
          f"out={r.get('stdout','').strip()!r}")

    # 3b. bad cwd → structured error (since 0ebb87f; was exit code 127)
    r, w = sh("pwd", cwd="/nonexistent-dir-xyz")
    check("cwd-bad", r.get("ok") is False
          and "not a directory" in r.get("error", ""),
          f"ok={r.get('ok')} error={r.get('error','')[:80]!r}")

    # 4. stderr separation
    r, w = sh("echo to-out; echo to-err >&2")
    check("stderr", "to-out" in r.get("stdout", "")
          and "to-err" in r.get("stderr", "")
          and "to-err" not in r.get("stdout", ""),
          f"out={r.get('stdout','').strip()!r} err={r.get('stderr','').strip()!r}")

    # 5. timeout: sleep 60 with 5s cap → timed_out, code=137, wall ≈5s
    r, w = sh("/var/jb/bin/sleep 60", timeout_ms=5000)
    check("timeout", r.get("timed_out") is True and 4.5 <= w <= 12,
          f"timed_out={r.get('timed_out')} code={r.get('code')} wall={w:.2f}s")

    # 6. truncation: ~2MB stdout → truncated flag, out capped at 1MiB
    r, w = sh("export PATH=/var/jb/usr/bin:/var/jb/bin:$PATH; "
              "head -c 2000000 /dev/zero | tr '\\0' 'a'", timeout_ms=30000)
    check("truncate", r.get("truncated") is True
          and len(r.get("stdout", "")) <= 1048576 + 16,
          f"truncated={r.get('truncated')} out_len={len(r.get('stdout',''))} "
          f"wall={w:.2f}s")

    # 6b. link survived the big response
    r, w = sh("echo still-alive")
    check("post-truncate-alive", "still-alive" in r.get("stdout", ""),
          f"out={r.get('stdout','').strip()!r}")

    # 7a. background footgun: child holds pipe → drains until timeout
    r, w = sh("/var/jb/bin/sleep 30 & echo bg-started", timeout_ms=5000)
    check("bg-footgun(expected-hang)", r.get("timed_out") is True and w >= 4.5,
          f"timed_out={r.get('timed_out')} wall={w:.2f}s "
          f"out={r.get('stdout','').strip()!r}")

    # 7b. with redirects → returns immediately
    r, w = sh("/var/jb/bin/sleep 30 >/dev/null 2>&1 </dev/null & echo bg-clean",
              timeout_ms=5000)
    check("bg-redirected", r.get("timed_out") in (False, None) and w < 3
          and "bg-clean" in r.get("stdout", ""),
          f"timed_out={r.get('timed_out')} wall={w:.2f}s")

    # 8. concurrency: a long shell must not block screenshot — host handles
    # UDS clients on per-connection workers since upstream 30d1f47.
    shell_done = {}

    def long_shell():
        resp, wall = sh("/var/jb/bin/sleep 8; echo slept", timeout_ms=20000)
        shell_done.update(resp=resp, wall=wall)

    th = threading.Thread(target=long_shell)
    th.start()
    time.sleep(1.0)  # let the shell start
    shot_times = []
    for _ in range(3):
        t0 = time.perf_counter()
        request({"t": "screenshot"}, sock_path=SOCK, timeout=15)
        shot_times.append(time.perf_counter() - t0)
        time.sleep(0.5)
    th.join()
    max_shot = max(shot_times)
    check("concurrency", max_shot < 4.0
          and "slept" in shell_done["resp"].get("stdout", ""),
          f"screenshot_max={max_shot:.2f}s shell_wall={shell_done['wall']:.2f}s")

    print()
    fails = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(fails)}/{len(results)} passed"
          + (f"; FAILED: {fails}" if fails else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
