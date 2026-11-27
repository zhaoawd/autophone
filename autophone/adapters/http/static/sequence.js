"use strict";
/* 需求序列页（执行方案 §6.3 最小版本）。
 *
 * 三条渲染纪律，和投影层是同一条：
 * ① 投影说 verifiable=false 就显示「不可核对」并带出理由，**不显示 0**；
 * ② 交付需求完成数的分母恒为 4，R0 是基线项，不写成「五项均 accepted」；
 * ③ 阻断先说对象、影响、下一步，原始错误码进可展开详情。
 * 页面自己不做任何计数换算——数字全部来自 /api/requirements/sequences/<id>。
 */

const esc = (value) => String(value === null || value === undefined ? "" : value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

/** 不可核对的统一呈现：理由照抄投影，不猜、不补零。 */
function value(node, key, suffix) {
  if (!node || node.verifiable === false) {
    return `<span class="seq-unknown" title="${esc(node && node.reason || "")}">不可核对</span>`;
  }
  const raw = node[key];
  if (raw === null || raw === undefined) {
    return `<span class="seq-unknown">未记录</span>`;
  }
  return `${esc(raw)}${suffix || ""}`;
}

const STATUS_TONE = {
  accepted: "is-ok", baseline_verified: "is-ok",
  active: "", draft: "",
  abandoned: "is-warn", baseline_mismatch: "is-bad", mismatch: "is-bad",
  not_started: "is-warn", not_verifiable: "is-warn",
  baseline_not_recorded: "is-warn", baseline_not_verifiable: "is-warn",
  not_recorded: "is-warn", match: "is-ok",
};
const STATUS_TEXT = {
  accepted: "已验收", active: "进行中", draft: "草稿", abandoned: "已废弃",
  not_started: "未开始", not_verifiable: "不可核对",
  baseline_verified: "基线已核对", baseline_mismatch: "基线不一致",
  baseline_not_recorded: "基线未记录", baseline_not_verifiable: "基线不可核对",
  match: "已核对一致", mismatch: "核对不一致", not_recorded: "未记录",
};
const pill = (key) =>
  `<span class="seq-pill ${STATUS_TONE[key] || ""}">${esc(STATUS_TEXT[key] || key || "未知")}</span>`;

const COMPLETENESS = {
  unrecorded: "未记录",
  partial: "部分记录",
  complete_within_declared_scope: "在声明范围内完整",
};

function renderSummary(model) {
  const s = model.summary;
  const stages = s.business_stages;
  const done = s.delivery_completed;
  const duration = s.duration;
  const manual = s.manual_record_completeness;
  const doneText = done.verifiable
    ? `${esc(done.value)} / ${esc(done.denominator)}`
    : `<span class="seq-unknown">${esc(done.value)} / ${esc(done.denominator)}（部分不可核对）</span>`;
  return `<section class="seq-card">
    <h2>序列概览</h2>
    <div class="seq-metrics">
      <div class="seq-metric"><b>${esc(stages.total)}</b><span>业务阶段</span>
        <small>${esc(stages.baseline)} 项基线 + ${esc(stages.delivery)} 项交付需求</small></div>
      <div class="seq-metric"><b>${doneText}</b><span>交付需求完成数</span>
        <small>${done.verifiable ? "分母恒为 4，基线项不计入" : "不可核对阶段：" + esc((done.not_verifiable_stages || []).join("、"))}</small></div>
      <div class="seq-metric"><b>${esc(s.attempts.total)}</b><span>全部尝试数</span>
        <small>含废弃 ${esc(s.attempts.abandoned)} 次、进行中 ${esc(s.attempts.in_progress)} 次</small></div>
      <div class="seq-metric"><b>${esc(duration.attempts_total_minutes)} 分钟</b><span>各尝试耗时相加</span>
        <small>${duration.complete ? "全部尝试时间可算" : "有尝试时间缺失，见下方缺口"}；统计时点 ${esc(duration.measured_at)}</small></div>
      <div class="seq-metric"><b>${duration.sequence_span_minutes === null ? "不可核对" : esc(duration.sequence_span_minutes) + " 分钟"}</b>
        <span>序列历时</span><small>口径 ${esc(duration.sequence_span_basis || "未确定")}，与工作流阶段时长不混用</small></div>
      <div class="seq-metric"><b>${esc(COMPLETENESS[manual.status] || manual.status)}</b><span>人工记录完整性</span>
        <small>流程外操作 ${manual.out_of_process_count === null ? "未记录" : esc(manual.out_of_process_count) + " 条"}；工作流人工触点 ${esc(manual.workflow_touchpoints)} 次</small></div>
    </div>
    <p class="seq-note">${esc(duration.note)}</p>
  </section>`;
}

function renderLinks(item) {
  const links = item.report_links || [];
  if (!links.length) return `<span class="seq-unknown">无报告链接</span>`;
  return links.map((link) => {
    if (link.kind === "baseline_record") {
      return `<span class="mono" title="${esc(link.note || "")}">基线记录 ${esc(link.path)}</span>`;
    }
    const label = link.kind === "story" ? "结果报告" : "证据明细";
    const state = link.state ? `（${esc(STATUS_TEXT[link.state] || link.state)}）` : "";
    return `<a href="${esc(link.href)}" target="_blank" rel="noopener">${esc(link.attempt_id)} ${label}${state}</a>`;
  }).join(" · ");
}

function renderAttempts(item) {
  if (!item.attempts.length) return `<p class="seq-note">这一项还没有登记任何尝试。</p>`;
  const rows = item.attempts.map((a) => `<tr>
    <td class="mono">${esc(a.attempt_id)}</td>
    <td class="mono">${a.req_id ? esc(a.req_id) : "—"}</td>
    <td>${pill(a.state)}</td>
    <td>${a.duration_minutes === null ? '<span class="seq-unknown">不可核对</span>' : esc(a.duration_minutes) + " 分钟"}
      <small>${esc(a.duration_basis || "")}</small></td>
    <td>${(a.gaps || []).length ? `<span class="seq-unknown">${esc(a.gaps.join("、"))}</span>` : "—"}</td>
  </tr>`).join("");
  return `<details><summary>全部尝试（${item.attempts.length} 次，含废弃）</summary>
    <table><thead><tr><th>尝试</th><th>需求单</th><th>状态</th><th>耗时</th><th>缺口</th></tr></thead>
    <tbody>${rows}</tbody></table></details>`;
}

function renderItem(item) {
  const v = item.verification;
  const r = item.repair;
  const f = item.findings;
  const h = item.handover;
  return `<article class="seq-item">
    <div class="seq-item-head">
      <h3>${esc(item.stage)} · ${esc(item.business_key)}</h3>
      ${pill(item.status.value)}
      <span class="seq-note">${esc(item.round_role === "baseline" ? "基线项，不计入交付完成数" : "交付需求项")}</span>
    </div>
    <p class="seq-goal">${esc(item.goal)}</p>
    <div class="seq-grid">
      <div><b>${value(v, "total")}</b><span>验证次数</span>
        <small>初始 ${value(v, "initial")} · 修复后 ${value(v, "after_repair")}</small></div>
      <div><b>${value(r, "execution_attempts")}</b><span>修复执行尝试</span>
        <small>成功 ${value(r, "succeeded")}</small></div>
      <div><b>${value(f, "records_total")}</b><span>Finding 记录数</span>
        <small>跨尝试累计，非去重后的业务缺陷数</small></div>
      <div><b>${pill(h.status)}</b><span>源码交接</span>
        <small>${h.next_item_recorded ? "下一项起点已登记" : "下一项起点未登记"}</small></div>
    </div>
    <p class="seq-note">报告：${renderLinks(item)}</p>
    ${renderAttempts(item)}
  </article>`;
}

function renderManual(model) {
  const m = model.manual;
  const ops = m.out_of_process_operations;
  const opRows = (ops.records || []).map((row) => `<tr>
    <td>${esc(row.operation_type || "—")}</td><td>${esc(row.actor || "—")}</td>
    <td>${esc(row.at || "—")}</td><td class="mono">${esc(row.attempt_id || "—")}</td>
    <td>${esc(row.evidence_ref || "—")}</td>
    <td>${(row.missing_fields || []).length ? `<span class="seq-unknown">缺 ${esc(row.missing_fields.join("、"))}</span>` : "—"}</td>
  </tr>`).join("");
  return `<section class="seq-card">
    <h2>人工记录</h2>
    <div class="seq-metrics">
      <div class="seq-metric"><b>${esc(m.workflow_touchpoints.count)}</b><span>工作流人工触点</span>
        <small>${esc(m.workflow_touchpoints.definition)}</small></div>
      <div class="seq-metric"><b>${esc(m.final_reviews.length)}</b><span>最终评审回执</span>
        <small>按实际回执列示，不并入工作流触点</small></div>
      <div class="seq-metric"><b>${esc(m.required_finding_acks.length)}</b><span>required Finding ack 记录</span>
        <small>按需求单列示</small></div>
      <div class="seq-metric"><b>${esc(m.source_sharing_authorizations.length)}</b><span>源码共享授权</span>
        <small>读 source_sharing_approved 回执</small></div>
    </div>
    <h2 style="margin-top:14px;">流程外操作 · ${esc(COMPLETENESS[ops.status] || ops.status)}</h2>
    ${opRows
      ? `<table><thead><tr><th>操作类型</th><th>人员</th><th>时间</th><th>关联尝试</th><th>证据</th><th>缺口</th></tr></thead><tbody>${opRows}</tbody></table>`
      : `<p class="seq-note">序列描述没有声明流程外操作记录，因此显示为未记录，不显示为 0 条。</p>`}
    ${(ops.reasons || []).length
      ? `<details><summary>记录缺口 ${ops.reasons.length} 条</summary><pre>${esc(ops.reasons.join("\n"))}</pre></details>` : ""}
  </section>`;
}

function renderIntegrity(model) {
  const gaps = model.integrity.gaps || [];
  if (!gaps.length) return `<section class="seq-card"><h2>核对完整性</h2>
    <p class="seq-note">本次投影没有读取缺口。这只说明引用都读到了，不代表任何一项已验收。</p></section>`;
  const rows = gaps.map((g) => `<tr><td class="mono">${esc(g.key)}</td><td>${esc(g.reason)}</td></tr>`).join("");
  return `<section class="seq-card"><h2>核对完整性 · ${gaps.length} 处缺口</h2>
    <p class="seq-note">以下事实读不到或对不上，相关数字已标为不可核对，没有折算成 0。</p>
    <table><thead><tr><th>对象</th><th>原因</th></tr></thead><tbody>${rows}</tbody></table></section>`;
}

function renderSequence(model) {
  const seq = model.sequence;
  document.getElementById("seqTitle").textContent = seq.title || "需求序列";
  document.getElementById("seqSub").textContent =
    `${seq.sequence_id} · App ${seq.app} · 产品仓 ${seq.product_repo.identifier} · 生效分支 ${seq.product_repo.line_branch}`;
  return `<section class="seq-card">
      <h2>执行范围</h2>
      <p>${esc(seq.execution_scope.statement)}</p>
      <p class="seq-note">起点 commit <span class="mono">${esc(seq.product_repo.base_commit)}</span> ·
        策略选择 ${esc(seq.execution_scope.acceptance_policy_choice)} ·
        描述版本 v${esc(seq.revision.version)}（${esc(seq.revision.changed_at)}）${seq.sealed ? " · 已封存" : ""}</p>
    </section>`
    + renderSummary(model)
    + model.items.map(renderItem).join("")
    + renderManual(model)
    + renderIntegrity(model);
}

/** 阻断：先说对象、影响、下一步；原始错误进可展开详情。 */
function renderBlocked(subject, impact, next, raw) {
  return `<div class="seq-block">
    <h2>读不到需求序列</h2>
    <dl>
      <dt>对象</dt><dd>${esc(subject)}</dd>
      <dt>影响</dt><dd>${esc(impact)}</dd>
      <dt>下一步</dt><dd>${esc(next)}</dd>
    </dl>
    <details><summary>原始错误</summary><pre>${esc(raw)}</pre></details>
  </div>`;
}

async function loadList(main) {
  try {
    const response = await fetch("/api/requirements/sequences");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const out = await response.json();
    const ids = out.sequence_ids || [];
    main.innerHTML = ids.length
      ? `<section class="seq-card"><h2>选择一条序列</h2><ul>${ids
          .map((id) => `<li><a href="?sequence=${encodeURIComponent(id)}">${esc(id)}</a></li>`).join("")}</ul>
        <p class="seq-note">序列按显式 ID 读取，与需求单列表的时间窗口无关。</p></section>`
      : `<section class="seq-card"><h2>归档里还没有序列描述</h2>
        <p class="seq-note">落点是 <span class="mono">${esc(out.root)}/&lt;sequence_id&gt;/sequence.json</span>。
        放入描述后用 <span class="mono">?sequence=&lt;id&gt;</span> 打开。</p></section>`;
  } catch (error) {
    main.innerHTML = renderBlocked(
      "序列清单接口 /api/requirements/sequences",
      "无法列出归档里的序列，页面只能靠手填 ID 打开。",
      "确认看板服务在运行；仍失败时按错误详情排查接口。",
      String(error && error.message || error),
    );
  }
}

async function load() {
  const main = document.getElementById("seqMain");
  const params = new URLSearchParams(location.search);
  const sequenceId = (params.get("sequence") || "").trim();
  document.getElementById("seqInput").value = sequenceId;
  if (!sequenceId) return loadList(main);
  try {
    const response = await fetch(`/api/requirements/sequences/${encodeURIComponent(sequenceId)}`);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body && body.detail || {};
      const code = detail.code || `http_${response.status}`;
      main.innerHTML = renderBlocked(
        `序列 ${sequenceId}`,
        code === "sequence_not_found"
          ? "归档里没有这条序列描述，页面无法显示任何阶段与计数。"
          : "序列描述没有通过校验，页面拒绝按它渲染，以免显示出与账本不一致的数字。",
        code === "sequence_not_found"
          ? "核对 evidence/requirement-sequences/ 下的目录名，或改用下面的清单选择。"
          : "按错误详情修正描述文件后重新打开；不要为了让页面出图而删字段。",
        JSON.stringify(body, null, 2),
      );
      return;
    }
    main.innerHTML = renderSequence(body);
  } catch (error) {
    main.innerHTML = renderBlocked(
      `序列 ${sequenceId}`,
      "读取请求失败，页面没有任何可显示的事实。",
      "确认看板服务在运行后重试。",
      String(error && error.message || error),
    );
  }
}

document.getElementById("seqForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = document.getElementById("seqInput").value.trim();
  location.search = id ? `?sequence=${encodeURIComponent(id)}` : "";
});
load();
