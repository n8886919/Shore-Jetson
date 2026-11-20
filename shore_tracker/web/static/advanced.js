const $ = id => document.getElementById(id);
const embedded = !!$('controlsPanel');
const stateNames = {manual: '雲台手動控制', ready: '就緒', idle: '待命（尚未辨識）', waiting: '載入中', starting: '啟動中', offline: '離線', error: '異常', paused: '已暫停', stale: '資料逾時'};
const recoveryNames = {ready: '持續監看 · 自動恢復已啟用', queued: '等待重新連接', reconnecting: '正在重新連接', waiting: '等待串流恢復', blocked: '等待舊串流資源釋放'};
let pending = false;
const display = (id, value) => {
  const node = $(id) || (embedded && ['cameraMessage','recordStatus','tuningMessage'].includes(id) ? $('controlFeedback') : null);
  if (node) {
    node.textContent = value ?? '—';
    if (node === $('controlFeedback')) node.hidden = !value || value === '已套用';
    if (id === 'commandResult') node.hidden = !value;
  }
};
function render(status, diagnostic) {
  const online = diagnostic.available && !status.offline_preview;
  window.shoreInferenceControl?.render(status, online);
  window.shoreKeyboardControl?.render(status, online);
  display('connection', online ? (status.tracking.armed ? '追蹤中' : status.tracking.mode === 'arming' ? '啟動追蹤中' : '待命') : '離線版面預覽');
  if ($('connection')) $('connection').className = online ? 'badge ready' : 'badge';
  $('components')?.replaceChildren(...Object.entries({camera: '相機', inference: '辨識', servo: '舵機串口', recording: '錄影'}).map(([key, name]) => {
    const item = document.createElement('span');
    const state = status.components[key]?.state || 'offline';
    item.className = `component ${state === 'ready' || state === 'idle' ? 'ready' : ''}`;
    item.textContent = `${name} ${stateNames[state] || state}`;
    item.title = status.components[key]?.detail || '';
    return item;
  }));
  const issues = [];
  if (online) {
    const messages = {camera:'影像尚未就緒：請檢查相機電源與網路，系統會自動重連。',inference:'辨識尚未就緒：請稍候；若持續異常，請查看下方連線與診斷。',servo:'舵機連線異常：請檢查控制板電源與接線。',recording:'錄影尚未就緒：目前不保證畫面已錄下，請查看下方連線與診斷。'};
    for (const [key, message] of Object.entries(messages)) {
      if (status.components[key]?.state !== 'ready' && !(key === 'inference' && status.components[key]?.state === 'idle') && !(key === 'servo' && status.components[key]?.state === 'manual')) {
        issues.push({camera:'影像中斷',inference:'辨識未就緒',servo:'舵機未連線',recording:'錄影未就緒'}[key]);
      }
    }
    if (status.metrics.disk_pressure === 'stop') issues.push('儲存空間不足');
    else if (status.metrics.disk_pressure === 'warning') issues.push('儲存空間偏低');
    if (status.manual_recording?.state === 'failed') issues.push('錄影失敗');
  }
  display('operatorIssue', issues.join('\n'));
  $('operatorIssue').hidden = issues.length === 0;
  renderControls(status, online, diagnostic);
  display('targetId', online && status.tracking.armed && status.tracking.target_track_id != null ? `#${status.tracking.target_track_id}` : '—');
  display('trackingPhase', online ? ({manual:'方向鍵手動控制',disarmed:'待命',arming:'啟動中',searching:'尋找衝浪者',chasing:'追浪試拍',takeoff:'起乘',surfing:'衝浪中',finished:'本次跟拍結束'}[status.tracking.mode] || '等待狀態') : '尚未連線');
  const metrics = status.metrics;
  display('inferenceFps', online && Number.isFinite(metrics.inference_fps) ? Math.round(metrics.inference_fps) : '—');
  display('frameAge', online && Number.isFinite(metrics.camera_frame_age_ms) ? `${Math.round(metrics.camera_frame_age_ms)} ms` : '—');
  const recovery = diagnostic.recovery || {};
  display('recoveryState', recoveryNames[recovery.state] || '實機連線後顯示');
  display('attempts', recovery.attempts);
  display('lastReason', {manual: '手動重新連接', stream_unavailable: '影像或錄影片段停止更新'}[recovery.last_reason] || '尚無');
  display('lastAttempt', recovery.last_attempt_at ? new Date(recovery.last_attempt_at).toLocaleTimeString('zh-TW', {hour12: false}) : '尚無');
  display('finalized', recovery.previous_recording_finalized == null ? '尚無' : recovery.previous_recording_finalized ? '已確認完成' : '未確認完整 · 已保留片段');
  const video = diagnostic.video || {}, servo = diagnostic.servo || {};
  display('codec', video.codec?.toUpperCase());
  display('rotation', video.rotation_degrees == null ? null : `${video.rotation_degrees}°`);
  display('targetFps', video.preview_fps == null ? null : `${video.preview_fps} FPS`);
  display('protocol', servo.protocol === 'm0_pulse' ? 'M0 串口控制板' : servo.protocol);
  display('serial', servo.serial_device ? `${servo.serial_device} · ${servo.serial_baud}` : null);
  display('angles', servo.home_deg == null ? null : `${servo.angle_min_deg}–${servo.angle_max_deg}° / ${servo.home_deg}°`);
  const progress = {queued:'等待重新連接相機…', reconnecting:'正在重新連接相機…',
    waiting:'等待相機影像恢復…', blocked:'影像資源尚未釋放，系統會繼續重試'};
  display('commandResult', online ? progress[recovery.state] || '' : '');
  display('updated', `更新於 ${new Date().toLocaleTimeString('zh-TW', {hour12: false})}`);
}
async function refresh() {
  if (pending) return;
  pending = true;
  try {
    const responses = await Promise.all(['/api/status', '/api/diagnostics'].map(path => fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(4000)})));
    if (responses.some(response => !response.ok)) throw new Error('unavailable');
    const [status, diagnostic] = await Promise.all(responses.map(response => response.json()));
    render(status, diagnostic);
  } catch (_) {
    window.shoreInferenceControl?.disconnect();
    window.shoreKeyboardControl?.disconnect();
    controlsOnline = false;
    controlStreamDirty = true;
    if (!embedded) { $('controlStream').style.visibility = 'hidden'; $('controlNoSignal').hidden = false; }
    display('controlNoSignal', '等待服務連線恢復');
    updateControlButtons();
    display('operatorIssue', '網頁無法連接 Jetson 服務，追蹤狀態未知。可嘗試停止追蹤；連線恢復後請重新確認。');
    $('operatorIssue').hidden = false;
    display('connection', '服務連線中斷');
    if ($('connection')) $('connection').className = 'badge';
    display('commandResult', '等待服務恢復連線');
    display('updated', '資料已過期，恢復後會自動更新');
    for (const id of ['inferenceFps', 'frameAge', 'targetId', 'trackingPhase']) display(id, '—');
  } finally { pending = false; }
}
refresh();
setInterval(refresh, 2000);

