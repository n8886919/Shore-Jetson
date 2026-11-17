const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const elements=new Map(), listeners={}, requests=[];let interval, observer, now=0, delayedStart=null, startFailure='';
function element(){return {textContent:'',disabled:false,hidden:false,classList:{toggle(){}},attrs:{'aria-hidden':'false'},addEventListener(event,fn){this[event]=fn},getAttribute(name){return this.attrs[name]}};}
const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
let status={available:true,calibrated:true,connected:false,running:false,epoch:0,phase:'idle',notice:'',control_token:'test-token',tracking_active:false};
const context=vm.createContext({document:{getElementById:get,hidden:false,addEventListener:(event,fn)=>listeners[event]=fn},window:{addEventListener:(event,fn)=>listeners[event]=fn},MutationObserver:class{constructor(fn){observer=fn}observe(){}},Date:{now:()=>now},AbortSignal,JSON,
setInterval:fn=>interval=fn,fetch:async(path,options={})=>{
  if(!options.method)return {ok:true,json:async()=>({...status})};
  const body=JSON.parse(options.body);requests.push(body);
  if(body.action==='start'){
    if(startFailure)return {ok:false,json:async()=>({ok:false,detail:startFailure})};
    status={...status,running:true,connected:true,phase:'center',notice:'置中'};
    const result={ok:true,status:{...status},session:'session'};
    if(delayedStart)return {ok:true,json:()=>new Promise(resolve=>delayedStart=()=>resolve(result))};
    return {ok:true,json:async()=>result};
  }
  if(body.action==='stop')status={...status,running:false,connected:false,epoch:status.epoch+1,phase:'stopped',notice:'測試已停止'};
  return {ok:true,json:async()=>({ok:true,status:{...status}})};
}});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/servo-test.js','utf8'),context);await tick();
  assert.equal(requests.length,0,'load/poll never starts or moves the servos');
  assert.equal(get('servoTestButton').disabled,false);
  assert.equal(get('servoTestState').hidden,true);
  await get('servoTestButton').click();
  assert.equal(requests.at(-1).action,'start');
  assert.equal(get('servoTestButton').textContent,'置中\n停止測試');
  assert.equal(get('servoTestButton').disabled,false);
  assert.equal(get('servoTestState').hidden,true);
  for(const [phase,label] of [['left','左 20°'],['right','右 40°'],['up','抬頭 40°'],['down','低頭 40°']]){
    status.phase=phase;status.notice='完整流程說明不應顯示在按鈕下方';await interval();
    assert.equal(get('servoTestButton').textContent,`${label}\n停止測試`);
    assert.equal(get('servoTestState').hidden,true,'only the short action belongs on the progress button');
  }
  assert.equal(requests.at(-1).action,'heartbeat');
  assert.equal(requests.at(-1).session,'session');
  await get('servoTestButton').click();
  assert.equal(requests.at(-1).action,'stop','the progress button must stop the test');
  assert.equal(get('servoTestButton').textContent,'雲台測試');
  await get('servoTestButton').click();
  get('controlsPanel').attrs['aria-hidden']='true';observer();await tick();
  assert.equal(requests.at(-1).action,'stop');
  assert.equal(get('servoTestButton').textContent,'雲台測試');
  get('controlsPanel').attrs['aria-hidden']='false';
  delayedStart=true;const starting=get('servoTestButton').click();await tick();
  assert.equal(get('servoTestButton').textContent,'啟動中\n停止測試');
  assert.equal(get('servoTestButton').disabled,false,'STOP remains available while START is pending');
  get('controlsPanel').attrs['aria-hidden']='true';observer();await tick();
  delayedStart();await starting;
  assert.equal(get('servoTestButton').textContent,'雲台測試','late start response must not revive a dismissed test');
  assert.equal(get('servoTestState').hidden,true);
  delayedStart=null;get('controlsPanel').attrs['aria-hidden']='false';
  startFailure='請先結束錄影並等待收尾';await get('servoTestButton').click();
  assert.equal(requests.at(-1).action,'stop','failed start must still be fenced');
  assert.equal(get('servoTestState').textContent,startFailure);
  now+=1001;await interval();
  assert.equal(get('servoTestState').textContent,startFailure,'polling must not erase the rejection reason');
  startFailure='';await get('servoTestButton').click();
  assert.equal(get('servoTestState').hidden,true,'retry clears the prior failure');
  status={...status,running:false,connected:false,phase:'complete',notice:'測試指令完成：水平在中心右側 5°，垂直回中'};
  await interval();
  assert.equal(get('servoTestButton').textContent,'雲台測試');
  assert.equal(get('servoTestState').hidden,true,'completion has no standalone message, even on an older backend');
  status={...status,offline_preview:true};now+=1001;await interval();
  assert.equal(get('servoTestButton').disabled,true);
  assert.equal(get('servoTestState').hidden,true,'offline instructions are not a paragraph under the button');
  assert.equal(get('servoTestButton').title,'離線預覽不執行舵機動作');
  status={...status,offline_preview:false,tracking_active:true};now+=1001;await interval();
  assert.equal(get('servoTestButton').disabled,true);
  assert.equal(get('servoTestState').hidden,true);
  assert.equal(get('servoTestButton').title,'請先停止自動跟拍');
  status={...status,tracking_active:false,calibrated:false};now+=1001;await interval();
  assert.equal(get('servoTestButton').disabled,true);
  assert.equal(get('servoTestState').hidden,true);
  assert.equal(get('servoTestButton').title,'請先設定水平 M0、垂直 M1 並確認兩軸方向');
  assert.ok(requests.every(body=>['start','heartbeat','stop'].includes(body.action)));
  console.log('PWM test controls: inline steps, STOP during start/run, retained errors, quiet completion, cancel fence, offline safety passed');
})().catch(error=>{console.error(error);process.exitCode=1});
