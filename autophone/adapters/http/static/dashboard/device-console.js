/* 远程操作（device console）—— 看板侧的 iframe 宿主适配层（BL-102）。
 *
 * 画面、手势、心跳、重试、关闭全部搬到独立嵌入页 `/embed/device-console`
 * （`/static/embed/device-console.js`），这里只剩三件事：开弹窗装 iframe、
 * 收嵌入页的四个事件、关闭时向嵌入页发一条 stop。
 *
 * 本文件**不引用** Dashboard 全局 `state`、`#overlayHost`、`toast`、`P`、`esc`、
 * `DEMO`：同一个嵌入页 B 端也要用，宿主适配层不能把看板的私有约定带进协议。
 * 提示改用本文件自己的浮层（`devconNotice`），弹窗挂在 `document.body` 上。
 *
 * 鉴权（BL-103，§五）：宿主在开弹窗前先取一次性**启动凭证**（`POST /api/access/launch-tokens`，
 * 走看板既有 Basic Auth 或开发模式），把它拼进 iframe URL 的 `launch` 参数；嵌入页再拿它换
 * 会话访问凭证。长期凭证不进 URL，`controller_token` 不进浏览器。
 *
 * 协议（docs/platform-device-console-embed.md §10.3）：
 * 嵌入页 → 宿主：ready / device_busy(附持有者) / ended(附 reason) / error(附 code)；
 * 宿主 → 嵌入页：只有 stop。每条消息带协议版本字段与会话标识 `embed_id`，
 * 收发两侧都校验来源（`event.origin`）与窗口（`event.source` / 目标窗口）。
 */
const DEVCON_PROTOCOL='autophone.device-console';
const DEVCON_PROTOCOL_VERSION=1;
/* 嵌入页发过来的事件封闭集：不在集合里的一律忽略。 */
const DEVCON_EVENTS=['ready','device_busy','ended','error'];
/* 发出 stop 后等 ended 的上限；超时就直接拆 iframe（嵌入页的 beforeunload
 * keepalive 仍会补发一次归还请求，服务端心跳超时兜底）。 */
const DEVCON_STOP_WAIT_MS=1500;

/* 当前弹窗；同一时刻只允许一个。 */
let devconHost=null;

