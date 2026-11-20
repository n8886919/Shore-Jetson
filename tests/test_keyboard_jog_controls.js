const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function fixture() {
  const documentEvents = {}, windowEvents = {}, timers = new Map(), requests = [];
  let time = 0, nextTimer = 0, delayStart = false, releaseStart, delaySpeed = false, releaseSpeed;
  const speedInput = {value:'5',max:'5',disabled:true,listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}};
  const speedMessage = {textContent:''};
  const document = {hidden:false,activeElement:null,
    getElementById:id=>({manualSpeed:speedInput,manualSpeedMessage:speedMessage}[id] || null),
    addEventListener:(name, fn)=>documentEvents[name]=fn};
  const window = {addEventListener:(name, fn)=>windowEvents[name]=fn};
  const context = vm.createContext({document,window,AbortSignal,
    Date:{now:()=>time},setInterval:fn=>{timers.set(++nextTimer,fn);return nextTimer;},
    clearInterval:id=>timers.delete(id),fetch:async(path, options)=>{
      if (!options?.method) return {ok:true,json:async()=>({control_token:'token'})};
      assert.equal(options.headers['X-Pan-Tilt-Token'],'token');
      const body=JSON.parse(options.body);requests.push(body);
      if (body.action==='start' && delayStart) await new Promise(resolve=>releaseStart=resolve);
      if (body.action==='speed') {
        if(delaySpeed){delaySpeed=false;await new Promise(resolve=>releaseSpeed=resolve);}
        return {ok:true,json:async()=>({ok:true,status:{speed_deg_s:body.speed_deg_s,available:true}})};
      }
      return {ok:true,json:async()=>({ok:true,session:'session'})};
    }});
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/keyboard-jog.js','utf8'),context);
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  await flush();
  const render=(epoch='1:1',enabled=true,extra={})=>window.shoreKeyboardControl.render({keyboard:{epoch,enabled,available:true,speed_deg_s:5,max_speed_deg_s:5},...extra});
  const event=(key,extra={})=>({key,target:{matches:()=>false},preventDefault(){this.prevented=true;},...extra});
  const down=async(key,extra={})=>{const e=event(key,extra);await documentEvents.keydown(e);return e;};
  const up=key=>documentEvents.keyup(event(key));
  const tick=async()=>{time+=100;for(const fn of [...timers.values()])await fn();};
  const input=async value=>{document.activeElement=speedInput;speedInput.value=value;speedInput.listeners.input();await flush();};
  return {window,document,requests,render,down,up,tick,flush,documentEvents,windowEvents,speedInput,speedMessage,input,
    delaySpeed:()=>{delaySpeed=true;},releaseSpeed:()=>releaseSpeed(),
    delay:()=>{delayStart=true;},release:()=>releaseStart(),age:()=>time+=1600};
}

(async()=>{
  let f=await fixture();
  f.render();assert.equal(f.requests.length,0,'opening and polling never moves');
  await f.down('ArrowUp',{target:{matches:()=>true}});
  assert.equal(f.requests.length,0,'input arrows retain their native behavior');
  await f.down('ArrowRight',{target:{tagName:'INPUT',type:'checkbox',matches:()=>true}});
  for(let i=0;i<35;i++){
    f.render();await f.down('ArrowRight',{repeat:true});await f.tick();
  }
  assert.equal(f.requests.filter(r=>r.action==='start').length,1,'arrows work immediately after clicking the AI checkbox');
  const moves=f.requests.filter(r=>r.action==='move');
  assert.equal(moves.length,35,'holding continues beyond two seconds without OS key repeats');
  assert.ok(moves.every((r,i)=>r.sequence===i+1));
  f.up('ArrowRight');assert.equal(f.requests.at(-1).action,'stop');
  const count=f.requests.length;await f.tick();assert.equal(f.requests.length,count);

  f=await fixture();f.render();f.delay();
  const pending=f.down('ArrowUp');await f.flush();f.up('ArrowUp');
  assert.deepEqual(f.requests.map(r=>r.action),['start','stop']);
  f.release();await pending;await f.tick();
  assert.equal(f.requests.length,2,'late start response cannot restore a released key');

  for(const reason of ['blur','pagehide','hidden','offline','ai','epoch','stale','escape','input']) {
    f=await fixture();f.render();await f.down('ArrowLeft');
    if(reason==='blur'||reason==='pagehide')f.windowEvents[reason]();
    else if(reason==='hidden'){f.document.hidden=true;f.documentEvents.visibilitychange();}
    else if(reason==='offline')f.window.shoreKeyboardControl.disconnect();
    else if(reason==='ai')f.render('1:1',false);
    else if(reason==='epoch')f.render('2:1');
    else if(reason==='stale'){f.age();await f.tick();}
    else if(reason==='input')f.documentEvents.focusin({target:{matches:()=>true}});
    else await f.down('Escape');
    assert.equal(f.requests.at(-1).action,'stop',reason);
    const count=f.requests.length;await f.tick();assert.equal(f.requests.length,count,reason);
  }
  f=await fixture();f.render('1:1',true,{offline_preview:true});await f.down('ArrowDown');
  assert.equal(f.requests.length,0);
  await f.input('2');assert.equal(f.requests.length,0,'offline input does not send settings');

  f=await fixture();f.render();await f.input('2.5');
  assert.deepEqual(f.requests,[{action:'speed',speed_deg_s:2.5}],'input applies without Enter or blur');
  assert.equal(f.speedMessage.textContent,'已套用');
  const speedCount=f.requests.length;
  for(const value of ['', '0', '-1', '6', 'abc'])await f.input(value);
  assert.equal(f.requests.length,speedCount,'partial and invalid edits do not change speed');
  await f.down('ArrowUp',{target:{tagName:'INPUT',type:'number'}});
  assert.equal(f.requests.length,speedCount,'arrows inside speed input do not move a servo');

  f=await fixture();f.render();f.delaySpeed();
  await f.input('1');await f.input('2');await f.input('3');f.render();
  assert.equal(f.speedInput.value,'3','polling preserves an in-progress edit');
  f.releaseSpeed();await f.flush();
  assert.deepEqual(f.requests.filter(r=>r.action==='speed').map(r=>r.speed_deg_s),[1,3],'latest queued value wins');
  f.window.shoreKeyboardControl.disconnect();await f.input('4');
  assert.equal(f.requests.length,2,'disconnection disables speed updates');

  for(const file of ['index.html','mobile.html','advanced.html']) {
    const html=fs.readFileSync(`shore_tracker/web/static/${file}`,'utf8');
    assert.ok(html.includes('id="manualSpeed"') && html.includes('src="/keyboard-jog.js"'),file);
  }
  console.log('PWM keyboard: sustained hold, release, late reply, focus, offline and lease guards passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
