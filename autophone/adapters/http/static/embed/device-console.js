/* 远程操作嵌入页 —— 独立于看板的设备操作模块（BL-102）。
 *
 * 这个文件不引用任何 Dashboard 全局（`state`、`#overlayHost`、`toast`、`P`、`esc`、
 * `DEMO_MODE`）：画面轮询、手势、心跳、重试、关闭全部自持，页面靠 URL query 参数取
 * 设备键与模式。autophone 看板与 B 端平台都用 iframe 加载它。
 *
 * 与宿主的协议（docs/platform-device-console-embed.md §10.3）：
 * 嵌入页向宿主发 ready / device_busy / ended / error 四事件，宿主只发 stop。
 * 每条消息带 `protocol` + `version` 协议版本字段与 `embed_id` 会话标识；
 * 收发两侧都校验来源（`event.origin`）与窗口（`event.source` / 目标窗口）。
 *
 * 设备访问仍走既有 console 路由 `/api/devices/{device}/console/...`，请求带的是**会话
 * 访问凭证**（BL-103，§五）：URL 上的一次性启动凭证 `launch` 先换成会话访问凭证，
 * 之后每条请求都带 `X-Console-Access-Token` 头。rig 侧要的那把会话 token 由服务端从
 * 授权记录里取，本文件的代码不接触它。
 *
 * 幂等纪律（与原看板实现一致）：一个 event_id 最多驱动一次设备。只有网络层失败
 * （没拿到 HTTP 状态）才用同一个 event_id 重发一次；服务端已受理的会原样回同一个
 * ack，不会二次派发。拿到 HTTP 状态的错误一律不重发。
 */
