const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const embedded=process.argv.includes('--embedded');
const elements=new Map();
function element(){return {textContent:'',value:0,disabled:false,dataset:{},style:{},classList:{toggle(){}},children:[],setAttribute(k,v){this[k]=v},replaceChildren(...a){this.children=a},addEventListener(k,v){this[k]=v}};}
const get=id=>{if(id==='controlsPanel'&&!embedded)return null;if(embedded&&['backLink','controlStream','controlNoSignal','controlStop','servoAngleSlider'].includes(id))return null;if(!elements.has(id))elements.set(id,element());return elements.get(id)};
const lens=['focus_near','focus_far','zoom_in','zoom_out'].map(action=>{const e=element();e.dataset.cameraAction=action;return e});
let current={components:{camera:{state:'ready'},inference:{state:'ready'},servo:{state:'ready'},recording:{state:'ready'}},tracking:{armed:false,mode:'disarmed'},metrics:{preview_fps:20},tuning:{pan_gain_deg:45,pan_deadband:.04,pan_direction:-1,zoom_maximum:1},manual_recording:{state:'idle',manual_requested:false}};
const diagnostic={available:true,recovery:{state:'ready',attempts:0},video:{},servo:{},inference:{max_fps:2}};
let offline=false;
const requests=[];
const context=vm.createContext({document:{getElementById:get,createElement:element,querySelectorAll:()=>lens},location:{search:'?mode=kiosk'},URLSearchParams,setInterval(){},Date,Number,String,Object,Math,AbortSignal,
fetch:async(path,options={})=>{if(offline)throw new Error('offline');if(options.method==='POST'){const body=JSON.parse(options.body);requests.push([path,body]);if(path==='/api/tuning')current.tuning=body;if(path==='/api/inference/rate')diagnostic.inference.max_fps=body.max_fps;return {ok:true,json:async()=>({ok:true})}}return {ok:true,json:async()=>path==='/api/status'?current:path==='/api/diagnostics'?diagnostic:{ok:true,zoom:true,focus:true}}}});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/advanced.js','utf8'),context);
  await tick();
  assert.equal(requests.length,0,'page load must never actuate');
  assert.equal(get('operatorIssue').hidden,true);
  current.components.inference={state:'stale',detail:'推論影格逾時'};
  vm.runInContext('render('+JSON.stringify(current)+','+JSON.stringify(diagnostic)+')',context);
  assert.equal(get('operatorIssue').hidden,false);
  assert.match(get('operatorIssue').textContent,/辨識尚未就緒/);
  current.components.inference={state:'ready'};
  vm.runInContext('render('+JSON.stringify(current)+','+JSON.stringify(diagnostic)+')',context);
  if(!embedded)assert.equal(get('backLink').href,'/kiosk');
  assert.equal(lens[0].disabled,false);
  if(!embedded){await get('controlStop').click();assert.equal(requests.at(-1)[0],'/api/stop');}
  await lens[0].click();assert.equal(requests.at(-1)[0],'/api/camera');assert.equal(requests.at(-1)[1].action,'focus_near');
  current.tracking={armed:true,mode:'searching'};vm.runInContext('render('+JSON.stringify(current)+','+JSON.stringify(diagnostic)+')',context);
  assert.equal(lens[0].disabled,true);const count=requests.length;await lens[0].click();assert.equal(requests.length,count);
  current.tracking={armed:false,mode:'disarmed'};vm.runInContext('render('+JSON.stringify(current)+','+JSON.stringify(diagnostic)+')',context);
  get('panGain').value=55;await get('panGain').change();assert.equal(current.tuning.pan_gain_deg,55);assert.equal(current.tuning.pan_direction,-1);
  get('inferenceRate').value=5;await get('inferenceRate').change();assert.equal(requests.at(-1)[0],'/api/inference/rate');assert.equal(diagnostic.inference.max_fps,5);
  await get('recordButton').click();assert.equal(requests.at(-1)[1].action,'start');
  current.manual_recording={state:'recording',manual_requested:true};vm.runInContext('render('+JSON.stringify(current)+','+JSON.stringify(diagnostic)+')',context);
  assert.equal(get('recordButton')['aria-label'],'結束手動錄影');await get('recordButton').click();assert.equal(requests.at(-1)[1].action,'stop');
  offline=true;await vm.runInContext('refresh()',context);assert.equal(get('recordButton').disabled,true);assert.equal(lens[0].disabled,true);if(!embedded)assert.equal(get('controlStream').style.visibility,'hidden');
  console.log('Control page: no actuation on load, lens guard, tuning readback, record start/stop, stale safety passed');
})().catch(error=>{console.error(error);process.exitCode=1});
