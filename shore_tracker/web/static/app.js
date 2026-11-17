const names = {camera: '相機', inference: '辨識', servo: '追蹤', recording: '錄影'};
const $ = (id) => document.getElementById(id);
const score = (value) => value == null ? 0 : Number(value);
const phaseNames = {disarmed: '已停止', arming: '啟動中', searching: '等候目標 · 90°', chasing: '追浪試拍 · 等待起乘', takeoff: '起乘', surfing: '衝浪', finished: '返回 90°'};
let tuningLoaded = false;
let statusPending = false;
let detectionItems = [];
let detectionZoom = 1;
let detectionCenter = .5;
let detectionTimer = null;
let panDirection = null;
let cameraConnected = false;
let cameraReadRetryAt = 0;
let streamNeedsReconnect = true;
let streamRetryAt = 0;
let cameraArmed = false;
let cameraPending = 0;
let cameraState = null;
let recordingState = "unavailable";
let recordingPending = false;
let continuousRecordingEnabled = false;
let manualRecordingRequested = false;
let offlinePreview = true;
let tuningReady = false;
let tuningPending = false;
let queuedTuning = null;
let trackingActive = false;
let trackingKnown = false;
let trackingPending = false;
const previousLogValues = new Map();

function appendLog(message) {
  const viewport = $('logWindow');
  const follow = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop < 24;
  const row = document.createElement('div');
  row.className = 'log-line';
  row.textContent = `${new Date().toLocaleTimeString('zh-TW', {hour12: false})} ${String(message).replace(/\s+/g, ' ').trim()}`;
  $('logEntries').append(row);
  while ($('logEntries').children.length > 100) {
    $('logEntries').firstElementChild.remove();
    if (!follow) viewport.scrollTop = Math.max(0, viewport.scrollTop - 18);
  }
  if (follow) viewport.scrollTop = viewport.scrollHeight;
}

function logChange(key, value, message) {
  if (previousLogValues.get(key) === value) return;
  previousLogValues.set(key, value);
  if (message) appendLog(message);
}

function renderSystemStatus(data) {
  const items = Object.entries(names).map(([key, name]) => {
    const state = data?.components?.[key]?.state || 'offline';
    const item = document.createElement('span');
    item.className = `status-light${state === 'ready' ? ' ready' : ''}`;
    item.textContent = name;
    item.title = `${name}:${state}${data?.components?.[key]?.detail ? ' · ' + data.components[key].detail : ''}`;
    item.setAttribute('aria-label', item.title);
    return item;
  });
  const disk = document.createElement('span');
  disk.className = 'status-disk';
  disk.textContent = `磁碟:${Number.isFinite(data?.metrics?.free_gib) ? data.metrics.free_gib.toFixed(1) + 'G' : '—'}`;
  disk.title = `可用磁碟空間（GiB） ${data?.metrics?.disk_pressure || ''}`;
  const file = document.createElement('span');
  file.className = 'status-file';
  file.textContent = `檔案:${data?.metrics?.last_recording || '—'}`;
  file.title = `最近${file.textContent}`;
  $('liveInfo').replaceChildren(...items, disk, file);
}

$('stream').addEventListener('error', () => {
  streamNeedsReconnect = true;
  streamRetryAt = Date.now() + 1000;
});

async function post(path, payload = {}) {
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload), signal: AbortSignal.timeout(5000)
    });
    const body = await response.json();
    appendLog(`${path === '/api/arm' ? '開始追蹤' : '停止追蹤'}：${body.ok ? '指令已接受' : body.detail || '指令被拒絕'}`);
    if (!body.ok) $('armReason').textContent = body.detail || '指令被拒絕';
  } catch (_) {
    appendLog('控制指令失敗：本機控制服務無回應');
    $('armReason').textContent = '本機控制服務無回應';
  }
  await refresh();
}

function showTuning() {
  $('panGainValue').textContent = Number($('panGain').value).toFixed(0);
  const tolerance = `${Math.round(Number($('deadband').value)*200)}%`;
  $('deadbandValue').textContent = tolerance;
  $('deadband').setAttribute('aria-valuetext',tolerance);
  $('zoomMaxValue').textContent = `${Number($('zoomMax').value).toFixed(1)}×`;
}