(function () {
  'use strict';

  /* ── 协议常量 ─────────────────────────────────────────────────────────── */
  const PROTOCOL = 'autophone.device-console';
  const PROTOCOL_VERSION = 1;
  const HOST_MESSAGES = ['stop']; // 宿主只允许发这一种消息

  /* 硬件键封闭集，与 host 侧 vphone_uds.HARDWARE_KEYS 一致（guest 只认这四个）。 */
  const KEYS=[['home','主屏幕'],['power','电源'],['volup','音量+'],['voldown','音量−']];
  const KEY_GLYPHS = { home: '⌂', power: '⏻', volup: '🔊', voldown: '🔉' }; // 仅按钮图形，不进意图词汇
  /* 会话结束原因的可读说明；原始 reason 仍保留在括号里，宿主与测试按它判断。 */
  const END_REASONS = {
    stopped: '已手动结束', host_stop: '宿主已关闭窗口', server_closed: '服务端已关闭会话',
    access_denied: '凭证已失效', stop_failed: '结束请求失败，设备将在心跳超时后归还',
  };

  const DEMO_SHOT = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="430" height="932" viewBox="0 0 430 932"><rect width="430" height="932" fill="#eef2f7"/><rect x="25" y="90" width="380" height="120" rx="18" fill="#dbe7f6"/><rect x="25" y="240" width="380" height="300" rx="18" fill="#fff" stroke="#ccd8e6"/><text x="215" y="560" text-anchor="middle" fill="#64748b" font-family="sans-serif" font-size="20">demo console frame</text></svg>');

  /* 会话访问凭证的请求头名（服务端 `CONSOLE_ACCESS_HEADER`）与兑换口路径。 */
  const ACCESS_HEADER = 'X-Console-Access-Token';
  const EXCHANGE_PATH = '/api/access/embed-sessions';

  const FRAME_INTERVAL_MS = 200; // 每帧响应读完后再等这么久取下一帧；标签页隐藏时暂停
  const HEARTBEAT_INTERVAL_MS = 30000;
  const FRAME_ERROR_STREAK = 3; // 连续这么多帧取不到才向宿主报 error
  /* 结束请求的客户端上限：服务端交互通道单次请求最长 30s（REQUEST_TIMEOUT_S），这里留出余量；
   * 超过上限按 stop_failed 结束，页面不会停在「正在结束」。 */
  const STOP_TIMEOUT_MS = 45000;

  /* ── 入参 ─────────────────────────────────────────────────────────────── */
  const params = new URLSearchParams(location.search);
  const DEVICE = String(params.get('device') || '').trim();
  const MODE = params.get('mode') === 'view' ? 'view' : 'control';
  const DEMO_MODE = params.get('demo') === '1';
  const EMBED_ID = String(params.get('embed') || '').trim() || ('emb-' + randomId());
  const HOST_ORIGIN = resolveHostOrigin(params.get('host_origin'));
  /* 一次性启动凭证：宿主放进 URL，兑换后即失效，不留在页面的任何后续请求里。 */
  const LAUNCH_TOKEN = String(params.get('launch') || '').trim();
  if (params.has('launch')) {
    const url = new URL(location.href);
    url.searchParams.delete('launch');
    history.replaceState(history.state, '', url.pathname + url.search + url.hash);
  }

  function randomId() {
    return (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : (Date.now().toString(36) + '-' + Math.random().toString(36).slice(2));
  }

  /** 宿主来源：显式传入时取其 origin，否则用本页 origin（同源嵌入）。 */
  function resolveHostOrigin(raw) {
    const value = String(raw || '').trim();
    if (!value) return location.origin;
    try {
      return new URL(value).origin;
    } catch (e) {
      return location.origin;
    }
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  /* ── 与宿主的 postMessage ─────────────────────────────────────────────── */

  /** 只发给父窗口，且必须指定目标 origin；独立打开（无宿主）时什么都不发。 */
  function postToHost(type, payload) {
    const target = window.parent;
    if (!target || target === window) return;
    const message = Object.assign({
      protocol: PROTOCOL,
      version: PROTOCOL_VERSION,
      type: type,
      embed_id: EMBED_ID,
      session_id: S.id || null,
      device: DEVICE,
      mode: MODE,
    }, payload || {});
    try {
      target.postMessage(message, HOST_ORIGIN);
    } catch (e) { /* 宿主已关闭，忽略 */ }
  }

  window.addEventListener('message', (event) => {
    // 来源与窗口双重校验：origin 不匹配或不是父窗口发来的一律忽略。
    if (event.origin !== HOST_ORIGIN) return;
    if (event.source !== window.parent) return;
    const msg = event.data;
    if (!msg || typeof msg !== 'object') return;
    if (msg.protocol !== PROTOCOL || msg.version !== PROTOCOL_VERSION) return;
    if (msg.embed_id && msg.embed_id !== EMBED_ID) return;
    if (HOST_MESSAGES.indexOf(msg.type) < 0) return;
    if (msg.type === 'stop') releaseConsole('host_stop');
  });

  /* ── 会话状态 ─────────────────────────────────────────────────────────── */
  const S = {
    id: '',
    access: '',
    frameSeq: 0,
    busy: false,
    blobUrl: '',
    actions: 0,
    ended: false,
    endReason: '',
    stopping: false,  // 用户或宿主发起的结束请求已发出、响应未到（BL-222）
    stopPromise: null,
    ready: false,
    frameFailures: 0,
    frameTimer: 0,
    framePaused: false,
    heartbeatTimer: 0,
    frames: 0,        // 已收到的画面帧数
    lastFrameAt: 0,   // 最近一帧到达时间（performance.now）
    startedAt: 0,     // 会话建立时间，用于已用时长
    lastAction: '',   // 最近一次动作的可读描述
    tickTimer: 0,     // 每秒刷新时长与画面新鲜度
  };

  const el = {};

  function consoleBase(sessionId) {
    return '/api/devices/' + encodeURIComponent(DEVICE) + '/console'
      + (sessionId ? '/' + encodeURIComponent(sessionId) : '');
  }

  async function requestJson(path, opts) {
    const res = await fetch(path, Object.assign({ cache: 'no-store' }, opts || {}));
    if (!res.ok) {
      let detail = {};
      try { detail = await res.json(); } catch (e) { /* 非 JSON 错误体 */ }
      const d = (detail && detail.detail) || detail;
      const err = new Error((d && d.message) || ('HTTP ' + res.status));
      err.status = res.status;
      err.code = d && d.code;
      err.holder = (d && d.holder) || null;
      throw err;
    }
    return res.json();
  }

  /** 会话访问凭证进请求头；没有凭证就不加，让服务端明确回 401 而不是半带身份。 */
  function withAccess(headers) {
    const merged = Object.assign({}, headers || {});
    if (S.access) merged[ACCESS_HEADER] = S.access;
    return merged;
  }

  /** 会话已结束或结束请求进行中。此时轮询、心跳与动作拿到的 404/410/401/403 是本次结束
   * 的结果（服务端先关闭会话、撤销凭证，再返回 /stop 响应），不作为独立的结束原因。 */
  function closing() {
    return S.ended || S.stopping;
  }

  /* ── 界面 ─────────────────────────────────────────────────────────────── */

  function setStatus(text, bad, wait) {
    if (!el.status) return;
    el.status.textContent = text;
    el.status.className = 'status' + (bad ? ' status-bad' : (wait ? ' status-wait' : ''));
  }

  function fmtElapsed(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(total / 60), sec = total % 60;
    return (m < 10 ? '0' : '') + m + ':' + (sec < 10 ? '0' : '') + sec;
  }

  function endReasonText() {
    const raw = S.endReason || 'stopped';
    const label = END_REASONS[raw];
    return label ? (label + '（' + raw + '）') : raw;
  }

  /** 画面底栏：连接点、帧计数与新鲜度、光标坐标。 */
  function updateScreenBar() {
    if (!el.frameInfo) return;
    const live = !S.ended;
    if (el.liveDot) el.liveDot.className = 'dot' + (S.frameFailures >= FRAME_ERROR_STREAK ? ' bad' : (live && S.frames ? ' live' : ''));
    if (!S.frames) { el.frameInfo.textContent = live ? '等待第一帧' : '画面已停止'; return; }
    const age = performance.now() - S.lastFrameAt;
    el.frameInfo.textContent = '第 ' + S.frames + ' 帧 · ' + (age < 1000 ? Math.round(age) + 'ms 前' : (age / 1000).toFixed(1) + 's 前');
  }

  /** 会话统计：动作数、已用时长、帧数、最近动作。 */
  function updateStats() {
    if (el.statActions) el.statActions.textContent = String(S.actions);
    if (el.statElapsed) el.statElapsed.textContent = S.startedAt ? fmtElapsed(performance.now() - S.startedAt) : '--:--';
    if (el.statFrames) el.statFrames.textContent = String(S.frames);
    if (el.lastAction) el.lastAction.textContent = S.lastAction || '还没有动作';
  }

  /** 页面内提示条：嵌入页没有看板的 toast，自带一条 4 秒自动消隐的提示。 */
  let noticeTimer = 0;
  function notice(text, kind) {
    if (!el.notice) return;
    el.notice.textContent = text;
    el.notice.className = 'notice notice-' + (kind === 'error' ? 'error' : 'ok');
    el.notice.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { el.notice.hidden = true; }, 4000);
  }

  function paint() {
    const live = !S.ended;
    if (MODE === 'view') {
      setStatus(live ? (S.frames ? '只读观看中' : '连接画面中…') : ('已结束：' + endReasonText()), !live, live && !S.frames);
    } else if (S.stopping && live) {
      setStatus('正在结束会话并归还设备…', false, true);
    } else if (!S.id && live) {
      setStatus('连接中…', false, true);
    } else {
      setStatus(
        live
          ? (S.busy ? ('动作发送中 · 已发送 ' + S.actions + ' 个动作 · 动作在飞')
                    : ('操作中 · 已发送 ' + S.actions + ' 个动作'))
          : ('会话已结束：' + endReasonText()),
        !live, live && S.busy,
      );
    }
    const disabled = S.busy || !live || S.stopping;
    if (el.typeBtn) el.typeBtn.disabled = disabled;
    document.querySelectorAll('[data-key]').forEach((b) => { b.disabled = disabled; });
    if (el.stopBtn) {
      el.stopBtn.style.display = live ? 'inline-flex' : 'none';
      el.stopBtn.disabled = S.stopping;
    }
    if (el.frame) el.frame.classList.toggle('readonly', MODE !== 'control' || !live || S.stopping);
    updateStats();
    updateScreenBar();
  }

  function markReady() {
    if (S.ready) return;
    S.ready = true;
    postToHost('ready', {});
  }

  function endConsole(reason) {
    if (S.ended) return;
    S.ended = true;
    S.endReason = reason;
    clearTimeout(S.frameTimer);
    clearInterval(S.heartbeatTimer);
    clearInterval(S.tickTimer);
    paint();
    postToHost('ended', { reason: reason });
  }

  function reportError(code, message) {
    postToHost('error', { code: code, message: message || '' });
  }

  /* ── 画面 ─────────────────────────────────────────────────────────────── */

  function framePath() {
    // 绑定了 console 会话就读会话帧（凭证按会话校验）；view 模式没绑会话时读设备预览快照。
    return S.id
      ? consoleBase(S.id) + '/frame'
      : '/devices/' + encodeURIComponent(DEVICE) + '/screen.jpg?t=' + Date.now();
  }

  async function pollFrame() {
    if (closing()) return;
    if (document.hidden) { S.framePaused = true; return; }
    try {
      if (DEMO_MODE) {
        el.frame.src = DEMO_SHOT;
        S.frameSeq++;
      } else {
        const res = await fetch(framePath(), { cache: 'no-store', headers: withAccess() });
        // 这一帧在飞期间开始了结束流程：结果不再解释，结束原因由 releaseConsole 决定。
        if (closing()) return;
        if (!res.ok) {
          if (res.status === 404 || res.status === 410) { endConsole('server_closed'); return; }
          if (res.status === 401 || res.status === 403) { endConsole('access_denied'); return; }
          throw new Error('HTTP ' + res.status);
        }
        // Run 执行中、执行侧尚未取图（BL-227）：202 JSON 不是画面，保留上一帧等下一轮。
        if (res.status === 202) {
          if (el.wait) { el.wait.style.display = 'flex'; el.wait.textContent = '执行中，等待执行侧画面'; }
          if (!closing()) S.frameTimer = setTimeout(pollFrame, FRAME_INTERVAL_MS);
          return;
        }
        const blob = await res.blob();
        if (closing()) return;
        const url = URL.createObjectURL(blob);
        if (S.blobUrl) URL.revokeObjectURL(S.blobUrl);
        S.blobUrl = url;
        el.frame.src = url;
        if (MODE !== 'view') S.frameSeq = Number(res.headers.get('x-frame-seq') || 0);
        else S.frameSeq = S.frameSeq + 1;
      }
      S.frameFailures = 0;
      S.frames++;
      S.lastFrameAt = performance.now();
      if (el.wait) el.wait.style.display = 'none';
      if (S.frames === 1) paint(); else updateScreenBar();
      markReady();
    } catch (e) {
      if (closing()) return;
      S.frameFailures++;
      if (el.wait) {
        el.wait.style.display = 'flex';
        el.wait.textContent = '画面暂时不可用：' + e.message;
      }
      // 连续多帧取不到才向宿主报 error：单帧抖动不值得让宿主改 UI。
      if (S.frameFailures === FRAME_ERROR_STREAK) reportError('frame_unavailable', e.message);
      updateScreenBar();
    }
    if (!closing()) S.frameTimer = setTimeout(pollFrame, FRAME_INTERVAL_MS);
  }

  // 隐藏时 pollFrame 不再排下一帧（framePaused），重新可见后从这里续取；
  // 没暂停过说明定时器还在，不重复起循环。
  document.addEventListener('visibilitychange', () => {
    if (document.hidden || closing() || !S.framePaused) return;
    S.framePaused = false;
    pollFrame();
  });

  /* ── 手势 ─────────────────────────────────────────────────────────────── */

  function px(e, r) { return Math.max(0, Math.min(429, Math.round((e.clientX - r.left) * 430 / r.width))); }
  function py(e, r) { return Math.max(0, Math.min(931, Math.round((e.clientY - r.top) * 932 / r.height))); }

  /* 画面上的动作反馈：tap 画一圈涟漪，swipe 画一条带箭头的轨迹。坐标按 430×932 设备
   * 像素换算回画面盒子的百分比，所以和画面缩放无关。 */
  function showTap(x, y) {
    if (!el.fx) return;
    const dot = document.createElement('div');
    dot.className = 'ripple';
    dot.style.left = (x / 430 * 100) + '%';
    dot.style.top = (y / 932 * 100) + '%';
    el.fx.appendChild(dot);
    setTimeout(() => dot.remove(), 600);
  }
  function showSwipe(x1, y1, x2, y2) {
    if (!el.fx) return;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'stroke');
    svg.setAttribute('viewBox', '0 0 430 932');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.innerHTML = '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 + '" stroke="#fff" stroke-width="5" stroke-linecap="round" opacity=".9"/>'
      + '<circle cx="' + x1 + '" cy="' + y1 + '" r="10" fill="#1473e6" stroke="#fff" stroke-width="3"/>'
      + '<circle cx="' + x2 + '" cy="' + y2 + '" r="14" fill="none" stroke="#fff" stroke-width="4"/>';
    el.fx.appendChild(svg);
    setTimeout(() => svg.remove(), 750);
  }

  function bindGestures() {
    el.frame.addEventListener('pointermove', (e) => {
      if (!el.cursor) return;
      const r = el.frame.getBoundingClientRect();
      el.cursor.textContent = '(' + px(e, r) + ', ' + py(e, r) + ')';
    });
    el.frame.addEventListener('pointerleave', () => { if (el.cursor) el.cursor.textContent = '—'; });
    // view 模式只读：不绑手势，光标也不做成可点击的样子。
    if (MODE !== 'control') { el.frame.classList.add('readonly'); return; }
    let down = null;
    el.frame.addEventListener('pointerdown', (e) => {
      if (S.busy || closing()) return;
      el.frame.setPointerCapture(e.pointerId);
      const r = el.frame.getBoundingClientRect();
      down = { x: px(e, r), y: py(e, r), at: performance.now(), pointerId: e.pointerId };
    });
    el.frame.addEventListener('pointerup', (e) => {
      if (!down || down.pointerId !== e.pointerId) return;
      const r = el.frame.getBoundingClientRect();
      const x = px(e, r), y = py(e, r);
      const dx = x - down.x, dy = y - down.y;
      const ms = Math.max(120, Math.round(performance.now() - down.at));
      const start = down;
      down = null;
      if (Math.hypot(dx,dy)<20) { showTap(x, y); sendIntent({kind:'tap',px:x,py:y}); }
      else { showSwipe(start.x, start.y, x, y); sendIntent({kind:'swipe',px1:start.x,py1:start.y,px2:x,py2:y,ms:ms}); }
    });
  }

  function describeIntent(intent) {
    if (intent.kind === 'tap') return 'tap (' + intent.px + ', ' + intent.py + ')';
    if (intent.kind === 'swipe') return 'swipe (' + intent.px1 + ', ' + intent.py1 + ') → (' + intent.px2 + ', ' + intent.py2 + ')';
    if (intent.kind === 'key') { const k = KEYS.find((x) => x[0] === intent.name); return '硬件键 ' + (k ? k[1] : intent.name); }
    if (intent.kind === 'type') return '写入文本 ' + intent.text.length + ' 字';
    return intent.kind;
  }

  async function sendIntent(intent) {
    if (MODE !== 'control' || S.busy || closing() || !S.frameSeq) {
      if (MODE === 'control' && !closing() && !S.frameSeq) notice('画面还没到，等第一帧出来再操作', 'error');
      return;
    }
    S.busy = true;
    const t0 = performance.now();
    paint();
    const payload = Object.assign({ event_id: randomId(), frame_seq: S.frameSeq }, intent);
    const submit = () => requestJson(consoleBase(S.id) + '/event', {
      method: 'POST',
      headers: withAccess({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload),
    });
    try {
      if (DEMO_MODE) {
        await new Promise((r) => setTimeout(r, 150));
      } else {
        try {
          await submit();
        } catch (e) {
          // 只有「没拿到 HTTP 状态」（网络断在半路）才用同一个 event_id 重发一次：
          // 服务端若已受理会原样回同一个 ack，绝不二次驱动设备。
          if (e.status) throw e;
          await submit();
        }
      }
      S.actions++;
      S.lastAction = describeIntent(intent) + ' · ' + Math.round(performance.now() - t0) + 'ms';
    } catch (e) {
      if (e.status === 404 || e.status === 410) {
        // 结束请求进行中时会话由本页关闭，不改写结束原因。
        if (!closing()) endConsole('server_closed');
      } else if (e.code === 'stale_frame') notice('画面已过期，等新画面出来再点', 'error');
      else {
        notice('动作未生效：' + e.message, 'error');
        reportError(e.code || 'action_failed', e.message);
      }
    } finally {
      S.busy = false;
      paint();
    }
  }

  /* ── 心跳与关闭 ───────────────────────────────────────────────────────── */

  async function heartbeat() {
    if (closing() || DEMO_MODE || MODE !== 'control') return;
    try {
      await requestJson(consoleBase(S.id) + '/heartbeat', {
        method: 'POST',
        headers: withAccess(),
      });
    } catch (e) {
      if (closing()) return; // 在飞心跳的结果在结束请求开始后到达
      if (e.status === 404 || e.status === 410) endConsole('server_closed');
    }
  }

  /* 结束会话并归还设备。宿主关闭弹窗（发 stop）与页面上的「结束操作」走同一条路径：
   * 会话访问凭证只活在这个页面的内存里，页面一走就再也够不着这条会话，留着只会白占
   * 设备到心跳超时（90s）。
   *
   * 进入 stopping 后先停画面轮询与心跳，再发 /stop（BL-222）：服务端在返回 /stop 响应之前
   * 已关闭会话并撤销凭证，若轮询继续，会把 404/410/401/403 记成 server_closed /
   * access_denied 并抢先结束。stopping 期间重复调用（宿主 stop 与按钮同时触发）共用同一次请求。 */
  function releaseConsole(reason) {
    if (S.stopPromise) return S.stopPromise;
    if (S.ended) return Promise.resolve(null);
    S.stopping = true;
    clearTimeout(S.frameTimer);
    clearInterval(S.heartbeatTimer);
    paint();
    S.stopPromise = requestStop(reason);
    return S.stopPromise;
  }

  async function requestStop(reason) {
    let err = null;
    if (!DEMO_MODE && MODE === 'control' && S.id) {
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), STOP_TIMEOUT_MS);
      try {
        await requestJson(consoleBase(S.id) + '/stop', {
          method: 'POST',
          headers: withAccess(),
          signal: abort.signal,
        });
      } catch (e) {
        if (abort.signal.aborted) err = '结束请求 ' + (STOP_TIMEOUT_MS / 1000) + 's 内无响应';
        else if (e.status !== 404 && e.status !== 410) err = e.message;
      } finally {
        clearTimeout(timer);
      }
    }
    endConsole(err ? 'stop_failed' : (reason || 'stopped'));
    if (err) notice('结束失败：' + err, 'error');
    return err;
  }

  // 关标签页/刷新/宿主销毁 iframe：keepalive 让这条归还请求活过页面卸载；
  // 发不出去也有服务端心跳超时兜底。
  window.addEventListener('beforeunload', () => {
    if (S.ended || DEMO_MODE || MODE !== 'control' || !S.id) return;
    try {
      fetch(consoleBase(S.id) + '/stop', {
        method: 'POST',
        keepalive: true,
        headers: withAccess(),
      });
    } catch (e) { /* 卸载途中，无处可报 */ }
  });

  /* ── 启动 ─────────────────────────────────────────────────────────────── */

  function renderShell() {
    const control = MODE === 'control';
    document.body.innerHTML = `<div class="embed-root">
  <div class="screen-pane">
    <div class="phone"><div class="screen-box">
      <img data-frame alt="设备画面" draggable="false">
      <div class="fx" data-fx></div>
      <div data-wait class="wait">连接设备画面…</div>
    </div></div>
    <div class="screen-bar">
      <span class="dot" data-live-dot></span>
      <span data-frame-info>等待第一帧</span>
      <span class="sp"></span>
      <span>光标 <span class="mono" data-cursor>—</span></span>
    </div>
  </div>
  <div class="side-pane">
    <div class="head">
      <div class="head-row">
        <div class="title">${control ? '远程操作' : '设备画面'}</div>
        <span class="badge ${control ? 'badge-control' : 'badge-view'}">${control ? '可操作 · 独占设备' : '只读 · 不占用设备'}</span>
      </div>
      <div class="mono meta" data-meta>${esc(DEVICE)}</div>
    </div>

    <div class="card">
      <div class="card-head">会话状态</div>
      <div class="status" data-status>连接中…</div>
      <div class="stats">
        <div class="stat"><b data-stat-actions>0</b><span>已发送动作</span></div>
        <div class="stat"><b data-stat-elapsed>--:--</b><span>已用时长</span></div>
        <div class="stat"><b data-stat-frames>0</b><span>收到画面帧</span></div>
      </div>
      ${control ? '<div class="last">最近动作：<span class="mono" data-last-action>还没有动作</span></div>' : ''}
    </div>
    <div class="notice" data-notice hidden></div>

    ${control ? `<div class="card">
      <div class="card-head">怎么操作</div>
      <div class="howto">
        <div class="how"><span class="glyph">●</span><b>点击画面 = tap</b>按下即发送，画面上出现涟漪</div>
        <div class="how"><span class="glyph">⟶</span><b>拖动 ≥ 20px = swipe</b>松开即发送，画面上显示轨迹</div>
        <div class="how"><span class="glyph">⏱</span><b>一次一个动作</b>上一个动作落地前按钮暂时禁用</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">硬件键</div>
      <div class="keys">${KEYS.map((k) => `<button data-key="${k[0]}" class="key" type="button"><span class="glyph">${KEY_GLYPHS[k[0]] || ''}</span>${k[1]}</button>`).join('')}</div>
    </div>

    <div class="card">
      <div class="card-head">写入文本</div>
      <div class="steps"><span><i>1</i>在下面输入</span><span><i>2</i>点「写入」送进设备剪贴板</span><span><i>3</i>在设备输入框长按 → 粘贴</span></div>
      <div class="row">
        <input data-text class="input" placeholder="要写入设备剪贴板的文本">
        <button data-type class="btn btn-outline" type="button">写入</button>
      </div>
      <details class="more"><summary>为什么不是直接打字</summary><p>文本通过 <code>UIPasteboard</code> 写入剪贴板，不经过键盘事件，所以要在设备上粘贴一次。要把这段操作沉淀成可回放资产，请改用「录制 / 回放」页的浏览器录制。</p></details>
    </div>` : `<div class="card">
      <div class="card-head">说明</div>
      <div class="hint">只读观看：不占用设备，也不能点击或输入。画面按轮询刷新，底栏显示最近一帧的时间。</div>
    </div>`}

    <div class="foot">
      ${control ? `<div class="fine"><b>本会话不录制</b>：不产生录制目录、不落 trace。期间设备对 run 与录制互斥，结束或关闭窗口后自动归还。</div>
      <button data-stop class="btn btn-danger" type="button">结束操作并归还设备</button>` : '<div class="fine">关闭窗口即停止观看。</div>'}
    </div>
  </div>
</div>`;
    el.frame = document.querySelector('[data-frame]');
    el.fx = document.querySelector('[data-fx]');
    el.wait = document.querySelector('[data-wait]');
    el.status = document.querySelector('[data-status]');
    el.notice = document.querySelector('[data-notice]');
    el.meta = document.querySelector('[data-meta]');
    el.typeBtn = document.querySelector('[data-type]');
    el.stopBtn = document.querySelector('[data-stop]');
    el.liveDot = document.querySelector('[data-live-dot]');
    el.frameInfo = document.querySelector('[data-frame-info]');
    el.cursor = document.querySelector('[data-cursor]');
    el.statActions = document.querySelector('[data-stat-actions]');
    el.statElapsed = document.querySelector('[data-stat-elapsed]');
    el.statFrames = document.querySelector('[data-stat-frames]');
    el.lastAction = document.querySelector('[data-last-action]');
  }

  function bindControls() {
    if (MODE !== 'control') return;
    document.querySelectorAll('[data-key]').forEach((b) => {
      b.addEventListener('click', () => sendIntent({kind:'key',name:b.dataset.key}));
    });
    if (el.typeBtn) {
      el.typeBtn.addEventListener('click', () => {
        const input = document.querySelector('[data-text]');
        const text = String((input && input.value) || '');
        if (!text) { notice('请输入要写入的文本', 'error'); return; }
        const before = S.actions;
        sendIntent({kind:'type',text:text}).then(() => {
          if (S.actions > before) notice('已写入设备剪贴板，去设备输入框长按粘贴', 'ok');
        });
      });
    }
    if (el.stopBtn) {
      el.stopBtn.addEventListener('click', async () => {
        const err = await releaseConsole('stopped');
        if (!err) notice('已结束远程操作，设备已归还', 'ok');
      });
    }
  }

  /** 设备被占用时的持有者信息：兑换口已在错误体里带上，这里只补一条消息。
   * 页面此刻还没有任何凭证，不能自己去问设备状态（会被鉴权挡住）。 */
  async function busyHolder(error) {
    const holder = Object.assign({}, (error && error.holder) || {});
    holder.message = (error && error.message) || '';
    return holder;
  }

  async function start() {
    renderShell();
    bindGestures();
    bindControls();
    paint();

    if (!DEVICE) {
      setStatus('缺少设备参数', true);
      reportError('missing_device', 'device query parameter is required');
      return;
    }
    if (!LAUNCH_TOKEN && !DEMO_MODE) {
      setStatus('缺少启动凭证', true);
      reportError('missing_launch_token', 'launch query parameter is required');
      return;
    }

    // 兑换：一次性启动凭证 → 会话访问凭证。control 模式下服务端在这一步开会话，
    // controller_token 留在服务端，响应里不会有它。
    let session;
    try {
      session = DEMO_MODE
        ? { session_id: 'ctl-demo', device: DEVICE, mode: MODE }
        : await requestJson(EXCHANGE_PATH, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ launch_token: LAUNCH_TOKEN }),
        });
    } catch (e) {
      setStatus('无法开始远程操作：' + e.message, true);
      if (e.code === 'device_busy') postToHost('device_busy', { holder: await busyHolder(e) });
      else reportError(e.code || 'start_failed', e.message);
      return;
    }
    // 凭证绑定的设备与模式以服务端为准；与 URL 不符说明宿主拼错了 URL，此时页面渲染的
    // 是另一台设备/另一种权限，继续下去只会拿到一连串 403。
    if (session.device !== DEVICE || session.mode !== MODE) {
      setStatus('凭证与页面参数不一致', true);
      reportError('credential_mismatch', 'the credential is bound to another device or mode');
      return;
    }
    S.id = session.session_id || '';
    S.access = session.access_token || '';
    if (el.meta) el.meta.textContent = DEVICE + (S.id ? (' · 会话 ' + S.id) : '');
    if (MODE === 'control') S.heartbeatTimer = setInterval(heartbeat, HEARTBEAT_INTERVAL_MS);
    S.startedAt = performance.now();
    S.tickTimer = setInterval(() => { updateStats(); updateScreenBar(); }, 1000);
    paint();
    pollFrame();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
