const $ = id => document.getElementById(id);
const phaseNames = {arming:'正在啟動',searching:'尋找衝浪者',chasing:'等待起乘',takeoff:'跟拍目標 · 起乘',surfing:'跟拍目標 · 衝浪中',finished:'本次跟拍結束'};
let commandNotice = '';
const displayQuery = new URLSearchParams(location.search);
const localDisplay = displayQuery.get('display') === 'local';
const streamUrl = () => `/api/preview.mjpeg?mode=${localDisplay ? 'local' : 'web'}&reconnect=${Date.now()}`;
$('stream').src = streamUrl();
$('displayRate').textContent = localDisplay ? '跟隨相機' : '5 FPS';
const kioskCanvas = document.querySelector('.kiosk');
function fitKioskCanvas() {
  const scale = Math.min(window.innerWidth / 1280, window.innerHeight / 800);
  kioskCanvas.style.setProperty('--kiosk-scale',String(scale));
}
window.addEventListener('resize',fitKioskCanvas);
fitKioskCanvas();
let controlsOpen = false;
function setControlsOpen(open) {
  controlsOpen = open;
  kioskCanvas.classList.toggle('controls-open',open);
  $('controlsPanel').hidden = !open;
  $('controlReadout').hidden = !open;
  $('controlToggle').setAttribute('aria-expanded',String(open));
  $('controlToggleLabel').textContent = '控制';
  $('controlToggle').classList.toggle('active',open);
  $('controlToggle').title = open ? '收合控制' : '展開控制';
}
$('controlToggle').addEventListener('click',() => setControlsOpen(!controlsOpen));
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
  const names = {camera:'影像',inference:'辨識'};
  const nodes = Object.entries(names).map(([key, name]) => {
    const state = components[key]?.state;
    const text = offlinePreview ? `${name}未連線` : state === 'ready' ? `${name}${key === 'servo' ? '連線' : '正常'}`
      : `${name}${state === 'starting' ? '準備中' : state === 'error' ? '異常' : '中斷'}`;
    return chip(text, offlinePreview ? 'neutral' : state === 'ready' ? 'good' : state === 'starting' ? 'warning' : 'bad',
      key === 'servo' ? '控制串口狀態；沒有 Servo 實際位置回授' : '');
  });
  const seconds = Math.max(0, Math.floor(Number(recording.elapsed_seconds) || 0));
  const elapsed = `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;
  let recordText = '錄影待命', recordTone = 'good';
  if (offlinePreview) { recordText = '錄影未連線'; recordTone = 'neutral'; }
  else if (metrics.disk_pressure === 'stop') { recordText = '空間不足'; recordTone = 'bad'; }
  else if (recording.state === 'failed') { recordText = '錄影失敗'; recordTone = 'bad'; }
  else if (components.recording?.state !== 'ready') { recordText = '錄影未就緒'; recordTone = 'warning'; }
  else if (recording.state === 'finalizing') { recordText = '影片存檔中'; recordTone = 'warning'; }
  else if (recording.state === 'starting') { recordText = '錄影準備中'; recordTone = 'warning'; }
  else if (recording.state === 'recording') {
    recordText = `${recording.manual_requested ? '手動錄影' : recording.tracking_qualified ? '跟拍錄影' : '追浪試拍'} ${elapsed}`;
    recordTone = 'recording';
  } else if (metrics.disk_pressure === 'warning') { recordText = '空間偏低'; recordTone = 'warning'; }
  nodes.push(chip(recordText, recordTone));
  $('liveInfo').replaceChildren(...nodes);
  $('phase').hidden = !trackingActive || offlinePreview;
  $('phase').textContent = tracking.reason === 'temporarily_lost' ? '目標暫時離開畫面' : phaseNames[tracking.mode] || '正在跟拍';
  if (commandNotice) {
    $('phase').hidden = false;
    $('phase').textContent = commandNotice;
  }
}
let pending = false, commandPending = false, trackingActive = false, trackingKnown = false, offlinePreview = true;
let streamNeedsReconnect = true, streamRetryAt = 0;
let detectionItems = [], detectionZoom = 1, detectionCenter = .5, detectionTimer = null;
$('stream').addEventListener('error', () => { streamNeedsReconnect = true; streamRetryAt = Date.now()+1000; });
function render(data) {
  offlinePreview = data.offline_preview === true;
  const tracking = data.tracking, metrics = data.metrics, cameraReady = data.components.camera.state === 'ready';
  trackingKnown = true;
  trackingActive = tracking.armed || tracking.mode === 'arming';
  $('armButton').textContent = tracking.mode === 'arming' ? '取消啟動' : trackingActive ? '停止追蹤' : '開始追蹤';
  $('armButton').className = trackingActive ? 'stop' : 'arm';
  $('armButton').disabled = offlinePreview || commandPending || (!trackingActive && !data.arm_allowed);
  $('armButton').title = trackingActive ? '停止傳送新目標，不切斷 Servo 電源' : '開始自動尋找與跟拍衝浪者';
  renderOperatorStatus(data);
  if (!cameraReady) streamNeedsReconnect = true;
  if (cameraReady && streamNeedsReconnect && Date.now() >= streamRetryAt) {
    streamNeedsReconnect = false;
    $('stream').src = streamUrl();
  }
  const visible = cameraReady && !streamNeedsReconnect;
  $('noSignal').hidden = visible;
  $('stream').style.visibility = visible ? 'visible' : 'hidden';
  $('streamNotice').hidden = visible;
  $('streamNotice').textContent = offlinePreview ? '離線預覽 · 未連接 Jetson 相機' : cameraReady ? '影像重新連接中' : '等待相機畫面';
  const zoom = Number(metrics.zoom_scale || 1), center = Number(metrics.zoom_center_x || .5);
  $('stream').style.transformOrigin = `${center*100}% 50%`;
  $('stream').style.transform = `scale(${zoom})`;
  updateDetections(data.recognition,visible,zoom,center);
}
async function refresh() {
  if (pending) return;
  pending = true;
  try {
    const response = await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error('unavailable');
    render(await response.json());
  } catch (_) {
    trackingKnown = false;
    $('armButton').textContent = '停止追蹤';
    $('armButton').title = '狀態未知；只會嘗試送出停止指令';
    $('armButton').className = 'stop';
    $('armButton').disabled = offlinePreview || commandPending;
    $('phase').hidden = true;
    $('liveInfo').replaceChildren(chip('服務連線中斷','bad'));
    $('phase').hidden = false;
    $('phase').textContent = '請至控制確認';
    streamNeedsReconnect = true;
    $('noSignal').hidden = false;
    $('stream').style.visibility = 'hidden';
    $('streamNotice').hidden = false;
    $('streamNotice').textContent = '等待服務連線恢復';
    updateDetections(null,false,1,.5);
  } finally { pending = false; }
}
$('armButton').addEventListener('click',async () => {
  if (commandPending || offlinePreview) return;
  const stop = !trackingKnown || trackingActive;
  if (!stop && !window.confirm('請確認 Servo 中位與方向已驗證，周圍淨空。開始追蹤？')) return;
  commandNotice = '';
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
  const layer = $('detectionOverlay'), img = $('stream');
  const width = img.clientWidth, height = img.clientHeight;
  layer.replaceChildren();
  if (!width || !height || !img.naturalWidth || !img.naturalHeight) return;
  layer.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const names = {chasing_wave: '追', takeoff: '起', surfing: '衝'};
  function svg(tag, attributes) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
  }
  for (const item of detectionItems) {
    const b = detectionRect(item.box, width, height, img.naturalWidth, img.naturalHeight, detectionZoom, detectionCenter);
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
$('stream').addEventListener('load', drawDetections);
if (typeof ResizeObserver !== 'undefined') new ResizeObserver(drawDetections).observe($('stream'));
else window.addEventListener('resize', drawDetections);
