'use strict';
// 纯决策层：delegate 型 task 短路的「编排/可行性」逻辑（无 frida 依赖 → 可 node --test）。
//
// 背景（docs/api-scenario-interception.md §6.2）：completion-block 型 task 能直接合成回调
// （invokeCompletion 调那份 Block_copy）；但 delegate 型 task —— RN 的 RCTHTTPRequestHandler
// 走 `dataTaskWithRequest:`（无 completionHandler），响应经 NSURLSessionDataDelegate 分片下发
// —— 没有块可调。要短路它，只能**驱动 app 自己的 delegate 回调**：按序调
//   didReceiveResponse: → didReceiveData: → didCompleteWithError:
// 喂合成对象、且**不发真网络**（不调原始 resume）。这是「无 completion block 就放行」缺口
// （applyRule 的 no_completion_block 分支）的正解，也是 RN 走 delegate 的接口能被
// 拦下的前提。
//
// 本模块只产「计划」——要调哪些 selector、按什么序、带什么合成载荷——并判可行性（fail-closed）；
// 真正的 ObjC 调用在 net_tap.js 的 executor（frida 绑定，活机验证）。切两层的原因：决策纯、可测；
// 效果侧脏、只能活机验（threading/队列、合成对象、app 内部 task↔delegate 映射都要真机才算数）。

export const SEL_RESPONSE = 'URLSession:dataTask:didReceiveResponse:completionHandler:';
export const SEL_DATA = 'URLSession:dataTask:didReceiveData:';
export const SEL_COMPLETE = 'URLSession:task:didCompleteWithError:';

// step.kind → 该步依赖 app delegate 实现的 selector。
const STEP_SELECTOR = {
  response: SEL_RESPONSE,
  data: SEL_DATA,
  complete: SEL_COMPLETE,
};

// 按 action 类型产出有序 step 列表（纯函数）。
//   fixed_json：response(status/headers) → data(body) → complete(无错)
//   timeout   ：只 complete(NSURLErrorTimedOut=-1001)——立即超时、无响应体（§一 第一期）
// 未知/缺省类型按 fixed_json 处理（上游 rules.py 已白名单校验，这里只兜底）。
export function planDelegateSteps(action) {
  const type = (action && action.type) || 'fixed_json';
  if (type === 'timeout') {
    return [{ kind: 'complete', error: { domain: 'NSURLErrorDomain', code: -1001 } }];
  }
  const status = action && Number.isInteger(action.status) ? action.status : 200;
  const headers = (action && action.headers) || {};
  const body = action && action.body != null ? String(action.body) : '';
  return [
    { kind: 'response', status, headers },
    { kind: 'data', body },
    { kind: 'complete', error: null },
  ];
}

// 一个计划所需的 delegate selector 全集（去重、保序）。
export function requiredSelectors(steps) {
  const out = [];
  for (const s of steps) {
    const sel = STEP_SELECTOR[s.kind];
    if (sel && out.indexOf(sel) < 0) out.push(sel);
  }
  return out;
}

// 可行性判定（fail-closed）：delegate 必须实现该计划**所有** step 依赖的 selector，否则无法
// 安全走完请求——缺 didCompleteWithError: 会把请求吊死（比放行真网络更糟）、fixed_json 缺
// didReceiveResponse:/didReceiveData: 会丢 status/body 交给 app 一个残缺响应。任一缺失即
// 判不可行，调用方诚实记 applied:false 并放行真网络（与 completion 路的 no_completion_block 同策）。
//
// 注：didReceiveResponse: 在 NSURLSessionDataDelegate 里是 optional（不实现＝系统默认 Allow），
// 但我们**要求 fixed_json 必须实现它**——因为我们要靠它把合成的 status/headers 交给 app，缺了
// 就只能给 app 一个无状态码的响应。RN 的 RCTHTTPRequestHandler 三个都实现，故实际不受限。
//
// respondsTo: (selector:string) => boolean —— 由调用方注入（活机是 delegate.respondsToSelector_）。
export function planDelegateShortCircuit(action, respondsTo) {
  const steps = planDelegateSteps(action);
  const selectors = requiredSelectors(steps);
  const missing = selectors.filter((sel) => !respondsTo(sel));
  return { applicable: missing.length === 0, steps, selectors, missing };
}
