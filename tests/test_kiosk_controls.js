const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element() {
  return {textContent:'',value:0,disabled:false,hidden:false,dataset:{},style:{setProperty(k,v){this[k]=v}},children:[],classList:{toggle(){}},
    setAttribute(key,value){this[key]=value},replaceChildren(...children){this.children=children},append(...children){this.children.push(...children)},
    addEventListener(event,handler){this[event]=handler},getContext(){return {drawImage(){}}},
    animate(frames,options){const pulse={frames,options,cancelled:false,cancel(){this.cancelled=true}};(this.pulses ||= []).push(pulse);return pulse},clientWidth:0,clientHeight:0};
}
const get = id => { if(!elements.has(id)) elements.set(id,element());return elements.get(id); };
let current = {components:{camera:{state:'ready'},inference:{state:'ready'},servo:{state:'ready'},recording:{state:'ready'}},tracking:{armed:false,mode:'disarmed'},metrics:{preview_fps:10},manual_recording:{state:'idle'},arm_allowed:true};
let offline=false;
const requests=[];
const windowListeners={};
let closeRequests=0;
const context=vm.createContext({document:{getElementById:get,createElement:element,createElementNS:element,querySelector:()=>get('kiosk')},window:{innerWidth:1280,innerHeight:800,confirm:()=>true,close:()=>{closeRequests++},dispatchEvent(event){for(const fn of windowListeners[event.type]||[])fn(event);},addEventListener(event,handler){(windowListeners[event] ||= []).push(handler)}},location:{search:''},URL:{revokeObjectURL(){}},URLSearchParams,Event:class{constructor(type){this.type=type;}},setInterval(){},setTimeout(){},clearTimeout(){},Date,Number,String,Math,AbortSignal,
fetch:async(path,options={})=>{if(offline) throw new Error('offline');if(options.method==='POST')requests.push(path);if(path==='/api/kiosk/window'){const compact=JSON.parse(options.body).compact;if(compact){context.window.fullSize=[context.window.innerWidth,context.window.innerHeight];context.window.innerWidth/=2;context.window.innerHeight/=2}else{[context.window.innerWidth,context.window.innerHeight]=context.window.fullSize}return {ok:true,json:async()=>({ok:true,compact})}}return {ok:true,json:async()=>path==='/api/status'?current:{ok:true}}}});
(async()=>{
  context.location.search='?display=local';
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/kiosk.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  context.window.innerWidth=871;context.window.innerHeight=544;
  vm.runInContext('fitKioskCanvas()',context);
  assert.equal(get('kiosk').style['--kiosk-scale'],'0.68');
  const requestsBeforeWindowActions=requests.length;
  await get('windowScaleToggle').click();
  assert.equal(get('kiosk').style['--kiosk-scale'],'0.34');
  assert.equal(get('windowScaleToggle')['aria-pressed'],'true');
  await get('windowScaleToggle').click();
  assert.equal(get('kiosk').style['--kiosk-scale'],'0.68');
  context.window.confirm=()=>false;get('windowClose').click();assert.equal(closeRequests,0);context.window.confirm=()=>true;get('windowClose').click();assert.equal(closeRequests,1);
  assert.ok(requests.slice(requestsBeforeWindowActions).every(p=>p==='/api/kiosk/window'),'window controls do not issue hardware commands');
  const projection=vm.runInContext('detectionRect({x:0,y:0,width:1,height:1},100,100,200,100,1,.5)',context);
  assert.equal(projection.x,0);assert.equal(projection.y,0);
  assert.equal(projection.width,100);assert.equal(projection.height,50);
  context.window.innerWidth=1280;context.window.innerHeight=800;vm.runInContext('fitKioskCanvas()',context);
  // The guide reflects the controller's source-frame deadband, including digital zoom.
  Object.assign(get('stream'),{width:960,height:540,clientWidth:960,clientHeight:540});
  current.tuning={pan_deadband:.04};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('trackingZoneOverlay').style.display,'block');
  assert.ok(Math.abs(Number(get('trackingZoneFill').x)-441.6)<1e-6);
  assert.ok(Math.abs(Number(get('trackingZoneFill').width)-76.8)<1e-6);
  assert.equal(get('trackingZoneEdges').d,'M 441.6 0 V 540 M 518.4 0 V 540');
  assert.equal(get('trackingZoneHorizontalEdges').d,'','tilt disabled hides horizontal bounds');
  current.tuning.tilt_enabled=true;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('trackingZoneHorizontalEdges').d,'M 0 248.4 H 960 M 0 291.6 H 960');
  current.tuning.tilt_enabled=false;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('trackingZoneHorizontalEdges').d,'');
  current.metrics.zoom_scale=2;current.metrics.zoom_center_x=.8;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.ok(Math.abs(Number(get('trackingZoneFill').x)-115.2)<1e-6);
  assert.ok(Math.abs(Number(get('trackingZoneFill').width)-153.6)<1e-6);
  const zoneBeforeClear=get('trackingZoneFill').x;
  vm.runInContext('updateDetections(null,false,1,.5)',context);
  assert.equal(get('trackingZoneFill').x,zoneBeforeClear,'clearing detections does not move the guide');
  current.tuning.pan_deadband=.08;current.metrics.zoom_scale=1;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.ok(Math.abs(Number(get('trackingZoneFill').width)-153.6)<1e-6);
  current.tuning.pan_deadband=.4;current.metrics.zoom_center_x=.5;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('trackingZoneOverlay').style.display,'block','80% tolerance remains visible');
  assert.ok(Math.abs(Number(get('trackingZoneFill').width)-768)<1e-6);
  current.tuning.pan_deadband=.125;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(Number(get('trackingZoneFill').x),360);
  assert.equal(Number(get('trackingZoneFill').width),240);
  current.tuning.pan_deadband=.04;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').textContent,'開始追蹤');
  assert.equal(get('inferenceLamp').style.visibility,'hidden');
  const countBeforeToggle=requests.length;
  get('controlToggle').click();assert.equal(get('windowActions').hidden,true);assert.equal(get('controlRail').hidden,false);assert.equal(get('controlsPanel')['aria-hidden'],'false');assert.equal(get('controlsPanel').inert,false);assert.equal(get('controlToggle')['aria-expanded'],'true');
  for(const handler of windowListeners.pointerdown)handler({target:{closest:selector=>selector.includes('#controlRail')?get('controlRail'):null}});
  get('controlToggle').click();assert.equal(get('controlsPanel')['aria-hidden'],'true','footer gear closes without the outside handler reopening it');
  get('controlToggle').click();
  for(const handler of windowListeners.pointerdown)handler({target:{closest:()=>get('controlsPanel')}});assert.equal(get('controlsPanel')['aria-hidden'],'false','inside click keeps controls open');
  for(const handler of windowListeners.pointerdown)handler({target:{closest:()=>null}});assert.equal(get('windowActions').hidden,false);assert.equal(get('controlRail').hidden,false);assert.equal(get('controlsPanel')['aria-hidden'],'true');assert.equal(get('controlsPanel').inert,true);assert.equal(requests.length,countBeforeToggle,'opening controls must not actuate');
  current.metrics.used_gib=9.4;current.metrics.free_gib=19.0;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('recordingCapacity').textContent,'錄影空間：已用 9.4 GiB／剩餘 19.0 GiB');
  const badges=()=>get('liveInfo').children.map(e=>e.textContent);
  assert.deepEqual(badges(),['10 FPS','未錄影']);
  assert.match(get('liveInfo').children[0].className,/good/);
  for (const stopped of [0, null, NaN, -1, .001]) {
    current.metrics.preview_fps=stopped;
    vm.runInContext('render('+JSON.stringify(current)+')',context);
    assert.equal(badges()[0],'0 FPS');
    assert.match(get('liveInfo').children[0].className,/bad/,'zero or unavailable FPS must be red');
  }
  for (const [rate,expected] of [[10.4,'10 FPS'],[10.5,'11 FPS']]) {
    current.metrics.preview_fps=rate;
    vm.runInContext('render('+JSON.stringify(current)+')',context);
    assert.equal(badges()[0],expected);
  }
  current.metrics.preview_fps=10;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.match(get('liveInfo').children[0].className,/good/);
  assert.ok(!fs.readFileSync('shore_tracker/web/static/index.html','utf8').includes('預覽來源'));

  assert.equal(get('phase').hidden,true,'idle state is already shown by the button');
  current.components.servo={state:'error'};current.arm_allowed=false;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('phase').textContent,'舵機未連線');
  current.components.servo={state:'manual'};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('phase').textContent,'舵機手動控制中');
  current.components.servo={state:'ready'};current.arm_allowed=true;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  current.manual_recording={state:'recording',manual_requested:true,elapsed_seconds:83};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(badges().at(-1),'錄影中');
  assert.match(get('liveInfo').children.at(-1).className,/is-recording/);
  const recordingNode=get('liveInfo').children.at(-1);current.metrics.preview_fps=11;vm.runInContext('render('+JSON.stringify(current)+')',context);assert.equal(get('liveInfo').children.at(-1),recordingNode,'blink node survives status/FPS updates');current.metrics.preview_fps=10;
  current.manual_recording.continuous_requested=true;vm.runInContext('render('+JSON.stringify(current)+')',context);assert.equal(badges().at(-1),'錄影中');
  current.manual_recording={state:'failed'};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(badges().at(-1),'未錄影');assert.equal(get('liveInfo').children.at(-1).title,'錄影失敗');assert.match(get('liveInfo').children.at(-1).className,/not-recording/);
  current.manual_recording={state:'idle'};current.metrics.disk_pressure='stop';
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(badges().at(-1),'未錄影');assert.equal(get('liveInfo').children.at(-1).title,'空間不足');
  current.metrics.disk_pressure='ok';current.components.inference={state:'stale',detail:'推論影格逾時'};current.arm_allowed=false;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').disabled,true);
  assert.equal(get('inferenceLamp').style.visibility,'hidden');
  assert.ok(!badges().some(text=>text.includes('逾時')),'reasons belong on the control page');
  current.components.inference={state:'ready'};current.arm_allowed=true;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  context.window.confirm=()=>{throw new Error('Starting tracking must not open a confirmation')};
  get('controlToggle').click();
  for(const handler of windowListeners.pointerdown)handler({target:{closest:selector=>selector==='#armButton'?get('armButton'):null}});
  assert.equal(get('controlsPanel')['aria-hidden'],'false','pointer-down does not move the tracking button away');
  await get('armButton').click();assert.equal(requests.at(-1),'/api/arm');assert.equal(get('controlsPanel')['aria-hidden'],'true');
  current.tracking={armed:false,mode:'arming'};vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').textContent,'取消啟動');
  await get('armButton').click();assert.equal(requests.at(-1),'/api/stop');
  current.tracking={armed:true,mode:'searching'};current.arm_allowed=false;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').disabled,false);assert.equal(get('armButton').textContent,'停止追蹤');
  assert.equal(get('phase').textContent,'尋找中');assert.match(get('phase').className,/phase-searching/);
  current.tracking.mode='manual';
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('phase').textContent,'方向鍵手動控制');
  // Continuous/manual recording alone must never qualify a surf event.
  current.manual_recording={state:'recording',continuous_requested:true,manual_requested:true,tracking_requested:false,tracking_qualified:false};
  for (const mode of ['chasing','takeoff','surfing']) {
    current.tracking.mode=mode;
    vm.runInContext('render('+JSON.stringify(current)+')',context);
    assert.equal(get('phase').textContent,'偵測到追浪');assert.match(get('phase').className,/phase-chasing/);
  }
  current.manual_recording.tracking_requested=true;current.manual_recording.tracking_qualified=true;
  for (const reason of ['surfing','temporarily_lost']) {
    current.tracking.reason=reason;
    vm.runInContext('render('+JSON.stringify(current)+')',context);
    assert.equal(get('phase').textContent,'起乘／衝浪');assert.match(get('phase').className,/phase-qualified/);
  }
  for (const mode of ['finished','searching','arming']) {
    current.tracking.mode=mode;
    vm.runInContext('render('+JSON.stringify(current)+')',context);
    assert.equal(get('phase').textContent,'尋找中');assert.match(get('phase').className,/phase-searching/,'old clip qualification must not color a new search green');
  }
  current.tracking={armed:true,mode:'searching'};
  current.inference_run_id=7;
  vm.runInContext('stopRequested=false',context); // A new user-started run after the simulated STOP.
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  vm.runInContext('commitDisplayFrame({width:960,height:540},{cursor:1,recognized:true,run_id:7,recognition:{available:true,sequence:1,detections:[{track_id:1,box:{x:0,y:0,width:.1,height:.1}}]}})',context);
  assert.equal(get('inferenceLamp').style.visibility,'visible');
  assert.equal(vm.runInContext('detectionItems.length',context),1);
  const dot=get('inferenceDot');assert.equal(dot.pulses.length,1);
  for (let cursor=2;cursor<7;cursor++) {
    vm.runInContext(`commitDisplayFrame({width:960,height:540},{cursor:${cursor},recognized:true,run_id:7,recognition:{available:true,sequence:1,detections:[]}})`,context);
    vm.runInContext('render('+JSON.stringify(current)+')',context);
  }
  assert.equal(dot.pulses.length,1,'preview frames and status polls must not restart the inference pulse');
  vm.runInContext('commitDisplayFrame({width:960,height:540},{cursor:7,recognized:true,run_id:7,recognition:{available:true,sequence:2,detections:[]}})',context);
  assert.equal(dot.pulses.length,2,'an empty model result still flashes once');
  assert.equal(dot.pulses[0].cancelled,true);
  vm.runInContext('stopRequested=true;commitDisplayFrame({width:960,height:540},{cursor:8,recognized:true,run_id:7,recognition:{available:true,sequence:3,detections:[{}]}})',context);
  assert.equal(get('inferenceLamp').style.visibility,'hidden');
  assert.equal(dot.pulses.length,2);assert.equal(dot.pulses[1].cancelled,true);
  assert.equal(vm.runInContext('detectionItems.length',context),0);
  vm.runInContext('stopRequested=false',context);
  vm.runInContext('commitDisplayFrame({width:960,height:540},{cursor:2,recognized:false,run_id:null})',context);
  assert.equal(get('inferenceLamp').style.visibility,'hidden');
  assert.equal(vm.runInContext('detectionItems.length',context),0);
  vm.runInContext('commitDisplayFrame({width:960,height:540},{cursor:3,recognized:true,run_id:6,recognition:{available:true,detections:[{}]}})',context);
  assert.equal(get('inferenceLamp').style.visibility,'hidden');
  assert.equal(dot.pulses.length,2,'old inference runs cannot flash');
  current.inference_run_id=8;vm.runInContext('render('+JSON.stringify(current)+')',context);
  vm.runInContext('commitDisplayFrame({width:960,height:540},{cursor:9,recognized:true,run_id:8,recognition:{available:true,sequence:2,detections:[]}})',context);
  assert.equal(dot.pulses.length,3,'a new run can reuse a sequence after service restart');
  context.window.matchMedia=()=>({matches:true});
  vm.runInContext('commitDisplayFrame({width:960,height:540},{cursor:10,recognized:true,run_id:8,recognition:{available:true,sequence:3,detections:[]}})',context);
  assert.equal(dot.pulses.length,3,'reduced motion disables per-frame animation');
  offline=true;await vm.runInContext('refresh()',context);
  assert.equal(get('stream').style.visibility,'hidden');assert.equal(get('trackingZoneOverlay').style.display,'none');
  assert.equal(get('armButton').disabled,false);
  assert.deepEqual(badges(),['服務連線中斷'],'stale data must never retain green status');
  offline=false;await get('armButton').click();assert.equal(requests.at(-1),'/api/stop');
  current.offline_preview=true;vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').disabled,true);
  assert.ok(!requests.some(path=>path.includes('camera')||path.includes('recording')));
  const nano = vm.createContext({...context, location:{search:'?display=local&layout=nano'}, window:{...context.window,innerWidth:1024,innerHeight:600}});
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/kiosk.js','utf8'),nano);
  assert.equal(get('kiosk').style['--kiosk-scale'],'1');
  for (const [width,height] of [[375,812],[844,390],[1024,600],[1920,1080]]) {
    const web=vm.createContext({...context,location:{search:'?layout=nano'},window:{...context.window,innerWidth:width,innerHeight:height}});
    vm.runInContext(fs.readFileSync('shore_tracker/web/static/kiosk.js','utf8'),web);
    assert.equal(get('kiosk').style['--kiosk-scale'],'1','Web uses CSS viewport instead of a fixed canvas');
    assert.equal(vm.runInContext('nanoLayout',web),false);
    assert.equal(get('windowActions').hidden,true,'remote Web has no desktop window controls');
    get('controlToggle').click();
    assert.equal(get('controlRail').hidden,false,'settings remains in the footer while open');
  }
  console.log('Kiosk: window scale/close, ARM/cancel/STOP, stale state, offline safety, Nano 1024x600 passed');
})().catch(error=>{console.error(error);process.exitCode=1});
