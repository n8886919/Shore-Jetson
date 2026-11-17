// UI state-machine regression checks with no browser or hardware connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element() {
  return {textContent:'', value:0, disabled:false, dataset:{}, style:{}, children:[],
    classList:{toggle(){}}, setAttribute(key,value){this[key]=value},
    append(child){this.children.push(child)}, replaceChildren(...children){this.children=children},
    addEventListener(type,listener){this[type]=listener},
    scrollHeight:0,clientHeight:0,scrollTop:0,clientWidth:0};
}
const get = id => { if (!elements.has(id)) elements.set(id,element()); return elements.get(id); };
let current = {
  components: {camera:{state:'ready'},inference:{state:'ready'},servo:{state:'ready'},recording:{state:'ready'}},
  tracking:{armed:false, mode:'disarmed'},metrics:{},tuning:{pan_gain_deg:45,pan_deadband:.04,zoom_maximum:1,pan_direction:-1},
  manual_recording:{state:'idle',manual_requested:false,continuous_enabled:false},arm_allowed:true
};
const requests = [];
let disconnected = false;
const context = vm.createContext({
  document:{getElementById:get,createElement:element,createElementNS:element,querySelectorAll:()=>[]},
  window:{confirm:()=>{throw new Error('Starting tracking must not open a confirmation')},addEventListener(){}}, setInterval(){},setTimeout(){},clearTimeout(){},
  Date,Map,Number,String,Math,AbortSignal,
  fetch: async (path,options={}) => {
    if(disconnected) throw new Error('offline');
    if(options.method === 'POST') {const body=JSON.parse(options.body);requests.push([path,body]);if(path==='/api/recording/mode')current.manual_recording.continuous_enabled=body.continuous;}
    return {ok:true,json:async()=>path === '/api/status' ? current : {ok:true}};
  },
});
const tick = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/app.js','utf8'),context);
  await tick();
  assert.equal(get('deadbandValue').textContent,'8%');
  get('deadband').value=.125;vm.runInContext('showTuning()',context);assert.equal(get('deadbandValue').textContent,'25%');
  assert.equal(get('armButton').textContent,'開始追蹤');
  assert.equal(get('armButton').disabled,false);
  await get('armButton').click();
  assert.equal(requests.at(-1)[0],'/api/arm');
  current.tracking={armed:false,mode:'arming'};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').textContent,'取消啟動');
  assert.equal(get('armButton').disabled,false);
  await get('armButton').click();
  assert.equal(requests.at(-1)[0],'/api/stop');
  current.tracking={armed:true,mode:'searching'};
  current.arm_allowed=false;
  current.manual_recording={state:'recording',manual_requested:true,continuous_enabled:false};
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').textContent,'停止追蹤');
  assert.equal(get('armButton').disabled,false);
  get('continuousRecording').checked=true;await get('continuousRecording').change();
  assert.equal(requests.at(-1)[0],'/api/recording/mode');assert.equal(requests.at(-1)[1].continuous,true);
  assert.equal(get('continuousRecording').checked,true);
  assert.ok(!requests.some(([path])=>path==='/api/recording'));
  disconnected=true;
  await vm.runInContext('refresh()',context);
  assert.match(get('armButton').textContent,/狀態未知/);
  assert.equal(get('armButton').disabled,false);
  assert.equal(get('continuousRecording').disabled,true);
  disconnected=false;
  await get('armButton').click();
  assert.equal(requests.at(-1)[0],'/api/stop');
  current.offline_preview=true;
  vm.runInContext('render('+JSON.stringify(current)+')',context);
  assert.equal(get('armButton').disabled,true);
  console.log('Dashboard controls: start / cancel / stop / continuous recording mode / stale status passed');
})().catch(error=>{console.error(error);process.exitCode=1});