function devconEscape(s){ return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

/* 与 core.js 的离线预览判据同源（URL ?demo=1 或 localStorage.ap_demo）。这里重算
 * 一遍而不是读看板全局：嵌入页的 demo 由 URL 参数驱动，宿主只负责把标记传下去。 */
function devconDemoMode(){
  if(document.getElementById('rig')) return false;
  try{
    if(new URLSearchParams(location.search).get('demo')==='1')return true;
    return localStorage.getItem('ap_demo')==='1';
  }catch(e){ return false; }
}

function devconEmbedUrl(device,mode,embedId,launchToken){
  const q=new URLSearchParams({device:device,mode:mode||'control',embed:embedId,host_origin:location.origin});
  if(launchToken)q.set('launch',launchToken);
  if(devconDemoMode())q.set('demo','1');
  return '/embed/device-console?'+q.toString();
}

/* 取一次性启动凭证。离线预览不取（嵌入页 demo 分支不兑换）。 */
async function devconLaunchToken(device,mode){
  if(devconDemoMode())return '';
  const res=await fetch('/api/access/launch-tokens',{
    method:'POST',cache:'no-store',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({device:device,mode:mode||'control'}),
  });
  if(!res.ok){
    let detail={};
    try{detail=await res.json();}catch(e){}
    const d=(detail&&detail.detail)||detail;
    throw new Error((d&&d.message)||('HTTP '+res.status));
  }
  const data=await res.json();
  return String(data.launch_token||'');
}

/* 页面级提示条：看板的 toast 是 Dashboard 私有全局，宿主适配层不引用它。 */
let devconNoticeTimer=0;
function devconNotice(text,kind){
  let box=document.querySelector('#deviceConsoleNotice');
  if(!box){
    box=document.createElement('div');
    box.id='deviceConsoleNotice';
    box.style.cssText='position:fixed;left:50%;top:18px;transform:translateX(-50%);z-index:120;max-width:min(520px,92vw);padding:10px 16px;border-radius:10px;font-size:13px;font-weight:600;box-shadow:0 10px 30px rgba(0,0,0,.18);';
    document.body.appendChild(box);
  }
  box.textContent=text;
  box.style.background=kind==='error'?'#fdecec':'#e8f2fe';
  box.style.color=kind==='error'?'#d7373f':'#0d66d0';
  clearTimeout(devconNoticeTimer);
  devconNoticeTimer=setTimeout(()=>{ const n=document.querySelector('#deviceConsoleNotice'); if(n)n.remove(); },4000);
}

function devconSetChrome(text,bad){
  if(!devconHost)return;
  const el=devconHost.overlay.querySelector('[data-devcon-chrome]');
  if(!el)return;
  el.textContent=text;
  el.style.background=bad?'rgba(215,55,63,.92)':'rgba(8,12,18,.72)';
  el.hidden=false;
}

function closeDeviceConsoleOverlay(){
  const h=devconHost;
  if(!h)return;
  devconHost=null;
  clearTimeout(h.stopTimer);
  window.removeEventListener('message',h.onMessage);
  h.overlay.remove();
  if(h.onClose)h.onClose();
  if(typeof render==='function')render();
}

/* 宿主 → 嵌入页的唯一一条消息。目标窗口取 iframe 的 contentWindow，目标 origin
 * 显式写死本页 origin（同源嵌入），不用 '*'。 */
function devconPostStop(){
  const h=devconHost;
  if(!h)return;
  const win=h.frame&&h.frame.contentWindow;
  if(!win)return;
  try{
    win.postMessage({
      protocol:DEVCON_PROTOCOL,version:DEVCON_PROTOCOL_VERSION,
      type:'stop',embed_id:h.embedId,
    },location.origin);
  }catch(e){}
}

/* 关弹窗 = 让嵌入页结束会话并归还设备。等它回 ended 再拆 iframe；超时兜底。 */
function requestDeviceConsoleStop(){
  const h=devconHost;
  if(!h||h.stopping)return;
  h.stopping=true;
  devconSetChrome('正在结束操作并归还设备…',false);
  devconPostStop();
  h.stopTimer=setTimeout(()=>{
    devconNotice('结束请求超时，设备将在心跳超时后归还','error');
    closeDeviceConsoleOverlay();
  },DEVCON_STOP_WAIT_MS);
}

function devconHolderText(holder){
  if(!holder)return '';
  const parts=[];
  if(holder.owner_kind)parts.push(holder.owner_kind);
  if(holder.session_id)parts.push(holder.session_id);
  if(holder.started_at)parts.push('自 '+holder.started_at);
  return parts.length?parts.join(' · '):String(holder.message||'');
}

/* 嵌入页事件的唯一入口：先校验来源与窗口，再按事件名分派。 */
function devconHandleMessage(event){
  const h=devconHost;
  if(!h)return;
  if(event.origin!==location.origin)return;
  if(!h.frame||event.source!==h.frame.contentWindow)return;
  const msg=event.data;
  if(!msg||typeof msg!=='object')return;
  if(msg.protocol!==DEVCON_PROTOCOL||msg.version!==DEVCON_PROTOCOL_VERSION)return;
  if(msg.embed_id!==h.embedId)return;
  if(DEVCON_EVENTS.indexOf(msg.type)<0)return;
  if(msg.type==='ready'){
    h.sessionId=msg.session_id||'';
    devconSetChrome('操作中 · '+h.device+(h.sessionId?(' · '+h.sessionId):''),false);
    return;
  }
  if(msg.type==='device_busy'){
    const who=devconHolderText(msg.holder);
    devconNotice('设备正被占用，无法远程操作'+(who?('：'+who):''),'error');
    closeDeviceConsoleOverlay();
    return;
  }
  if(msg.type==='ended'){
    const reason=String(msg.reason||'stopped');
    if(h.stopping)devconNotice('已结束远程操作，设备已归还','ok');
    else devconNotice('远程操作已结束：'+reason,reason==='stopped'?'ok':'error');
    closeDeviceConsoleOverlay();
    return;
  }
  if(msg.type==='error'){
    const code=String(msg.code||'error');
    devconSetChrome('连接异常：'+code,true);
    devconNotice('远程操作连接异常：'+code,'error');
  }
}

async function openDeviceConsole(device, onClose){
  closeDeviceConsoleOverlay();
  const target=String(device||window.AUTOPHONE_DEVICE||'').trim();
  if(!target){ devconNotice('没有可操作的设备','error'); return; }
  let launchToken='';
  try{
    launchToken=await devconLaunchToken(target,'control');
  }catch(e){
    devconNotice('无法取得远程操作凭证：'+e.message,'error');
    return;
  }
  const embedId='emb-'+((window.crypto&&crypto.randomUUID)?crypto.randomUUID():(Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)));
  const ov=document.createElement('div');
  ov.id='deviceConsoleOverlay';
  ov.style.cssText='position:fixed;inset:0;z-index:95;background:rgba(8,12,18,.82);display:flex;align-items:center;justify-content:center;padding:14px;';
  // 面板尺寸与改造前的弹窗一致；iframe 铺满面板，宿主的关闭按钮与状态条浮在其上。
  ov.innerHTML=`<div style="position:relative;width:min(900px,98vw);height:min(900px,96vh);background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.45);">
    <iframe data-devcon-frame title="远程操作设备" src="${devconEscape(devconEmbedUrl(target,'control',embedId,launchToken))}" style="width:100%;height:100%;border:0;display:block;"></iframe>
    <div data-devcon-chrome hidden style="position:absolute;left:12px;top:12px;max-width:calc(100% - 64px);padding:6px 11px;border-radius:8px;background:rgba(8,12,18,.72);color:#fff;font-size:11.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></div>
    <button data-action="devcon-close" aria-label="关闭并归还设备" style="position:absolute;right:12px;top:12px;border:0;background:rgba(8,12,18,.72);color:#fff;border-radius:8px;width:32px;height:32px;font-size:16px;line-height:1;cursor:pointer;">×</button>
  </div>`;
  document.body.appendChild(ov);
  devconHost={
    device:target,embedId:embedId,overlay:ov,
    frame:ov.querySelector('[data-devcon-frame]'),
    sessionId:'',stopping:false,stopTimer:0,
    onMessage:devconHandleMessage, onClose,
  };
  window.addEventListener('message',devconHost.onMessage);
  devconSetChrome('连接嵌入页…',false);
  return true;
}

/* 关标签页 / 刷新：嵌入页自己有 keepalive 归还，这里只补一条 stop，让它有机会
 * 在同一个卸载周期里发出去。 */
window.addEventListener('beforeunload',()=>{ devconPostStop(); });

if(typeof onAction==='function'){
  onAction('devcon-close',requestDeviceConsoleStop);
  onAction('device-console-open',el=>openDeviceConsole(el.dataset.device));
}else{
  document.addEventListener('click',event=>{
    if(event.target.closest('[data-action="devcon-close"]')) requestDeviceConsoleStop();
  });
}