function render(data) {
  offlinePreview = data.offline_preview === true;
  tuningReady = true;
  const recording = data.manual_recording || {state: 'unavailable'};
  recordingState = recording.state;
  const recordingActive = ['starting', 'recording'].includes(recordingState);
  manualRecordingRequested = recording.manual_requested ?? recordingActive;
  const automaticRecording = recording.tracking_requested === true;
  continuousRecordingEnabled = recording.continuous_enabled === true;
  if (!recordingPending) $('continuousRecording').checked = continuousRecordingEnabled;
  $('continuousRecording').disabled = offlinePreview || recordingPending || typeof recording.continuous_enabled !== 'boolean';
  $('recordStatus').textContent = (recording.continuous_requested ? '連續錄影 · ' : automaticRecording ? (recording.tracking_qualified ? '自動跟拍' : '追浪試拍') + ' · ' : manualRecordingRequested ? '手動 · ' : '') + (recording.detail || '') + (recordingActive ? ` · ${recording.elapsed_seconds || 0} 秒` : '');
  const tracking = data.tracking;
  const metrics = data.metrics;
  cameraArmed = tracking.armed || tracking.mode === 'arming';
  trackingActive = cameraArmed;
  trackingKnown = true;
  updateCameraButtons();
  $('systemMode').hidden = data.offline_preview === true;
  $('systemMode').textContent = tracking.armed ? `ARMED · ${phaseNames[tracking.mode] || tracking.mode}` : 'DISARMED';
  $('systemMode').className = `pill ${tracking.armed ? 'armed' : 'muted'}`;
  $('armButton').textContent = tracking.mode === 'arming' ? '取消啟動' : trackingActive ? '停止追蹤' : '開始追蹤';
  $('armButton').className = trackingActive ? 'stop' : 'arm';
  $('armButton').disabled = offlinePreview || trackingPending || (!trackingActive && !data.arm_allowed);
  $('armButton').title = trackingActive ? (data.servo_command?.protocol === 'pelco_d' ? '送出雲台停止指令，不切斷電源' : '停止傳送新目標，不切斷 Servo 電源') : '開始追蹤';
  $('armReason').textContent = tracking.armed ? tracking.reason : data.arm_reason;
  $('armReason').hidden = data.offline_preview === true;
  $('phase').textContent = phaseNames[tracking.mode] || tracking.mode;
  $('targetId').textContent = tracking.target_track_id ?? '—';
  const servoTarget = Number.isFinite(metrics.servo_angle_deg) ? `${metrics.servo_angle_deg.toFixed(1)}°` : '—';
  $('servoAngle').textContent = servoTarget;
  $('servoTargetAngle').textContent = servoTarget;
  $('servoAngleSlider').value = Number.isFinite(metrics.servo_angle_deg) ? metrics.servo_angle_deg : 90;
  $('servoAngleSlider').dataset.available = String(Number.isFinite(metrics.servo_angle_deg));
  $('servoAngleSlider').setAttribute('aria-valuetext', servoTarget === '—' ? '尚無角度資料' : servoTarget);
  $('zoomScale').textContent = `${Number(metrics.zoom_scale || 1).toFixed(1)}×`;
  $('inferenceFps').textContent = metrics.inference_fps == null ? '—' : metrics.inference_fps.toFixed(1);
  $('previewFps').textContent = Number.isFinite(metrics.preview_fps) ? metrics.preview_fps.toFixed(1) : '—';
  const components = Object.entries(data.components).map(([key, value]) => `${names[key] || key}:${value.state}`).join(' ');
  const disk = metrics.free_gib == null ? '—' : `${metrics.free_gib.toFixed(1)}GiB`;
  renderSystemStatus(data);
  logChange('connection', 'ready', previousLogValues.has('connection') ? '服務已恢復連線' : '狀態已連線');
  logChange('components', components, components);
  logChange('tracking', `${tracking.mode}:${tracking.target_track_id ?? ''}:${tracking.reason || ''}`, `追蹤:${phaseNames[tracking.mode] || tracking.mode} 主角:${tracking.target_track_id ?? '—'} ${tracking.reason || ''}`);
  const recordSummary = `錄影:${recordingState} ${automaticRecording ? '自動' : ''}${manualRecordingRequested ? ' 手動' : ''} ${recording.detail || ''}`;
  logChange('recording', recordSummary, recordSummary);
  logChange('file', metrics.last_recording, metrics.last_recording ? `最近檔案:${metrics.last_recording}` : '');
  logChange('disk', metrics.disk_pressure, metrics.disk_pressure ? `磁碟:${metrics.disk_pressure} 剩餘:${disk}` : '');
  const zoom = Number(metrics.zoom_scale || 1);
  const center = Number(metrics.zoom_center_x || 0.5);
  $('stream').style.transformOrigin = `${center * 100}% 50%`;
  $('stream').style.transform = `scale(${zoom})`;
  const cameraReady = data.components.camera.state === 'ready';
  if (!cameraReady) cameraState = null;
  if (cameraReady && (!cameraConnected || !cameraState) && cameraPending === 0 && Date.now() >= cameraReadRetryAt) readCamera();
  cameraConnected = cameraReady;
  updateCameraButtons();
  panDirection = data.tuning.pan_direction;
  if (!cameraReady) streamNeedsReconnect = true;
  if (cameraReady && streamNeedsReconnect && Date.now() >= streamRetryAt) {
    streamNeedsReconnect = false;
    $('stream').src = `/api/preview.mjpeg?reconnect=${Date.now()}`;
  }
  const hasPicture = cameraReady && !streamNeedsReconnect;
  $('noSignal').hidden = hasPicture;
  $('stream').style.visibility = hasPicture ? 'visible' : 'hidden';
  $('streamNotice').style.display = hasPicture ? 'none' : 'grid';
  $('streamNotice').textContent = cameraReady ? '影像重新連線中' : '等待相機畫面';
  updateDetections(data.recognition, hasPicture, zoom, center);
  if (!tuningLoaded) {
    $('panGain').value = data.tuning.pan_gain_deg;
    $('deadband').value = data.tuning.pan_deadband;
    $('zoomMax').value = data.tuning.zoom_maximum;
    tuningLoaded = true;
    showTuning();
  }
}

