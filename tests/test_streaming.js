const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('shore_tracker/web/static/streaming.js','utf8');
async function run({local=false,fail=false,hevc=true}={}) {
  const calls=[],video={hidden:true,dataset:{},play:async()=>{}},actions={style:{}},timers=[];
  class Peer {
    addTransceiver(){} close(){this.closed=true;}
    async createOffer(){return {type:'offer',sdp:'v=0\r\na=rtpmap:96 '+(hevc?'H265':'H264')+'/90000\r\n'};}
    async setLocalDescription(d){this.localDescription=d;}
    async setRemoteDescription(d){assert.equal(d.type,'answer');this.ontrack({track:{}});video.onplaying();}
  }
  const window={addEventListener(){}};
  const context={window,document:{getElementById:id=>id==='rtcStream'?video:actions},location:{search:local?'?display=local':'',hostname:'192.0.2.20'},URLSearchParams,Date,
    RTCPeerConnection:Peer,MediaStream:class {},AbortSignal:{timeout:()=>({})},setTimeout:f=>timers.push(f),
    fetch:async(path,options)=>{calls.push([path,options]);return path==='/api/stream'?{json:async()=>({webrtc:true})}:{ok:!fail,headers:{get:()=>fail?null:'test-session'},text:async()=>'answer'};}};
  vm.runInNewContext(source,context);assert.equal(calls.length,0,'loading a page sends no commands');
  await window.shoreRemoteVideo.tick(true);
  if(local){assert.equal(calls.length,0);return;}
  assert.equal(actions.style.display,'none');
  assert.equal(window.shoreRemoteVideo.active,!fail && hevc);
  if (!hevc) assert.deepEqual(calls.map(c=>c[0]),['/api/stream']);
  assert.equal(video.hidden,fail || !hevc);
  assert(calls.every(([path])=>path.startsWith('/api/stream')));
  await window.shoreRemoteVideo.tick(false);
  assert.equal(window.shoreRemoteVideo.active,false);assert(video.hidden);
}
(async()=>{await run();await run({fail:true});await run({hevc:false});await run({local:true});console.log('Streaming: SDP, fallback, local-mode exclusion and stale teardown passed');})().catch(e=>{console.error(e);process.exitCode=1;});
