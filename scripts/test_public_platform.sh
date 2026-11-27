#!/usr/bin/env bash
# Run the public platform suite against a disposable PostgreSQL database.
# Provisioning order:
#   1. Reuse AUTOPHONE_TEST_POSTGRES_DSN if already set.
#   2. Start a local Unix-socket-only PostgreSQL instance if pg_config/initdb/pg_ctl exist.
#   3. Fall back to an ephemeral postgres:16 Docker container bound to 127.0.0.1.
set -euo pipefail
cd "$(dirname "$0")/.."

pgbin=""
work=""
docker_cid=""

cleanup() {
    local rc=0
    if [[ -n "$work" ]]; then
        if [[ -f "$work/db/postmaster.pid" ]]; then
            "$pgbin/pg_ctl" -D "$work/db" -m fast -w stop >/dev/null || rc=1
        fi
        rm -rf -- "$work"
    fi
    if [[ -n "$docker_cid" ]]; then
        docker rm -f "$docker_cid" >/dev/null 2>&1 || true
    fi
    return "$rc"
}

if [[ -n "${AUTOPHONE_TEST_POSTGRES_DSN:-}" ]]; then
    export AUTOPHONE_TEST_POSTGRES_DSN
elif command -v pg_config >/dev/null 2>&1 \
    && pgbin="$(pg_config --bindir)" \
    && [[ -x "$pgbin/initdb" && -x "$pgbin/pg_ctl" ]]; then
    work="$(mktemp -d /tmp/autophone-platform.XXXXXXXX)"
    trap cleanup EXIT
    "$pgbin/initdb" -D "$work/db" -U platform_test --auth-local=trust --auth-host=reject --no-locale -E UTF8 >"$work/init.log"
    "$pgbin/pg_ctl" -D "$work/db" -l "$work/server.log" -o "-c listen_addresses='' -k $work" -w start >/dev/null
    export AUTOPHONE_TEST_POSTGRES_DSN="dbname=postgres host=$work user=platform_test"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    trap cleanup EXIT
    docker_cid="$(docker run -d --rm \
        -e POSTGRES_USER=platform_test \
        -e POSTGRES_PASSWORD=platform_test \
        -e POSTGRES_DB=platform_test \
        -p 127.0.0.1::5432 \
        postgres:16)"
    port="$(docker port "$docker_cid" 5432/tcp | head -n 1 | awk -F: '{print $NF}')"
    if [[ -z "$port" ]]; then
        echo "Error: failed to determine mapped PostgreSQL port for container $docker_cid." >&2
        exit 1
    fi
    ready=0
    for _ in $(seq 1 30); do
        if docker exec "$docker_cid" pg_isready -h 127.0.0.1 -U platform_test -d platform_test >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 1
    done
    if [[ "$ready" -ne 1 ]]; then
        echo "Error: ephemeral PostgreSQL container ($docker_cid) did not become ready within 30s." >&2
        exit 1
    fi
    export AUTOPHONE_TEST_POSTGRES_DSN="postgresql://platform_test:platform_test@127.0.0.1:${port}/platform_test?connect_timeout=5"
else
    cat >&2 <<'EOF'
Error: no PostgreSQL backend available for public platform tests.

Configure one of the following options and re-run:
  1. Install local PostgreSQL binaries (fastest, Unix-socket only):
       brew install postgresql@16
       export PATH="$(brew --prefix postgresql@16)/bin:$PATH"
  2. Start Docker so this script can launch an ephemeral postgres:16 container automatically.
  3. Supply an existing disposable PostgreSQL DSN:
       AUTOPHONE_TEST_POSTGRES_DSN='postgresql://user:pass@127.0.0.1:5432/dbname' \
         scripts/test_public_platform.sh
EOF
    exit 2
fi

# All HTTP and WebSocket endpoints in this suite use loopback.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
"${PYTHON:-.venv/bin/python}" -m pytest -q @tests/public_platform.txt -m 'not live_device and not live_frida' "$@"
