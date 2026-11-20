// Prefer hardware H.265/WebRTC on the LAN; retain JPEG on failure.
(() => {
  const video = document.getElementById('rtcStream');
  if (!video) return;
  const local = new URLSearchParams(location.search).get('display') === 'local';
  const actions = document.getElementById('windowActions');
  if (actions && !['127.0.0.1','localhost','[::1]'].includes(location.hostname)) actions.style.display = 'none';
  let peer = null, busy = false, retryAt = 0, generation = 0, statsTimer = null;
  const state = window.shoreRemoteVideo = {active:false, tick};
  function reset() {
    if (statsTimer) clearInterval(statsTimer);statsTimer=null;
    ++generation; state.active = false; busy = false; retryAt = Date.now()+30000;
    video.hidden = true; video.srcObject = null; video.dataset.transport = "jpeg";
    const old = peer; peer = null;
    if (old) {old.onconnectionstatechange = null; old.close();}
  }
  async function tick(online) {
    if (!online) {if (peer || busy) reset(); return;}
    if (local || peer || busy || Date.now()<retryAt || typeof RTCPeerConnection === 'undefined') return;
    busy = true;
    const revision = generation;
    try {
      const settings = await fetch('/api/stream',{cache:'no-store',signal:AbortSignal.timeout(3000)});
      if (!(await settings.json()).webrtc) {retryAt=Date.now()+30000;return;}
      if (revision !== generation) return;
      peer = new RTCPeerConnection({iceServers:[]});
      const current = peer;
      const receiver = current.addTransceiver('video',{direction:'recvonly'});
      if (receiver?.setCodecPreferences && typeof RTCRtpReceiver !== 'undefined') {
        const hevc = RTCRtpReceiver.getCapabilities?.('video')?.codecs.filter(
          codec => codec.mimeType.toLowerCase() === 'video/h265');
        if (hevc?.length) receiver.setCodecPreferences(hevc);
      }
      current.ontrack = event => {
        if (current !== peer) return;
        video.srcObject = new MediaStream([event.track]);
        video.play().catch(() => {if (current === peer) reset();});
      };
      current.onconnectionstatechange = () => {
        if (current === peer && ['failed','closed','disconnected'].includes(current.connectionState)) reset();
      };
      video.onplaying = () => {
        if (current !== peer) return;
        state.active=true;video.hidden=false;video.dataset.transport='webrtc';
        if (typeof video.requestVideoFrameCallback === 'function') {
          const frame = (_now,metadata) => {
            if (current !== peer) return;
            video.dataset.presentedFrames=String(metadata.presentedFrames);
            video.requestVideoFrameCallback(frame);
          };
          video.requestVideoFrameCallback(frame);
        }
      };
      const offer = await current.createOffer();
      if (current !== peer) return;
      if (!/^a=rtpmap:\d+ H265\/90000\s*$/im.test(offer.sdp)) {
        reset();
        return;
      }
      await current.setLocalDescription(offer);
      if (current !== peer) return;
      const reply = await fetch('/api/stream/webrtc',{method:'POST',
        headers:{'Content-Type':'application/sdp'},body:current.localDescription.sdp,
        signal:AbortSignal.timeout(10000)});
      if (!reply.ok) throw new Error('WebRTC unavailable');
      if (revision !== generation || current !== peer) return;
      await current.setRemoteDescription({type:'answer',sdp:await reply.text()});
      if (typeof current.getStats === 'function') {
        let lastFrames=0,lastTime=0;
        statsTimer=setInterval(async () => {
          if (current !== peer) return;
          try {
            for (const report of (await current.getStats()).values()) {
              if (report.type==='inbound-rtp' && report.kind==='video') {
                if(lastTime) video.dataset.decodeFps=((report.framesDecoded-lastFrames)*1000/(report.timestamp-lastTime)).toFixed(1);
                lastFrames=report.framesDecoded;lastTime=report.timestamp;
                video.dataset.jitterMs=(report.jitter*1000).toFixed(1);
              }
              if(report.type==='candidate-pair' && report.state==='succeeded' && report.currentRoundTripTime!=null)
                video.dataset.roundtripMs=(report.currentRoundTripTime*1000).toFixed(1);
            }
          } catch (_) { /* Stats are optional; playback and fallback remain independent. */ }
        },1000);
      }
      setTimeout(() => {if(current === peer && !state.active) reset();},10000);
    } catch (_) {if (revision === generation) reset();}
    finally {if (revision === generation) busy=false;}
  }
  window.addEventListener('pagehide',reset);
  window.addEventListener('beforeunload',reset);
})();