let controlsOnline = false, controlsOfflinePreview = true, controlsArmed = true;
let lensState = null, lensPending = false, lensRetryAt = 0, cameraReady = false;
let appliedContinuous = false, recordPending = false, recordState = 'unavailable', recordingModeAvailable = false;
let tuningLoaded = false, tuningPending = false, tuningDirection = null, tuningQueued = null;
let appliedPanGain = null;
let appliedTiltEnabled = false, tiltAvailable = false;
let controlStreamDirty = true, controlStreamRetryAt = 0;
const mode = new URLSearchParams(location.search).get('mode') === 'mobile' ? 'mobile' : 'kiosk';
if ($('backLink')) $('backLink').href = `/${mode}`;
$('controlStream')?.addEventListener('error', () => { controlStreamDirty = true; controlStreamRetryAt = Date.now()+1000; });
function renderControls(status, online, diagnostic) {
  controlsOnline = online;
  controlsOfflinePreview = status.offline_preview === true;
  controlsArmed = status.tracking.armed || status.tracking.mode === 'arming';
  const wasCameraReady = cameraReady;
  cameraReady = status.components.camera.state === 'ready';
  if (cameraReady && !wasCameraReady) lensRetryAt = 0;
  tuningDirection = status.tuning.pan_direction;
  appliedPanGain = status.tuning.pan_gain_deg;
  appliedTiltEnabled = status.tuning.tilt_enabled === true;
  tiltAvailable = status.tuning.tilt_available === true;
  if (!tuningPending && $('tiltEnabled')) $('tiltEnabled').checked = appliedTiltEnabled;
  const recording = status.manual_recording || {};
  recordState = recording.state || 'unavailable';
  appliedContinuous = recording.continuous_enabled === true;
  recordingModeAvailable = typeof recording.continuous_enabled === 'boolean';
  if (!recordPending) $('continuousRecording').checked = appliedContinuous;
  if ($('recordStatus')) display('recordStatus',recordState === 'recording'
    ? recording.continuous_requested ? '連續錄影中' : '自動跟拍錄影中'
    : recordState === 'failed' ? '錄影失敗' : recordState === 'finalizing' ? '正在完成影片'
    : recordState === 'starting' ? '錄影準備中' : '錄影待命');
  const angle = status.metrics.servo_angle_deg;
  if ($('servoAngleSlider')) $('servoAngleSlider').value = Number.isFinite(angle) ? angle : 90;
  display('servoTargetAngle',Number.isFinite(angle) ? `${angle.toFixed(1)}°` : '—');
  if (!tuningLoaded) {
    $('deadband').value = status.tuning.pan_deadband;
    $('zoomMax').value = status.tuning.zoom_maximum;
    tuningLoaded = true;
    showTuning();
  }
  if (!cameraReady) { controlStreamDirty = true; lensState = null; }
  if (!embedded) {
  if (cameraReady && controlStreamDirty && Date.now() >= controlStreamRetryAt) {
    controlStreamDirty = false;
    $('controlStream').src = `/api/preview.mjpeg?mode=web&control=${Date.now()}`;
  }
  const visible = cameraReady && !controlStreamDirty;
  $('controlNoSignal').hidden = visible;
  $('controlStream').style.visibility = visible ? 'visible' : 'hidden';
  display('controlNoSignal',controlsOfflinePreview ? '離線預覽 · 未連接相機' : '等待相機畫面');
  const zoom = Number(status.metrics.zoom_scale || 1), center = Number(status.metrics.zoom_center_x || .5);
  $('controlStream').style.transformOrigin = `${center*100}% 50%`;
  $('controlStream').style.transform = `scale(${zoom})`;
  }
  if (online && cameraReady && !lensState && !lensPending && Date.now() >= lensRetryAt) readLens();
  updateControlButtons();
}
function updateControlButtons() {
  if ($('controlStop')) $('controlStop').disabled = controlsOfflinePreview || stopPending;
  const blocked = !controlsOnline || controlsArmed || lensPending || !cameraReady;
  for (const button of document.querySelectorAll('[data-camera-action]')) {
    button.disabled = blocked || !(button.dataset.cameraAction.startsWith('zoom') ? lensState?.zoom : lensState?.focus);
  }
  $('continuousRecording').disabled = !controlsOnline || controlsOfflinePreview || recordPending || !recordingModeAvailable;
  for (const id of ['deadband','zoomMax']) $(id).disabled = !controlsOnline && !controlsOfflinePreview;
  if ($('tiltEnabled')) $('tiltEnabled').disabled = !controlsOnline || controlsOfflinePreview || !tiltAvailable;
}
async function readLens() {
  lensPending = true;
  lensRetryAt = Date.now()+30000;
  updateControlButtons();
  try {
    const response = await fetch('/api/camera',{cache:'no-store',signal:AbortSignal.timeout(8000)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.detail || '無法讀取鏡頭狀態');
    lensState = data;

  } catch (error) { lensState = null; display('cameraMessage',error.message || '鏡頭控制尚未就緒'); }
  finally { lensPending = false; updateControlButtons(); }
}
for (const button of document.querySelectorAll('[data-camera-action]')) button.addEventListener('click',async () => {
  if (!controlsOnline || controlsArmed || lensPending || !lensState) return;
  lensPending = true;
  updateControlButtons();
  display('cameraMessage','調整中…');
  try {
    const response = await fetch('/api/camera',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:button.dataset.cameraAction,values:{}}),signal:AbortSignal.timeout(8000)});
    const result = await response.json();
    display('cameraMessage',result.ok ? '已完成' : result.detail || '調整未完成');
  } catch (_) { display('cameraMessage','回覆中斷，請確認鏡頭已停止'); }
  finally { lensPending = false; updateControlButtons(); }
});
$('continuousRecording').addEventListener('change',async () => {
  if (!controlsOnline || controlsOfflinePreview || recordPending || !recordingModeAvailable) return;
  const enabled = $('continuousRecording').checked;
  recordPending = true;
  updateControlButtons();
  try {
    const response = await fetch('/api/recording/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({continuous:enabled}),signal:AbortSignal.timeout(5000)});
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.detail || '錄影設定未套用');
    const check = await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if (!check.ok) throw new Error('無法確認錄影設定');
    const status = await check.json();
    if (status.manual_recording?.continuous_enabled !== enabled) throw new Error('錄影設定讀回不一致');
    appliedContinuous = enabled;
    display('recordStatus','已套用');
  } catch (error) { display('recordStatus',error.message || '錄影設定未確認'); }
  finally { recordPending = false; $('continuousRecording').checked = appliedContinuous; await refresh(); updateControlButtons(); }
});
function showTuning() {
  const tolerance = `${Math.round(Number($('deadband').value)*200)}%`;
  display('deadbandValue',tolerance);
  $('deadband').setAttribute('aria-valuetext',tolerance);
  display('zoomMaxValue',`${Number($('zoomMax').value).toFixed(1)}×`);
}
for (const id of ['deadband','zoomMax']) {
  $(id).addEventListener('input',showTuning);
  $(id).addEventListener('change',applyTuning);
}
$('tiltEnabled')?.addEventListener('change',applyTuning);
async function applyTuning() {
  if (!controlsOnline || controlsOfflinePreview || !tuningLoaded) return;
  tuningQueued = {pan_gain_deg:appliedPanGain,pan_deadband:Number($('deadband').value),pan_direction:tuningDirection,zoom_maximum:Number($('zoomMax').value)};
  if ($('tiltEnabled')) tuningQueued.tilt_enabled = $('tiltEnabled').checked;
  if (tuningPending) return;
  tuningPending = true;
  try {
    while (tuningQueued && controlsOnline) {
      const values = tuningQueued;
      tuningQueued = null;
      display('tuningMessage','套用中…');
      const response = await fetch('/api/tuning',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values),signal:AbortSignal.timeout(8000)});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || '參數未套用');
      const check = await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(8000)});
      if (!check.ok) throw new Error('無法確認參數，請重試');
      const status = await check.json();
      if (!Object.entries(values).every(([key,value]) => status.tuning?.[key] === value)) throw new Error('參數讀回不一致，請重試');
      appliedTiltEnabled = status.tuning.tilt_enabled === true;
      display('tuningMessage','已套用');
    }
  } catch (error) { display('tuningMessage',error.message || '無法確認參數，請重試'); }
  finally { tuningPending = false; tuningQueued = null; if ($('tiltEnabled')) $('tiltEnabled').checked = appliedTiltEnabled; }
}

let stopPending = false;
$('controlStop')?.addEventListener('click', async () => {
  if (stopPending || controlsOfflinePreview) return;
  stopPending = true;
  updateControlButtons();
  try {
    const response = await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(5000)});
    const result = await response.json();
    display('controlStopMessage',result.ok ? '已送出停止追蹤指令' : result.detail || '停止指令未確認');
  } catch (_) { display('controlStopMessage','停止回覆未確認，請檢查服務連線'); }
  finally { stopPending = false; await refresh(); updateControlButtons(); }
});
