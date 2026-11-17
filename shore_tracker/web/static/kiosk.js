const $ = id => document.getElementById(id);
let commandNotice = '';
const displayQuery = new URLSearchParams(location.search);
const localDisplay = displayQuery.get('display') === 'local';
let frameCursor = 0, frameRecognized = false, currentRunId = null;
let frameExpiry = null;
let inferenceSequence = null, inferencePulse = null;
let stopRequested = false;
const kioskCanvas = document.querySelector('.kiosk');
const nanoLayout = localDisplay && displayQuery.get('layout') === 'nano';
let windowCompact = false, windowActionPending = false;
kioskCanvas.classList.toggle('nano-layout', nanoLayout);
kioskCanvas.classList.toggle('responsive-layout', !localDisplay);
function fitKioskCanvas() {
  const scale = localDisplay ? Math.min(window.innerWidth / (nanoLayout ? 1024 : 1280), window.innerHeight / (nanoLayout ? 600 : 800)) : 1;
  kioskCanvas.style.setProperty('--kiosk-scale',String(scale));
}
window.addEventListener('resize',fitKioskCanvas);
fitKioskCanvas();
$('windowScaleToggle').addEventListener('click',async () => {
  if (windowActionPending) return;
  windowActionPending = true;
  $('windowScaleToggle').disabled = true;
  try {
    const response = await fetch('/api/kiosk/window',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({compact:!windowCompact}),signal:AbortSignal.timeout(5000)});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error('window resize failed');
    windowCompact = result.compact;
    kioskCanvas.classList.toggle('half-size',windowCompact);
    $('windowScaleToggle').setAttribute('aria-pressed',String(windowCompact));
    const label = windowCompact ? '還原全螢幕' : '縮小視窗以顯示桌面';
    $('windowScaleToggle').setAttribute('aria-label',label);
    $('windowScaleToggle').title = label;
    commandNotice = '';
  } catch (_) {
    commandNotice = '無法縮放 Jetson 視窗';
    $('phase').hidden = false;
    $('phase').textContent = commandNotice;
  } finally {
    windowActionPending = false;
    $('windowScaleToggle').disabled = false;
    fitKioskCanvas();
  }
});
$('windowClose').addEventListener('click',() => {
  if (!window.confirm('確定關閉 kiosk 瀏覽器？')) return;
  window.close();
  // A manually opened browser tab may reject window.close().
  setTimeout(() => {
    commandNotice = '瀏覽器禁止關閉 · 請按 Alt+F4';
    $('phase').hidden = false;
    $('phase').textContent = commandNotice;
  },250);
});
let controlsOpen = false;
function setControlsOpen(open) {
  if (!open && controlsOpen) window.dispatchEvent(new Event('pan-tilt-panel-close'));
  controlsOpen = open;
  kioskCanvas.classList.toggle('controls-open',open);
  $('windowActions').hidden = open || !localDisplay;
  const panel = $('controlsPanel');
  if (!open && panel.contains?.(document.activeElement)) $('controlToggle').focus();
  panel.hidden = false;
  panel.setAttribute('aria-hidden',String(!open));
  panel.inert = !open;
  if (open) panel.focus?.({preventScroll:true});
  $('controlToggle').setAttribute('aria-expanded',String(open));
  $('controlToggleLabel').textContent = open ? '收合設定' : '設定';
  $('controlToggle').setAttribute('aria-label', open ? '收合設定' : '設定');
  $('controlToggle').classList.toggle('active',open);
  $('controlToggle').title = open ? '收合控制' : '展開控制';
}
$('controlToggle').addEventListener('click',() => setControlsOpen(!controlsOpen));
window.addEventListener('keydown',event => { if (event.key === 'Escape') setControlsOpen(false); });
window.addEventListener('pointerdown',event => {
  if (controlsOpen && event.target.closest('#armButton') === $('armButton') && !$('armButton').disabled) return;
  if (controlsOpen && !event.target.closest('.controls-drawer, .window-actions, #controlRail')) setControlsOpen(false);
});
setControlsOpen(displayQuery.get('controls') === '1');
function chip(text, tone, title = '') {
  const node = document.createElement('span');
  node.className = `status-chip ${tone}`;
  node.textContent = text;
  node.title = title;
  return node;
}
function renderOperatorStatus(data) {
  const {components, tracking, metrics} = data, recording = data.manual_recording || {};
  const sourceFps = !offlinePreview && components.camera?.state === 'ready' && Number.isFinite(metrics.preview_fps)
    ? Math.max(0, metrics.preview_fps) : 0;
  const fps = Math.round(sourceFps);
  const nodes = [chip(`${fps} FPS`, fps > 0 ? 'good' : 'bad',
    '影像來源更新速率' + (components.camera?.detail ? ` · ${components.camera.detail}` : ''))];
  const recordingNow = !offlinePreview && recording.state === 'recording' && metrics.disk_pressure !== 'stop';
  const detail = offlinePreview ? '錄影未連線' : metrics.disk_pressure === 'stop' ? '空間不足'
    : recording.state === 'failed' ? '錄影失敗' : recording.state === 'finalizing' ? '影片存檔中'
    : recording.state === 'starting' ? '錄影準備中' : recordingNow
    ? `${recording.continuous_requested ? '連續錄影' : recording.manual_requested ? '手動錄影' : recording.tracking_qualified ? '跟拍錄影' : '追浪試拍'} · ${Math.max(0,Math.floor(Number(recording.elapsed_seconds)||0))} 秒`
    : recording.detail || '未錄影';
  nodes.push(chip(recordingNow ? '錄影中' : '未錄影',
    `recording-badge ${recordingNow ? 'is-recording' : 'not-recording'}`,detail));
  const info = $('liveInfo');
  if (info.children.length !== nodes.length) info.replaceChildren(...nodes);
  else nodes.forEach((node,index) => {
    const current = info.children[index];
    if (current.className !== node.className) current.className = node.className;
    if (current.textContent !== node.textContent) current.textContent = node.textContent;
    current.title = node.title;
  }); // Keep the recording node attached so polling never restarts its blink.
  renderInferenceLamp();
  $('phase').hidden = !trackingActive || offlinePreview;
  const searching = ['arming','searching','finished'].includes(tracking.mode);
  const qualified = !searching && recording.tracking_requested === true && recording.tracking_qualified === true;
  $('phase').className = `status-chip ${searching ? 'phase-searching' : qualified ? 'phase-qualified' : 'phase-chasing'}`;
  $('phase').textContent = searching ? '尋找中' : qualified ? '起乘／衝浪' : '偵測到追浪';
  $('phase').title = qualified ? '追浪已發展成起乘／衝浪，事件影片會保留' : '';
  if (!trackingActive && !offlinePreview && !data.arm_allowed) {
    const blocked = [['camera','相機'],['inference','辨識'],['servo','舵機']].find(([key]) =>
      components[key]?.state !== 'ready' && !(key === 'inference' && components[key]?.state === 'idle'));
    if (blocked) {
      $('phase').hidden = false;
      $('phase').className = 'status-chip bad';
      $('phase').textContent = blocked[0] === 'servo'
        ? components.servo.state === 'manual' ? '舵機手動控制中' : '舵機未連線'
        : `${blocked[1]}未就緒 · 請開啟控制`;
    }
  }
  if (commandNotice) {
    $('phase').hidden = false;
    $('phase').className = 'status-chip bad';
    $('phase').textContent = commandNotice;
  }
}
let pending = false, commandPending = false, trackingActive = false, trackingKnown = false, offlinePreview = true;
let streamNeedsReconnect = true, streamRetryAt = 0;
let detectionItems = [], detectionZoom = 1, detectionCenter = .5, detectionTimer = null;
let trackingDeadband = null, trackingZoneVisible = false, trackingZoneZoom = 1, trackingZoneCenter = .5, trackingTiltEnabled = false;
$('stream').addEventListener('error', () => { streamNeedsReconnect = true; streamRetryAt = Date.now()+1000; });
function render(data) {
  offlinePreview = data.offline_preview === true;
  const tracking = data.tracking, metrics = data.metrics, cameraReady = data.components.camera.state === 'ready';
  const deadband = data.tuning?.pan_deadband;
  trackingDeadband = Number.isFinite(deadband) && deadband >= 0 && deadband <= .4 ? deadband : null;
  trackingTiltEnabled = data.tuning?.tilt_enabled === true;
  trackingKnown = true;
  trackingActive = tracking.armed || tracking.mode === 'arming';
  if (!trackingActive) stopRequested = false;
  if (currentRunId !== data.inference_run_id || !trackingActive) {
    frameRecognized = false;
    inferenceSequence = null;
    updateDetections(null,false,1,.5);
  }
  currentRunId = data.inference_run_id ?? null;
  $('armButton').textContent = tracking.mode === 'arming' ? '取消啟動' : trackingActive ? '停止追蹤' : '開始追蹤';
  $('armButton').className = trackingActive ? 'stop' : 'arm';
  $('armButton').disabled = offlinePreview || commandPending || (!trackingActive && !data.arm_allowed);
  $('armButton').title = trackingActive ? (data.servo_command?.protocol === 'pelco_d' ? '送出雲台停止指令，不切斷電源' : '停止傳送新目標，不切斷 Servo 電源') : '開始自動尋找與跟拍衝浪者';
  renderOperatorStatus(data);
  if (!cameraReady) streamNeedsReconnect = true;
  if (cameraReady && streamNeedsReconnect && Date.now() >= streamRetryAt) {
    streamNeedsReconnect = false;
    frameCursor = 0;
  }
  const visible = cameraReady && !streamNeedsReconnect;
  trackingZoneVisible = visible && !offlinePreview;
  $('noSignal').hidden = visible;
  window.shoreRemoteVideo?.tick(visible && !offlinePreview);
  const remoteVideo = window.shoreRemoteVideo?.active === true;
  $('stream').style.visibility = visible && !remoteVideo ? 'visible' : 'hidden';
  $('streamNotice').hidden = visible;
  $('streamNotice').textContent = offlinePreview ? '離線預覽 · 未連接 Jetson 相機' : cameraReady ? '影像重新連接中' : '等待相機畫面';
  const zoom = Number(metrics.zoom_scale || 1), center = Number(metrics.zoom_center_x || .5);
  $('stream').style.transformOrigin = `${center*100}% 50%`;
  $('stream').style.transform = `scale(${zoom})`;
  if ($('rtcStream')) {
    $('rtcStream').style.transformOrigin = `${center*100}% 50%`;
    $('rtcStream').style.transform = `scale(${zoom})`;
  }
  if (remoteVideo) {
    frameRecognized = trackingActive && data.recognition?.available === true;
    updateDetections(data.recognition,frameRecognized,zoom,center);
    renderInferenceLamp(data.recognition);
  }
  detectionZoom = zoom;
  detectionCenter = center;
  trackingZoneZoom = zoom;
  trackingZoneCenter = center;
  drawDetections();
}
async function refresh() {
  if (pending) return;
  pending = true;
  try {
    const response = await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error('unavailable');
    render(await response.json());
  } catch (_) {
    window.shoreRemoteVideo?.tick(false);
    trackingKnown = false;
    $('armButton').textContent = '停止追蹤';
    $('armButton').title = '狀態未知；只會嘗試送出停止指令';
    $('armButton').className = 'stop';
    $('armButton').disabled = offlinePreview || commandPending;
    $('phase').hidden = true;
    $('liveInfo').replaceChildren(chip('服務連線中斷','bad'));
    $('phase').hidden = false;
    $('phase').className = 'status-chip bad';
    $('phase').textContent = '請至控制確認';
    streamNeedsReconnect = true;
    trackingZoneVisible = false;
    $('noSignal').hidden = false;
    $('stream').style.visibility = 'hidden';
    $('streamNotice').hidden = false;
    $('streamNotice').textContent = '等待服務連線恢復';
    updateDetections(null,false,1,.5);
    frameRecognized = false;
    renderInferenceLamp();
  } finally { pending = false; }
}
$('armButton').addEventListener('click',async () => {
  if (commandPending || offlinePreview) return;
  setControlsOpen(false); // Move the button only after its pointer-up/click is delivered.
  const stop = !trackingKnown || trackingActive;
  commandNotice = '';
  if (stop) {
    stopRequested = true;
    frameRecognized = false;
    updateDetections(null,false,1,.5);
    renderInferenceLamp();
  }
  commandPending = true;
  $('armButton').disabled = true;
  try {
    const response = await fetch(stop ? '/api/stop' : '/api/arm',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(5000)});
    const result = await response.json();
    if (!result.ok) commandNotice = '指令未確認 · 請至控制';
  } catch (_) { commandNotice = '指令未確認 · 請至控制'; }
  finally { commandPending = false; await refresh(); }
});
setInterval(refresh,200);
refresh();