async function refresh() {
  if (statusPending) return;
  statusPending = true;
  try {
    const response = await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(3000)});
    render(await response.json());
  } catch (_) {
    tuningReady = false;
    trackingKnown = false;
    $('armButton').textContent = '停止追蹤 · 狀態未知';
    $('armButton').className = 'stop';
    $('armButton').disabled = offlinePreview || trackingPending;
    renderSystemStatus(null);
    logChange('connection', 'offline', '服務連線中斷');
    streamNeedsReconnect = true;
    cameraArmed = true;
    updateCameraButtons();
    $('continuousRecording').disabled = true;
    updateDetections(null, false, 1, .5);
    $('noSignal').hidden = false;
    $('stream').style.visibility = 'hidden';
    $('streamNotice').style.display = 'grid';
    $('streamNotice').textContent = '連線中斷';
    $('systemMode').textContent = '服務中斷';
    $('previewFps').textContent = '—';
    $('inferenceFps').textContent = '—';
    $('servoAngle').textContent = '—';
    $('servoTargetAngle').textContent = '—';
    $('servoAngleSlider').dataset.available = 'false';
    $('servoAngleSlider').setAttribute('aria-valuetext', '連線中斷');
    $('systemMode').className = 'pill warn';
  } finally {
    statusPending = false;
  }
}

$('armButton').addEventListener('click', async () => {
  if (trackingPending || offlinePreview) return;
  const stop = !trackingKnown || trackingActive;
  trackingPending = true;
  $('armButton').disabled = true;
  try { await post(stop ? '/api/stop' : '/api/arm'); }
  finally { trackingPending = false; await refresh(); }
});
for (const id of ['panGain', 'deadband', 'zoomMax']) {
  $(id).addEventListener('input', showTuning);
  $(id).addEventListener('change', applyTuning);
}
async function applyTuning() {
  if (!tuningLoaded || offlinePreview) return;
  if (!tuningReady) { appendLog('設定未送出：服務尚未連線'); return; }
  queuedTuning = {
    pan_gain_deg: Number($('panGain').value),
    pan_deadband: Number($('deadband').value),
    pan_direction: panDirection,
    zoom_maximum: Number($('zoomMax').value)
  };
  if (tuningPending) return;
  tuningPending = true;
  try {
    while (queuedTuning && tuningReady && !offlinePreview) {
      const values = queuedTuning;
      queuedTuning = null;
      try {
        const response = await fetch('/api/tuning', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(values), signal: AbortSignal.timeout(8000)
        });
        const body = await response.json();
        if (!response.ok || !body.ok) throw new Error(body.detail || '設定被拒絕');
        const check = await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(8000)});
        if (!check.ok) throw new Error('已送出，但無法確認目前設定，請重試');
        const data = await check.json();
        if (!Object.entries(values).every(([key, value]) => data.tuning?.[key] === value)) {
          throw new Error('讀回的設定不同，請重新套用');
        }
        appendLog(`設定已生效 P:${values.pan_gain_deg} 容許區:${Math.round(values.pan_deadband*200)}% Zoom:${values.zoom_maximum.toFixed(1)}×`);
        render(data);
      } catch (error) {
        appendLog(error.name === 'TimeoutError' || error.name === 'TypeError'
          ? '連線中斷或逾時，尚未確認套用結果，請重試'
          : `設定未生效：${error.message}`);
      }
    }
  } finally {
    tuningPending = false;
    queuedTuning = null;
  }
}
setInterval(refresh, 200);
refresh();

