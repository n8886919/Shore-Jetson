const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element() {
  return {textContent:'',value:0,disabled:false,hidden:false,dataset:{},style:{setProperty(k,v){this[k]=v}},children:[],classList:{toggle(){}},
    setAttribute(key,value){this[key]=value},replaceChildren(...children){this.children=children},append(...children){this.children.push(...children)},
    addEventListener(event,handler){this[event]=handler},clientWidth:0,clientHeight:0};
}
const get = id => { if(!elements.has(id)) elements.set(id,element());return elements.get(id); };
let current = {components:{camera:{state:'ready'},inference:{state:'ready'},servo:{state:'ready'},recording:{state:'ready'}},tracking:{armed:false,mode:'disarmed'},metrics:{},manual_recording:{state:'idle'},arm_allowed:true};
let offline=false;
const requests=[];
const context=vm.createContext({document:{getElementById:get,createElement:element,createElementNS:element,querySelector:()=>get('kiosk')},window:{innerWidth:1280,innerHeight:800,confirm:()=>true,addEventListener(){}},location:{search:''},URLSearchParams,setInterval(){},setTimeout(){},clearTimeout(){},Date,Number,String,Math,AbortSignal,
fetch:async(path,options={})=>{if(offline) throw new Error('offline');if(options.method==='POST')requests.push(path);return {ok:true,json:async()=>path==='/api/status'?current:{ok:true}}}});
(async()=>{
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/kiosk.js','utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  context.window.innerWidth=871;context.window.innerHeight=544;
  vm.runInContext('fitKioskCanvas()',context);
  assert.equal(get('kiosk').style['--kiosk-scale'],'0.68');
  const projection=vm.runInContext('detectionRect({x:0,y:0,width:1,height:1},100,100,200,100,1,.5)',context);
  assert.equal(projection.x,0);assert.equal(projection.y,0);
  assert.equal(projection.width,100);assert.equal(projection.height,50);
  context.window.innerWidth=1280;context.window.innerHeight=800;vm.runInContext('fitKioskCanvas()',context);
  assert.equal(get('armButton').textContent,'開始追蹤');
  assert.match(get('stream').src,/mode=web/);
  const countBeforeToggle=requests.length;
  get('controlToggle').click();assert.equal(get('controlsPanel').hidden,false);assert.equal(get('controlToggle')['aria-expanded'],'true');
  get('controlToggle').click();assert.equal(get('controlsPanel').hidden,true);assert.equal(requests.length,countBeforeToggle,'opening controls must not actuate');
  const badges=()=>get('liveInfo').children.map(e=>e.textContent);
  assert.deepEqual(badges(),['影像正常','辨識正常','錄影待命']);
  assert.equal(get('phase').hidden,true,'idle state is already shown by the button');
  current.manual_recording={state:'recording',manual_requested:true,elapsed_seconds:83};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(badges().at(-1),'手動錄影 1:23');
  assert.match(get('liveInfo').children.at(-1).className,/recording/);
  current.manual_recording={state:'failed'};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(badges().at(-1),'錄影失敗');
  current.manual_recording={state:'idle'};current.metrics.disk_pressure='stop';
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(badges().at(-1),'空間不足');
  current.metrics.disk_pressure='ok';current.components.inference={state:'stale',detail:'推論影格逾時'};current.arm_allowed=false;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').disabled,true);
  assert.equal(badges()[1],'辨識中斷');
  assert.ok(!badges().some(text=>text.includes('逾時')),'reasons belong on the control page');
  current.components.inference={state:'ready'};current.arm_allowed=true;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  await get('armButton').click();assert.equal(requests.at(-1),'/api/arm');
  current.tracking={armed:false,mode:'arming'};vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').textContent,'取消啟動');
  await get('armButton').click();assert.equal(requests.at(-1),'/api/stop');
  current.tracking={armed:true,mode:'searching'};current.arm_allowed=false;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').disabled,false);assert.equal(get('armButton').textContent,'停止追蹤');
  offline=true;await vm.runInContext('refresh()',context);
  assert.equal(get('stream').style.visibility,'hidden');
  assert.equal(get('armButton').disabled,false);
  assert.deepEqual(badges(),['服務連線中斷'],'stale data must never retain green status');
  offline=false;await get('armButton').click();assert.equal(requests.at(-1),'/api/stop');
  current.offline_preview=true;vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').disabled,true);
  assert.ok(!requests.some(path=>path.includes('camera')||path.includes('recording')));
  console.log('Kiosk: ARM/cancel/STOP, stale state, offline safety passed');
})().catch(error=>{console.error(error);process.exitCode=1});