function renderInferenceLamp(recognition = null) {
  const visible = frameRecognized && trackingActive && !stopRequested && !offlinePreview;
  $('inferenceLamp').style.visibility = visible ? 'visible' : 'hidden';
  if (!visible) {
    inferencePulse?.cancel();
    inferencePulse = null;
    return;
  }
  const sequence = recognition?.sequence;
  if (!Number.isSafeInteger(sequence) || sequence === inferenceSequence) return;
  inferenceSequence = sequence;
  inferencePulse?.cancel();
  if (!window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) {
    // One pulse per completed model result, even when many preview frames reuse it.
    inferencePulse = $('inferenceDot').animate([{opacity:1},{opacity:0}],{duration:120});
  }
}

function commitDisplayFrame(image, metadata) {
  if (streamNeedsReconnect || offlinePreview) return;
  const canvas = $('stream');
  const width = image.naturalWidth || image.width, height = image.naturalHeight || image.height;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  canvas.getContext('2d', {alpha:false}).drawImage(image,0,0);
  frameCursor = metadata.cursor;
  frameRecognized = metadata.recognized === true && metadata.recognition?.available === true && metadata.run_id === currentRunId && trackingActive && !stopRequested;
  updateDetections(frameRecognized ? metadata.recognition : null,true,detectionZoom,detectionCenter);
  renderInferenceLamp(metadata.recognition);
  clearTimeout(frameExpiry);
  // A stalled HTTP stream must not leave a green lamp or old boxes on screen.
  frameExpiry = setTimeout(() => {
    frameRecognized = false;
    updateDetections(null,false,1,.5);
    renderInferenceLamp();
  },500);
}