function updateCameraButtons() {
  const blocked = cameraArmed || cameraPending > 0 || !cameraState || !cameraConnected;
  for (const button of document.querySelectorAll('[data-camera-action]')) {
    const supported = button.dataset.cameraAction.startsWith('zoom') ? cameraState?.zoom : cameraState?.focus;
    button.disabled = blocked || !supported;
    button.title = cameraArmed ? '請先 STOP 追蹤再調整鏡頭' : !cameraConnected ? '相機未連線' : '小幅調整後自動停止';
  }
}

async function readCamera() {
  cameraReadRetryAt = Date.now() + 30000;
  cameraPending++;
  updateCameraButtons();
  try {
    const response = await fetch('/api/camera', {cache: 'no-store', signal: AbortSignal.timeout(8000)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.detail || '鏡頭控制未就緒');
    cameraState = data;
    $('cameraMessage').textContent = '';
  } catch (error) {
    cameraState = null;
    $('cameraMessage').textContent = error.message || '鏡頭控制未就緒';
  } finally {
    cameraPending--;
    updateCameraButtons();
  }
}

async function cameraCommand(action) {
  if (cameraArmed || cameraPending > 0 || !cameraConnected || !cameraState) return;
  cameraPending++;
  updateCameraButtons();
  $('cameraMessage').textContent = '調整中…';
  try {
    const response = await fetch('/api/camera', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, values: {}})
    });
    const data = await response.json();
    $('cameraMessage').textContent = response.ok && data.ok ? '' : data.detail || '鏡頭調整失敗';
    const actionName = {zoom_in: '放大', zoom_out: '縮小', focus_near: '近焦', focus_far: '遠焦'}[action] || action;
    appendLog(`鏡頭${actionName}：${$('cameraMessage').textContent || '已完成'}`);
  } catch (_) {
    $('cameraMessage').textContent = '操作回覆中斷，請確認鏡頭已停止';
    appendLog($('cameraMessage').textContent);
  } finally {
    cameraPending--;
    updateCameraButtons();
  }
}

for (const button of document.querySelectorAll('[data-camera-action]')) {
  button.addEventListener('click', () => cameraCommand(button.dataset.cameraAction));
}

$('continuousRecording').addEventListener('change', async () => {
  if (offlinePreview || recordingPending) return;
  const enabled = $('continuousRecording').checked;
  recordingPending = true;
  $('continuousRecording').disabled = true;
  try {
    const response = await fetch('/api/recording/mode', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({continuous:enabled})});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.detail || '錄影設定未套用');
    const check = await fetch('/api/status',{cache:'no-store'});
    if (!check.ok) throw new Error('無法確認錄影設定');
    const status = await check.json();
    if (status.manual_recording?.continuous_enabled !== enabled) throw new Error('錄影設定讀回不一致');
    continuousRecordingEnabled = enabled;
    appendLog('連續錄影：'+(enabled ? '開啟' : '關閉'));
  } catch (error) { $('recordStatus').textContent = error.message || '錄影設定未確認'; }
  finally { recordingPending = false; $('continuousRecording').checked = continuousRecordingEnabled; }
  await refresh();
});

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

// Match object-fit: contain plus the image element's digital zoom transform.
function detectionRect(box, width, height, naturalWidth, naturalHeight, zoom, center) {
  const scale = Math.min(width / naturalWidth, height / naturalHeight);
  const imageWidth = naturalWidth * scale, imageHeight = naturalHeight * scale;
  const originX = width * center, originY = height / 2;
  return {
    x: ((width-imageWidth)/2 + box.x*imageWidth-originX)*zoom+originX,
    y: ((height-imageHeight)/2 + box.y*imageHeight-originY)*zoom+originY,
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
