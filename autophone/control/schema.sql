CREATE TABLE IF NOT EXISTS hosts (
    host_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('online', 'offline', 'drain')),
    max_active_rigs INTEGER NOT NULL CHECK (max_active_rigs >= 0),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE hosts ADD COLUMN IF NOT EXISTS qualified_rigs JSONB;

-- NULL keeps the Step D policy until a Host qualification worker is enabled.
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS environment_observation JSONB;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS environment_observed_at TIMESTAMPTZ;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS qualified_environment TEXT;

CREATE TABLE IF NOT EXISTS rigs (
    rig_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    generation BIGINT NOT NULL CHECK (generation > 0),
    status TEXT NOT NULL CHECK (status IN ('online', 'offline', 'drain', 'quarantine')),
    capability_support JSONB NOT NULL DEFAULT '[]'::jsonb,
    capability_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    fencing_counter BIGINT NOT NULL DEFAULT 0,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- BL-151: the Agent process runs a code snapshot taken at start-up, so the
-- handshake reports which code it actually executes. NULL means an Agent that
-- predates this column; dispatch admission treats NULL as compatibility, not as
-- a match. Kept separate from capability_state/observed_state: those describe the
-- device and are reset per generation, while this one describes the Agent process.
ALTER TABLE rigs ADD COLUMN IF NOT EXISTS source_identity JSONB;

-- Agent connectivity is separate from operator disposition and observation time.
ALTER TABLE rigs ADD COLUMN IF NOT EXISTS agent_connection_id TEXT;
ALTER TABLE rigs ADD COLUMN IF NOT EXISTS agent_connected_until TIMESTAMPTZ;
ALTER TABLE rigs ADD COLUMN IF NOT EXISTS deregistration JSONB;

-- BL-224: the Agent reports the ScreenLock quarantine markers found in its own
-- host lock root. NULL means the Agent has not reported (older Agent): unknown,
-- not clear. Submission and dispatch refuse a Rig whose last report is quarantined.
ALTER TABLE rigs ADD COLUMN IF NOT EXISTS screen_quarantine JSONB;

CREATE TABLE IF NOT EXISTS rig_targets (
    rig_id TEXT PRIMARY KEY REFERENCES rigs(rig_id),
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    rig_generation BIGINT NOT NULL CHECK (rig_generation > 0),
    cli_dir TEXT NOT NULL,
    vm_dir TEXT NOT NULL,
    socket_path TEXT NOT NULL,
    expected_usb_udid TEXT NOT NULL,
    ssh_host TEXT NOT NULL,
    ssh_port INTEGER NOT NULL CHECK (ssh_port BETWEEN 1 AND 65535),
    frida_host TEXT NOT NULL,
    frida_port INTEGER NOT NULL CHECK (frida_port BETWEEN 1 AND 65535),
    frida_guest_port INTEGER NOT NULL CHECK (frida_guest_port BETWEEN 1 AND 65535),
    log_dir TEXT NOT NULL,
    temp_dir TEXT NOT NULL,
    package_cache TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (host_id, vm_dir),
    UNIQUE (host_id, socket_path),
    UNIQUE (host_id, ssh_host, ssh_port),
    UNIQUE (host_id, frida_host, frida_port),
    UNIQUE (host_id, log_dir),
    UNIQUE (host_id, temp_dir),
    UNIQUE (host_id, package_cache)
);

-- Existing control schemas predate explicit USB identity and the split Frida
-- host/guest port. Registration requires both fields; the nullable migration
-- column lets an operator re-register existing Rigs without fabricating an
-- identity during schema initialization.
ALTER TABLE rig_targets ADD COLUMN IF NOT EXISTS expected_usb_udid TEXT;
ALTER TABLE rig_targets ADD COLUMN IF NOT EXISTS frida_guest_port INTEGER NOT NULL DEFAULT 27042;
-- Migration follow-up: after every legacy Rig has re-registered a verified
-- expected_usb_udid, apply ALTER COLUMN expected_usb_udid SET NOT NULL.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rig_targets_host_usb_udid
ON rig_targets(host_id, expected_usb_udid) WHERE expected_usb_udid IS NOT NULL;

CREATE TABLE IF NOT EXISTS host_maintenance (
    maintenance_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    intent TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'passed', 'failed', 'expired')),
    expires_at TIMESTAMPTZ NOT NULL,
    result JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_host_maintenance