async function nextDisplayFrame() {
  let bitmap = null;
  const started = performance.now();
  try {
    if (!streamNeedsReconnect && !offlinePreview && !window.shoreRemoteVideo?.active) {
      const response = await fetch(`/api/display-frame?after=${frameCursor}`,{cache:'no-store',signal:AbortSignal.timeout(2500)});
      if (response.status === 200) {
        const metadata = JSON.parse(response.headers.get('X-Frame-Meta'));
        bitmap = await createImageBitmap(await response.blob());
        await new Promise(resolve => requestAnimationFrame(() => {
          commitDisplayFrame(bitmap,metadata); resolve();
        }));
      }
    }
  } catch (_) {
    frameRecognized = false;
    updateDetections(null,false,1,.5);
    renderInferenceLamp();
  } finally {
    if (bitmap) bitmap.close();
    setTimeout(nextDisplayFrame,Math.max(10,(localDisplay ? 0 : 200)-(performance.now()-started)));
  }
}
if (typeof createImageBitmap !== 'undefined') nextDisplayFrame();

function updateDetections(recognition, visible, zoom, center) {
  clearTimeout(detectionTimer);
  detectionItems = visible && recognition?.available ? (recognition.detections || []) : [];
  detectionZoom = zoom;
  detectionCenter = center;
  drawDetections();
  if (detectionItems.length) {
    detectionTimer = setTimeout(() => { detectionItems = []; drawDetections(); }, Math.max(0, recognition.valid_for_ms || 0));
  }
}

