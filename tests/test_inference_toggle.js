const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const toggle = {checked:false,disabled:true,addEventListener(event,callback){this[event]=callback;}};
const message = {hidden:true,textContent:''};
const current = {inference:{enabled:false,locked:false},components:{inference:{state:'idle'}}};
const requests = [];
let failure = false;
const context = vm.createContext({window:{},document:{getElementById:id=>id==='inferenceEnabled'?toggle:message},AbortSignal,
  fetch:async(path,options={})=>{
    if (failure) throw new Error('連線中斷');
    if (options.method==='POST') {
      const body=JSON.parse(options.body);requests.push({path,body});
      if (!current.inference.locked) current.inference.enabled=body.enabled;
      return {ok:!current.inference.locked,json:async()=>({ok:!current.inference.locked,detail:'錄影期間必須開啟 AI 辨識'})};
    }
    return {ok:true,json:async()=>current};
  }});
(async()=>{
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/inference-toggle.js','utf8'),context);
  const control=context.window.shoreInferenceControl;
  control.render(current);
  assert.equal(requests.length,0,'loading and rendering never sends controls');
  assert.equal(toggle.checked,false);assert.equal(toggle.disabled,false);
  toggle.checked=true;await toggle.change();
  assert.deepEqual(requests,[{path:'/api/inference',body:{enabled:true}}]);
  assert.equal(toggle.checked,true);
  current.inference.locked=true;control.render(current);
  assert.equal(toggle.disabled,true);assert.equal(toggle.checked,true);
  assert.equal(message.hidden,false);
  toggle.checked=false;await toggle.change();
  assert.equal(requests.length,1,'recording lock cannot be bypassed by an old event');
  assert.equal(toggle.checked,true);
  current.inference.locked=false;control.render(current);
  assert.equal(toggle.disabled,false);
  failure=true;toggle.checked=false;await toggle.change();
  assert.equal(toggle.checked,true,'failed request restores last confirmed value');
  assert.equal(message.hidden,false);
  control.disconnect();assert.equal(toggle.disabled,true);
  control.render({...current,offline_preview:true});assert.equal(toggle.disabled,true);
  assert.ok(requests.every(r=>r.path==='/api/inference'),'AI toggle never arms, records or sends motion');
  console.log('AI toggle: default off, readback, recording lock, failed request and offline guards passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
