import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

function harness() {
  const source=readFileSync(new URL('./net_tap.js',import.meta.url),'utf8');
  const sent=[];
  const c={SCENARIO_RULES:[{rule_id:'r',match:{method:'GET',host:'a.test',path:'/p',exact_url:'https://a.test/p?region=100100'}}],SCENARIO_COUNTS:{},send:e=>sent.push(e)};
  vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function matchRule('),source.indexOf('// 合成一个 NSHTTPURLResponse')),c);
  return c;
}
test('exact_url refuses another query despite identical method host and path',()=>{
  const c=harness();assert.equal(c.matchRule({method:'GET',host:'a.test',url:'https://a.test/p?city=310100'}),null);
  assert.equal(c.matchRule({method:'GET',host:'a.test',url:'https://a.test/p?region=100100'}).rule_id,'r');
});

async function driver(options={}) {
  const {deliverLocal}=await import('./controlled_delivery.mjs');
  const events=[],calls=[];let ack;
  const dependencies={via:'delegate',emit:(phase,data={})=>events.push({phase,...data}),
    makeResponse:()=>({status:200}),makeData:()=>({bytes:'abc'}),
    readData:()=>({b64:'YWJj',len:3,truncated:false}),
    response:(response,callback)=>{calls.push('response');ack=callback;},
    data:()=>calls.push('data'),complete:()=>calls.push('complete'),
    completion:()=>calls.push('completion'),success:()=>calls.push('success'),release:()=>calls.push('release'),
    ...options};
  const result=deliverLocal(dependencies);
  return {events,calls,result,ack:()=>ack};
}
test('async Allow waits then emits observed body and completed after callbacks',async()=>{
  const d=await driver();assert.deepEqual(d.calls,['response']);assert.equal(d.result.pending,true);
  d.ack()(1);assert.deepEqual(d.calls,['response','data','complete','success','release']);
  assert.deepEqual(d.events.map(e=>e.phase),['started','response_disposition','effective_body','completed']);
  assert.equal(d.result.pending,false);assert.equal(d.events[2].body_b64,'YWJj');
});
test('non Allow, missing synthesized objects, callback exception never complete',async()=>{
  for(const mode of ['deny','no_response','no_data','throw']) {
    const d=await driver(mode==='no_response'?{makeResponse:()=>null}:mode==='no_data'?{makeData:()=>null}:mode==='throw'?{data:()=>{throw Error('private');}}:{});
    if(d.ack())d.ack()(mode==='deny'?0:1);
    assert.equal(d.events.at(-1).phase,'failed');assert.ok(!d.calls.includes('success'));
    assert.ok(!JSON.stringify(d.events).includes('private'));
  }
});
test('synchronous Allow still waits until response invocation returns',async()=>{
  const order=[];const d=await driver({response:(_r,ack)=>{order.push('response');ack(1);order.push('returned');},data:()=>order.push('data')});
  assert.deepEqual(order,['response','returned','data']);assert.equal(d.events.at(-1).phase,'completed');
});
test('completion path observes bytes and waits for real callback return',async()=>{
  const d=await driver({via:'completion'});assert.deepEqual(d.calls,['completion','success','release']);
  assert.equal(d.events.at(-1).phase,'completed');
  const failed=await driver({via:'completion',completion:()=>{throw Error('callback');}});
  assert.equal(failed.events.at(-1).phase,'failed');assert.ok(!failed.calls.includes('success'));
});
test('duplicate disposition cannot replay callbacks',async()=>{
  const d=await driver();d.ack()(1);d.ack()(1);
  assert.equal(d.calls.filter(c=>c==='data').length,1);assert.equal(d.events.at(-1).phase,'failed');
});

async function nativeHarness({via='delegate',demux=false,missing=false,async=false,queueMode=null,failure=null}={}) {
  const {deliverLocal}=await import('./controlled_delivery.mjs');
  const source=readFileSync(new URL('./net_tap.js',import.meta.url),'utf8');
  const begin=source.indexOf('function applyControlledRule('),end=source.indexOf('function applyRule(',begin);
  assert.ok(begin>=0&&end>begin,'controlled native adapter missing');
  const calls=[],sent=[],queued=[],objects=[];let ack;
  const owned=(name,initial)=>{const obj={handle:name,refs:initial,isNull:()=>false,retain(){assert.ok(this.refs>0);this.refs++;return this;},release(){assert.ok(this.refs>0);this.refs--;}};objects.push(obj);return obj;};
  const live=()=>{for(const obj of objects)assert.ok(obj.refs>0,'native object alive during callback');};
  const native=object=>Object.assign(object,{isNull:()=>false,retain(){calls.push('retain');return this;},release(){calls.push('release');}});
  const delegate=native({$className:demux?'FixtureSessionDemux':'NativeDelegate',respondsToSelector_:()=>!missing,
    URLSession_dataTask_didReceiveResponse_completionHandler_(_s,_t,_r,block){live();calls.push('response');ack=block.implementation;if(!async)ack(1);},
    URLSession_dataTask_didReceiveData_(){live();if(failure==='data')throw Error('data');calls.push('data');},URLSession_task_didCompleteWithError_(){calls.push('complete');}});
  const queue=queueMode?native({addOperationWithBlock_(){calls.push('enqueued');if(queueMode==='throw')throw Error('queue');}}):null;
  const session=native({delegate:()=>delegate,delegateQueue:()=>queue});
  const task=native({session:()=>session,cancel:()=>calls.push('cancel')});
  const record={task_ptr:'0x1',task_instance_id:'instance1'};
  const req=native({URL:()=>({})});
  const c={deliverLocal,SCENARIO_COUNTS:{},SCENARIO_HASH:'sha256:rules',CONTROLLED_DELIVERIES:{},
    TASK_BY_PTR:{'0x1':record},TASK_INSTANCES:{instance1:record},COMPLETION_BLOCKS:via==='completion'?{'0x1':'block'}:{},
    ObjC:{Object:function(){return task;},Block:function(options){return options;},selector:x=>x},
    NULL:null,ptr:x=>x,Date,setTimeout:fn=>queued.push(fn),
    retainBlock:b=>b,releaseBlock:()=>calls.push('releaseBlock'),invokeCompletion:()=>{live();calls.push('completion');return failure!=='completion';},
    emitAgentEvent:e=>sent.push(e),emitTaskLifecycle:(_t,kind)=>sent.push({ev:'task_lifecycle',kind}),
    synthResponse:()=>owned('response',1),synthData:()=>failure==='synthesis'?null:owned('data',1),
    dataBytes:()=>({b64:'e30=',len:2,truncated:false})};
  vm.createContext(c);vm.runInContext(source.slice(begin,end),c);
  const result=c.applyControlledRule({rule_id:'r',action:{type:'fixed_json',status:200,body:'{}'}},'0x1',req);
  const run=()=>{while(queued.length)queued.shift()();};
  return {c,result,record,sent,calls,objects,run,ack:()=>ack};
}
for(const via of ['completion','delegate'])test(`controlled ${via} emits identity route then hit and observed delivery`,async()=>{
  const d=await nativeHarness({via});assert.equal(d.result,true);assert.equal(d.record.delivery_pending,true);
  d.run();assert.equal(d.record.delivery_pending,false);assert.equal(d.record.synthetic_callback_complete,true);
  assert.deepEqual(d.sent.filter(e=>e.ev.startsWith('scenario')).map(e=>e.ev==='scenario_delivery'?e.phase:e.ev),
    ['scenario_route','scenario_hit','started',...(via==='delegate'?['response_disposition']:[]),'effective_body','completed']);
  assert.ok(d.sent.filter(e=>e.ev.startsWith('scenario')).every(e=>e.task_instance_id==='instance1'&&e.controlled===true));
  assert.ok(!d.calls.includes('cancel'));
});
test('controlled demux and missing selectors fail without real override',async()=>{
  for(const options of [{demux:true},{missing:true}]){
    const d=await nativeHarness(options);assert.equal(d.result,false);d.run();
    assert.ok(!d.calls.includes('response'));assert.ok(!d.sent.some(e=>e.phase==='completed'));
  }
});
test('a fixed response for a demultiplexing delegate stays unapplied', () => {
  const source = readFileSync(new URL('./net_tap.js', import.meta.url), 'utf8');
  const delegate = {$className: 'FixtureSessionDemux', isNull: () => false};
  const session = {isNull: () => false, delegate: () => delegate, delegateQueue: () => null};
  const events = [];
  const context = vm.createContext({
    ObjC: {Object: function () { return {session: () => session}; }},
    planDelegateShortCircuit: () => ({applicable: true}),
    emitAgentEvent: event => events.push(event)
  });
  const helper = source.slice(source.indexOf('function isDemultiplexingDelegate('), source.indexOf('function applyRule('));
  const delivery = source.slice(source.indexOf('function applyViaDelegate('), source.indexOf('function deliveryEvent('));
  vm.runInContext(helper + delivery, context);
  const applied = context.applyViaDelegate({rule_id: 'r', action: {type: 'fixed_json'}}, 'p', null, 'fixed_json');
  assert.equal(applied, false);
  assert.equal(events.length, 1);
  assert.equal(events[0].applied, false);
  assert.equal(events[0].reason, 'delegate_demux_unsupported');
});

test('controlled async adapter remains pending and retains native objects until Allow',async()=>{
  const d=await nativeHarness({async:true});d.run();
  assert.equal(d.record.delivery_pending,true);assert.ok(!d.calls.includes('release'));
  d.ack()(1);d.run();assert.equal(d.record.delivery_pending,false);assert.ok(d.calls.includes('release'));
});

test('completion forwarding retains task until post-callback native observation',()=>{
  const source=readFileSync(new URL('./net_tap.js',import.meta.url),'utf8');
  const start=source.indexOf('function forwardObservedCompletion('),end=source.indexOf("['- dataTaskWithRequest:completionHandler:'",start);
  assert.ok(start>=0&&end>start,'lifetime-safe completion forwarding missing');
  const calls=[];const tracked={};let retained=false;
  const c={TASK_INSTANCES:{instance:tracked},COMPLETION_BLOCKS:{},ObjC:{Object:function(){return {retain(){retained=true;calls.push('retain');},release(){retained=false;calls.push('release');}};}},
    ptr:x=>x,emitResp:()=>calls.push('resp'),emitAgentEvent:()=>{},releaseBlock:()=>{},
    emitTaskLifecycle:()=>{assert.equal(retained,true);calls.push('lifecycle');}};
  vm.createContext(c);vm.runInContext(source.slice(start,end),c);
  c.forwardObservedCompletion({task_ptr:'0x1',task_instance_id:'instance'},()=>{assert.equal(retained,true);calls.push('original');},null,null,null);
  assert.deepEqual(calls,['retain','resp','original','lifecycle','release']);
});

test('delegate done observation retains task across native callback and releases after state snapshot',()=>{
  const source=readFileSync(new URL('./net_tap.js',import.meta.url),'utf8');
  const start=source.indexOf('function hookDelegateImpl('),end=source.indexOf('// 只读抓取用的 delegate 观察 hook',start);
  let hook,held=false;const calls=[];
  const c={DELEGATE_CAPTURE_HOOKS:{},Interceptor:{attach:(_i,callbacks)=>{hook=callbacks;}},guardDelegateCallbacks:(_k,callbacks)=>callbacks,
    CAPTURING:true,capturePolicyForTask:()=> 'metadata',TASK_BY_PTR:{'0x1':{}},pendingFor:()=>({is_menu:false,status:200,headers:{},body:{bodyLength:0}}),
    ObjC:{Object:function(){return {retain(){held=true;calls.push('retain');},release(){held=false;calls.push('release');}};}},
    isMenuListUrl:()=>false,taskRequestUrl:()=>'',PENDING:{},forgetCapturePolicy:()=>{},
    emitAgentEvent:e=>calls.push(e.ev),emitTaskLifecycle:()=>{assert.equal(held,true);calls.push('lifecycle');},Date};
  vm.createContext(c);vm.runInContext(source.slice(start,end),c);c.hookDelegateImpl({implementation:'done'},'done');
  const invocation={};hook.onEnter.call(invocation,[null,null,null,'0x1',null]);
  assert.equal(held,true);assert.ok(!calls.includes('lifecycle'));
  hook.onLeave.call(invocation);assert.equal(held,false);assert.deepEqual(calls,['retain','resp','lifecycle','release']);
});

for (const controlled of [false,true]) test(`matched unavailable route controlled=${controlled} preserves resume policy`,()=>{
  const source=readFileSync(new URL('./net_tap.js',import.meta.url),'utf8');
  const start=source.indexOf('function installScenarioResumeHook('),end=source.indexOf('// ── 响应侧（completion 路）',start);
  const calls=[];let replacement;
  const c={RESUME_HOOK_MODE:'none',RESUME_LISTENER:null,RESUME_HOOK_OK:false,SCENARIO_RULES:[{}],SCENARIO_CONTROLLED:controlled,
    C:{NSURLSessionTask:{'- resume':{implementation:'resume'}}},
    NativeFunction:function(){return ()=>calls.push('original');},NativeCallback:function(fn){return fn;},
    Interceptor:{replace:(_impl,fn)=>replacement=fn},observeResumeRequest:()=>({req:{},meta:{}}),
    matchRule:()=>({}),applyRule:()=>false,emitAgentEvent:()=>{},ObjC:{Object:function(){return {cancel:()=>calls.push('cancel')};}}};
  vm.createContext(c);vm.runInContext(source.slice(start,end),c);c.installScenarioResumeHook();replacement('task','resume');
  assert.deepEqual(calls,controlled?['cancel']:['original']);
});

test('controlled configuration and health echo compiler hash, process and policy',()=>{
  const source=readFileSync(new URL('./net_tap.js',import.meta.url),'utf8');
  const start=source.indexOf('rpc.exports = {'),end=source.indexOf("emitAgentEvent({ ev: 'loaded'",start);
  const events=[];const c={installCaptureLifecycleHooks:()=>{},sealCapture:()=>{},rpc:{},C:{NSURLSessionTask:{},NSURLSession:{}},Process:{id:42},RESUME_HOOK_OK:true,
    RESUME_HOOK_MODE:'replace',_Block_copy:true,_Block_release:true,SCENARIO_CONTROLLED:false,SCENARIO_HASH:null,
    SCENARIO_RULES:[],SCENARIO_MODE:null,SCENARIO_COUNTS:{},COMPLETION_BLOCKS:{},
    installScenarioResumeHook:()=>{},emitAgentEvent:e=>events.push(e),Date};
  vm.createContext(c);vm.runInContext(source.slice(start,end),c);
  const ack=c.rpc.exports.configureScenario({rules:[{}],mode:'spawn',controlled:true,hash:'sha256:frozen'});
  for(const e of [ack,c.rpc.exports.scenarioHealth(),events[0]]) {
    assert.equal(e.controlled,true);assert.equal(e.hash,'sha256:frozen');assert.equal(e.pid,42);
  }
});

test('enqueued but unexecuted delegate work stays pending; enqueue errors never complete',async()=>{
  const d=await nativeHarness({queueMode:'pending'});d.run();
  assert.equal(d.result,true);assert.equal(d.record.delivery_pending,true);
  assert.deepEqual(d.sent.map(e=>e.ev),['scenario_route','scenario_hit']);
  assert.ok(!d.calls.includes('release'));
  const failed=await nativeHarness({queueMode:'throw'});failed.run();
  assert.equal(failed.result,false);assert.equal(failed.sent.at(-1).phase,'failed');
});

test('delayed delivery retains registered instance identity if pointer lookup now names another task',async()=>{
  const d=await nativeHarness({async:true});d.run();
  d.c.TASK_BY_PTR['0x1']={task_ptr:'0x1',task_instance_id:'other-instance'};
  d.ack()(1);d.run();
  assert.ok(d.sent.filter(e=>e.ev.startsWith('scenario')).every(e=>e.task_instance_id==='instance1'));
  assert.equal(d.record.synthetic_callback_complete,true);
  assert.equal(d.c.TASK_BY_PTR['0x1'].synthetic_callback_complete,undefined);
});

for (const options of [{via:'completion'}, {}, {async:true}, {failure:'data'},
  {via:'completion',failure:'completion'}, {failure:'synthesis'}, {async:true,failure:'deny'}]) {
  test(`native synthesis balances owned response and autoreleased data ${JSON.stringify(options)}`,async()=>{
    const d=await nativeHarness(options);d.run();
    if(options.async){
      // Drain the native autorelease pool while the disposition callback is pending.
      const data=d.objects.find(o=>o.handle==='data');data.release();
      assert.ok(d.objects.every(o=>o.refs>0));
      d.ack()(options.failure==='deny'?0:1);
      assert.ok(d.objects.every(o=>o.refs>0),'release must wait for active callback return');
      d.run();
    } else {
      const data=d.objects.find(o=>o.handle==='data');if(data)data.release();
    }
    assert.ok(d.objects.length>0);
    assert.deepEqual(d.objects.map(o=>[o.handle,o.refs]),options.failure==='synthesis'
      ? [['response',0]] : [['response',0],['data',0]]);
    assert.equal(Object.keys(d.c.CONTROLLED_DELIVERIES).length,0);
    assert.equal(d.sent.at(-1).phase,options.failure?'failed':'completed');
  });
}