// Match top-left object-fit: contain plus the image element's digital zoom transform.
function detectionRect(box, width, height, naturalWidth, naturalHeight, zoom, center) {
  const scale = Math.min(width / naturalWidth, height / naturalHeight);
  const imageWidth = naturalWidth * scale, imageHeight = naturalHeight * scale;
  const originX = width * center, originY = height / 2;
  return {
    x: (box.x*imageWidth-originX)*zoom+originX,
    y: (box.y*imageHeight-originY)*zoom+originY,
    width: box.width*imageWidth*zoom, height: box.height*imageHeight*zoom
  };
}

function drawDetections() {
  drawTrackingZone();
  const layer = $('detectionOverlay'), img = $('stream');
  const width = img.clientWidth, height = img.clientHeight;
  layer.replaceChildren();
  const naturalWidth = img.width, naturalHeight = img.height;
  if (!width || !height || !naturalWidth || !naturalHeight) return;
  layer.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const names = {chasing_wave: '追', takeoff: '起', surfing: '衝'};
  function svg(tag, attributes) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
  }
  for (const item of detectionItems) {
    const b = detectionRect(item.box, width, height, naturalWidth, naturalHeight, detectionZoom, detectionCenter);
    if (b.x+b.width <= 0 || b.y+b.height <= 0 || b.x >= width || b.y >= height) continue;
    const states = (item.states || []).filter(state => names[state]);
    const color = states.includes('surfing') ? '#65f59c' : states.includes('takeoff') ? '#27dfcb' : states.includes('chasing_wave') ? '#ffce60' : '#dbe7eb';
    const label = `#${item.track_id}` + (states.length ? ` ${states.map(state => names[state]).join('/')}` : '');
    const labelWidth = 20 + String(item.track_id).length*7 + states.length*16;
    const x = Math.max(0, Math.min(width-labelWidth, b.x));
    const y = Math.max(0, Math.min(height-18, b.y-18));
    const text = svg('text', {x:x+4, y:y+12, fill:color});
    text.textContent = label;
    layer.append(svg('rect', {...b, fill:'none', stroke:color, 'stroke-width':1.5}),
      svg('rect', {x,y,width:labelWidth,height:17,rx:2,fill:'#061116','fill-opacity':.8}), text);
  }
}
function drawTrackingZone() {
  const layer = $('trackingZoneOverlay'), img = $('stream');
  const width = img.clientWidth, height = img.clientHeight;
  const visible = trackingZoneVisible && trackingDeadband !== null && width > 0 && height > 0 && img.width > 0 && img.height > 0;
  layer.style.display = visible ? 'block' : 'none';
  layer.setAttribute('aria-hidden',String(!visible));
  if (!visible) return;
  const band = detectionRect({x:.5-trackingDeadband,y:.5-trackingDeadband,width:2*trackingDeadband,height:2*trackingDeadband},
    width,height,img.width,img.height,trackingZoneZoom,trackingZoneCenter);
  layer.setAttribute('viewBox',`0 0 ${width} ${height}`);
  for (const [key,value] of Object.entries(band)) $('trackingZoneFill').setAttribute(key,String(value));
  $('trackingZoneEdges').setAttribute('d',`M ${band.x} 0 V ${height} M ${band.x+band.width} 0 V ${height}`);
  $('trackingZoneHorizontalEdges').setAttribute('d',trackingTiltEnabled ? `M 0 ${band.y} H ${width} M 0 ${band.y+band.height} H ${width}` : '');
}
$('stream').addEventListener('load', drawDetections);
if (typeof ResizeObserver !== 'undefined') new ResizeObserver(drawDetections).observe($('stream'));
else window.addEventListener('resize', drawDetections);