ON host_maintenance(host_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS host_coordination_events (
    event_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    rig_id TEXT REFERENCES rigs(rig_id),
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Host 异常记录与暴露量，见 docs/platform-host-resource-admission.md §3。
CREATE TABLE IF NOT EXISTS host_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    kind TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    fingerprint TEXT NOT NULL,
    record JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_host_anomalies_host_time
    ON host_anomalies(host_id, detected_at DESC);

-- 复盘只追加，原始记录不覆盖。
CREATE TABLE IF NOT EXISTS host_anomaly_reviews (
    review_id TEXT PRIMARY KEY,
    anomaly_id TEXT NOT NULL REFERENCES host_anomalies(anomaly_id),
    actor TEXT NOT NULL,
    labels JSONB NOT NULL DEFAULT '[]'::jsonb,
    note TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_host_anomaly_reviews_anomaly
    ON host_anomaly_reviews(anomaly_id);

CREATE TABLE IF NOT EXISTS host_load_samples (
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    sampled_at TIMESTAMPTZ NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    running_vm_count INTEGER NOT NULL CHECK (running_vm_count >= 0),
    active_rigs INTEGER,
    memory_free_percent INTEGER,
    pressure_level INTEGER,
    swap_used_bytes BIGINT,
    load_1m DOUBLE PRECISION,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (host_id, sampled_at)
);
ALTER TABLE host_load_samples ADD COLUMN IF NOT EXISTS running_vms JSONB;

-- 资源准入影子模式：只记录新逻辑会怎么判，不影响派单。
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS allocatable JSONB;
ALTER TABLE rigs ADD COLUMN IF NOT EXISTS resource_requests JSONB;

-- capacity 是默认行为；resource 用内存、压力和熔断准入。回退只需改回 capacity。
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS admission_mode TEXT NOT NULL DEFAULT 'capacity'
    CHECK (admission_mode IN ('capacity', 'resource'));
CREATE TABLE IF NOT EXISTS host_breaker (
    host_id TEXT PRIMARY KEY REFERENCES hosts(host_id),
    state TEXT NOT NULL CHECK (state IN ('closed', 'open', 'half_open', 'locked')),
    since TIMESTAMPTZ NOT NULL,
    reason TEXT NOT NULL
);
-- 每条 rig_down 只判一次是否重跑，判定结果留作记录。
CREATE TABLE IF NOT EXISTS host_anomaly_reruns (
    anomaly_id TEXT PRIMARY KEY REFERENCES host_anomalies(anomaly_id),
    decisions JSONB NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS host_admission_shadow (
    bucket TIMESTAMPTZ NOT NULL,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    assignment_id TEXT NOT NULL,
    rig_id TEXT NOT NULL,
    capacity_decision TEXT NOT NULL,
    resource_decision TEXT NOT NULL,
    evaluations INTEGER NOT NULL DEFAULT 1,
    inputs JSONB NOT NULL,
    PRIMARY KEY (bucket, assignment_id, rig_id, capacity_decision, resource_decision)
);
CREATE INDEX IF NOT EXISTS idx_host_admission_shadow_host_time
    ON host_admission_shadow(host_id, bucket DESC);

CREATE TABLE IF NOT EXISTS host_capacity_trials (
    trial_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    previous_capacity INTEGER NOT NULL CHECK (previous_capacity >= 0),
    trial_capacity INTEGER NOT NULL CHECK (trial_capacity > previous_capacity),
    status TEXT NOT NULL CHECK (status IN ('active', 'rolled_back', 'failed', 'expired')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    preflight_evidence JSONB NOT NULL,
    result JSONB,
    expires_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_host_capacity_trial
ON host_capacity_trials(host_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS rig_reservations (
    reservation_id TEXT PRIMARY KEY,
    rig_id TEXT NOT NULL REFERENCES rigs(rig_id),
    owner JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released')),
    expires_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    release_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_reservation_per_rig
ON rig_reservations(rig_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('high', 'normal')),
    requires_rig BOOLEAN NOT NULL,
    target_rig_id TEXT REFERENCES rigs(rig_id),
    reservation_id TEXT REFERENCES rig_reservations(reservation_id),
    required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    desired_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    reset_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    run_spec JSONB,
    status TEXT NOT NULL,
    wait_reason TEXT,
    cancel_request JSONB,
    queue_deadline TIMESTAMPTZ,
    bound_host_id TEXT,
    bound_rig_id TEXT,
    bound_generation BIGINT,
    current_attempt_no INTEGER,
    current_fencing BIGINT,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (run_spec IS NULL OR jsonb_typeof(run_spec) = 'object')
);

-- 重跑是一个新 Assignment，带新的 run_id，第一次的证据不动。每个 Assignment 最多重跑一次。
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS rerun_of TEXT REFERENCES assignments(assignment_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_assignments_rerun_of ON assignments(rerun_of) WHERE rerun_of IS NOT NULL;

CREATE TABLE IF NOT EXISTS assignment_attempts (
    assignment_id TEXT NOT NULL REFERENCES assignments(assignment_id),
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    host_id TEXT NOT NULL,
    rig_id TEXT NOT NULL,
    rig_generation BIGINT NOT NULL,
    fencing BIGINT NOT NULL,
    status TEXT NOT NULL,
    stage_results JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    last_heartbeat TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (assignment_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS recovery_incidents (
    recovery_id TEXT PRIMARY KEY,
    rig_id TEXT NOT NULL REFERENCES rigs(rig_id),
    rig_generation BIGINT NOT NULL,
    down_edge_sequence BIGINT NOT NULL CHECK (down_edge_sequence > 0),
    edge_id UUID,
    state TEXT NOT NULL CHECK (state IN ('observed','abort_requested','waiting_lease','deferred','recovering','recovered','quarantined','isolation_confirmed')),
    version BIGINT NOT NULL DEFAULT 0,
    actor TEXT NOT NULL,
    diagnostics JSONB NOT NULL,
    old_assignment_id TEXT,
    old_attempt_no INTEGER,
    recovery_assignment_id TEXT UNIQUE,
    recovery_attempt_no INTEGER,
    recovery_fencing BIGINT,
    automatic BOOLEAN NOT NULL DEFAULT true,
    retry_authorized BOOLEAN NOT NULL DEFAULT false,
    isolation_receipt JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE recovery_incidents ADD COLUMN IF NOT EXISTS isolation_receipt JSONB;
ALTER TABLE recovery_incidents ADD COLUMN IF NOT EXISTS edge_id UUID;
ALTER TABLE recovery_incidents DROP CONSTRAINT IF EXISTS recovery_incidents_rig_id_rig_generation_down_edge_sequence_key;
DROP INDEX IF EXISTS recovery_legacy_edge_identity;
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_incidents_edge_identity
    ON recovery_incidents(rig_id, rig_generation, down_edge_sequence, edge_id);

CREATE TABLE IF NOT EXISTS rig_leases (
    lease_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    rig_id TEXT NOT NULL REFERENCES rigs(rig_id),
    rig_generation BIGINT NOT NULL,
    fencing BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    release_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (assignment_id, attempt_no)
        REFERENCES assignment_attempts(assignment_id, attempt_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_lease_per_rig
ON rig_leases(rig_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS rig_recovery_receipts (
    receipt_id TEXT PRIMARY KEY,
    rig_id TEXT NOT NULL REFERENCES rigs(rig_id),
    rig_generation BIGINT NOT NULL,
    actor TEXT NOT NULL,
    probe_result JSONB NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('recovered', 'remain_quarantined')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL UNIQUE REFERENCES assignments(assignment_id),
    run_spec JSONB NOT NULL,
    status TEXT NOT NULL,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE runs ADD COLUMN IF NOT EXISTS closure JSONB;

-- BL-076：Lease 过期中断的 Attempt 的资源闭包（加法；旧库在下次 initialize() 时补建）。
-- 行由 reap_expired_leases 在同一事务内登记为 pending；`rig recover` 置 waiting_lease 并创建闭包恢复
-- Assignment；派单绑定时置 closing；完成时置 closed / failed。不改写被中断 Attempt 的终态与结果。
CREATE TABLE IF NOT EXISTS interrupted_attempt_closures (
    assignment_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    rig_id TEXT NOT NULL REFERENCES rigs(rig_id),
    rig_generation BIGINT NOT NULL,
    fencing BIGINT NOT NULL,
    run_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending','waiting_lease','closing','closed','failed')),
    closure_assignment_id TEXT,
    closure_attempt_no INTEGER,
    closure_fencing BIGINT,
    receipt JSONB,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (assignment_id, attempt_no),
    FOREIGN KEY (assignment_id, attempt_no) REFERENCES assignment_attempts(assignment_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS ix_interrupted_attempt_closures_rig
    ON interrupted_attempt_closures(rig_id, rig_generation, state);
CREATE INDEX IF NOT EXISTS ix_interrupted_attempt_closures_assignment
    ON interrupted_attempt_closures(closure_assignment_id);
ALTER TABLE rig_recovery_receipts ADD COLUMN IF NOT EXISTS interrupted_closure JSONB;

CREATE TABLE IF NOT EXISTS artifact_refs (
    artifact_id TEXT PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size BIGINT NOT NULL CHECK (size >= 0),
    governance TEXT NOT NULL,
    seal_status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_kind, owner_id, logical_name)
);

-- B 端接入授权记录（BL-104，docs/platform-device-console-embed.md §十一）。
-- 接入方本身登记在配置文件里（§6.1），不建 access_platforms 表。
CREATE TABLE IF NOT EXISTS access_grants (
    grant_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    platform_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    device TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('view', 'control')),
    status TEXT NOT NULL CHECK (status IN ('creating', 'active', 'closing', 'closed')),
    console_session_id TEXT,
    controller_token TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    closed_reason TEXT,
    error_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 迁移（2026-09-09 裁定）：`uq_active_grant_per_device` 原定义不带 `mode` 条件，
-- 一台设备因此同时只有一条未关闭授权，与 §7.5「每个观看者各持一个观看凭证」并存不了。
-- `CREATE UNIQUE INDEX IF NOT EXISTS` 对已存在的索引不改定义，所以先比对旧定义：
-- 认出旧定义（没有 mode 条件）就 DROP，随后统一由下面的 CREATE 建新定义。
-- 新定义是旧定义的子集（索引的行更少），已满足旧约束的库一定能建出来。
--
-- 只按名字取本 schema 内这一个索引的 OID，再对这一个 OID 调 pg_get_indexdef()。
-- 不查 pg_indexes：该视图的 indexdef 列对扫到的每一行调用 pg_get_indexdef()，需要
-- 打开对应 relation；测试夹具给每个 xdist worker 建独立 schema 并发 CREATE/DROP 时，
-- 扫全表会撞到别的 worker 刚删掉的 relation，报 could not open relation with OID …。
DO $$
DECLARE
    target TEXT := current_schema();
    idx REGCLASS := to_regclass(format('%I.%I', current_schema(), 'uq_active_grant_per_device'));
BEGIN
    IF idx IS NULL THEN
        RETURN;
    END IF;
    -- 同名对象不是索引时 pg_get_indexdef() 返回 NULL，比较结果为 NULL，不 DROP。
    IF pg_get_indexdef(idx) NOT LIKE '%mode = ''control''%' THEN
        EXECUTE format('DROP INDEX %I.uq_active_grant_per_device', target);
    END IF;
END
$$;

-- PG 侧的第二道设备互斥（主互斥仍是 rig 侧 ScreenLock），只管 control 授权：它覆盖
-- closing，所以清理未完成的设备不会被分配出去；`mode = 'control'` 让同一台设备上
-- 可以并存一条 control 授权与任意多条 view 授权（§7.5，2026-09-09 裁定）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_grant_per_device
ON access_grants(device) WHERE mode = 'control' AND status IN ('creating', 'active', 'closing');

-- §7.4 的一人一台：同一接入方下同一 subject 同时只有一条 control 授权。
CREATE UNIQUE INDEX IF NOT EXISTS uq_control_grant_per_subject
ON access_grants(platform_id, subject) WHERE mode = 'control' AND status IN ('creating', 'active');

-- 踢出与撤销的只追加事件表（BL-116，2026-09-23 裁定②，docs/platform-device-console-embed.md §十一）。
-- `access_grants.closed_reason` 仍是行上的当前值，本表记录每一次关闭操作的明细。
-- 写入只经 `update_access_grant(..., event=...)`，与授权记录的变更同一事务；
-- 下面的触发器拒绝对本表的 UPDATE、DELETE 与 TRUNCATE，只追加由数据库保证。
-- 新表用 `CREATE TABLE IF NOT EXISTS`，既有库在 `initialize()` 下次执行时自动补建。
CREATE TABLE IF NOT EXISTS access_grant_events (
    event_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES access_grants(grant_id),
    kind TEXT NOT NULL CHECK (kind IN ('revoked', 'kicked', 'expired')),
    actor TEXT NOT NULL CHECK (actor <> ''),
    reason TEXT NOT NULL CHECK (reason <> ''),
    platform_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    device TEXT NOT NULL,
    mode TEXT NOT NULL,
    console_session_id TEXT,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_access_grant_events_grant
ON access_grant_events(grant_id, created_at);

CREATE INDEX IF NOT EXISTS ix_access_grant_events_device
ON access_grant_events(device, created_at);

CREATE OR REPLACE FUNCTION access_grant_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'access_grant_events is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$;

-- 触发器只在不存在时创建：不用 DROP TRIGGER，免得每次 initialize() 都对本表取排他锁。
DO $$
DECLARE
    tbl REGCLASS := to_regclass(format('%I.%I', current_schema(), 'access_grant_events'));
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = tbl AND tgname = 'access_grant_events_no_update_delete'
    ) THEN
        CREATE TRIGGER access_grant_events_no_update_delete
        BEFORE UPDATE OR DELETE ON access_grant_events
        FOR EACH ROW EXECUTE FUNCTION access_grant_events_append_only();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = tbl AND tgname = 'access_grant_events_no_truncate'
    ) THEN
        CREATE TRIGGER access_grant_events_no_truncate
        BEFORE TRUNCATE ON access_grant_events
        FOR EACH STATEMENT EXECUTE FUNCTION access_grant_events_append_only();
    END IF;
END
$$;

-- X0: this is the authority for external Fixture reuse, separate from Rig readiness.
CREATE TABLE IF NOT EXISTS reset_fixtures (
    resource_key TEXT PRIMARY KEY,
    target JSONB NOT NULL,
    expected JSONB NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('restored', 'failed', 'unknown')),
    verification JSONB NOT NULL,
    receipt JSONB,
    owner_assignment_id TEXT,
    owner_attempt_no INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((owner_assignment_id IS NULL) = (owner_attempt_no IS NULL))
);
