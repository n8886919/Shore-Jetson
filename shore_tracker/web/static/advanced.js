const $ = id => document.getElementById(id);
const embedded = !!$('controlsPanel');
const stateNames = {ready: '就緒', starting: '啟動中', offline: '離線', error: '異常', paused: '已暫停', stale: '資料逾時'};
const recoveryNames = {ready: '持續監看 · 自動恢復已啟用', queued: '等待重新連接', reconnecting: '正在重新連接', waiting: '等待串流恢復', blocked: '等待舊串流資源釋放'};
let pending = false;
let commandPending = false;
let reconnectAllowed = false;
const display = (id, value) => { const node=$(id); if (node) node.textContent = value ?? '—'; };
function render(status, diagnostic) {
  const online = diagnostic.available && !status.offline_preview;
  display('connection', online ? (status.tracking.armed ? '追蹤中' : status.tracking.mode === 'arming' ? '啟動追蹤中' : '待命') : '離線版面預覽');
  $('connection').className = online ? 'badge ready' : 'badge';
  $('components').replaceChildren(...Object.entries({camera: '相機', inference: '辨識', servo: '控制串口', recording: '錄影'}).map(([key, name]) => {
    const item = document.createElement('span');
    const state = status.components[key]?.state || 'offline';
    item.className = `component ${state === 'ready' ? 'ready' : ''}`;
    item.textContent = `${name} ${stateNames[state] || state}`;
    item.title = status.components[key]?.detail || '';
    return item;
  }));
  const issues = [];
  if (online) {
    const messages = {camera:'影像尚未就緒：請檢查相機電源與網路，系統會自動重連。',inference:'辨識尚未就緒：請稍候；若持續異常，請查看下方連線與診斷。',servo:'控制連線異常：請檢查控制板電源與接線。',recording:'錄影尚未就緒：目前不保證畫面已錄下，請查看下方連線與診斷。'};
    for (const [key, message] of Object.entries(messages)) {
      if (status.components[key]?.state !== 'ready') {
        const detail = status.components[key]?.detail;
        issues.push(message + (detail ? ` 原因：${detail}` : ''));
      }
    }
    if (status.metrics.disk_pressure === 'stop') issues.push('儲存空間不足：請先備份並釋放空間，再重新確認錄影狀態。');
    else if (status.metrics.disk_pressure === 'warning') issues.push('儲存空間偏低：請安排備份並釋放空間。');
    if (status.manual_recording?.state === 'failed') issues.push('錄影未完整完成，未完成檔案已保留。請查看下方診斷後再試。');
  }
  display('operatorIssue', issues.join('\n'));
  $('operatorIssue').hidden = issues.length === 0;
  renderControls(status, online, diagnostic);
  display('targetId', online && status.tracking.armed && status.tracking.target_track_id != null ? `#${status.tracking.target_track_id}` : '尚未鎖定');
  display('trackingPhase', online ? ({disarmed:'待命',arming:'啟動中',searching:'尋找衝浪者',chasing:'追浪試拍',takeoff:'起乘',surfing:'衝浪中',finished:'本次跟拍結束'}[status.tracking.mode] || '等待狀態') : '尚未連線');
  const metrics = status.metrics;
  display('previewFps', online && Number.isFinite(metrics.preview_fps) ? metrics.preview_fps.toFixed(1) : '—');
  display('inferenceFps', online && Number.isFinite(metrics.inference_fps) ? metrics.inference_fps.toFixed(1) : '—');
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
  const recording = status.manual_recording || {};
  const busy = ['queued', 'reconnecting'].includes(recovery.state);
  const recordingBusy = recording.state !== 'idle' || recording.manual_requested || recording.tracking_requested;
  const reason = !online ? '離線預覽無法控制實機' : status.tracking.armed || status.tracking.mode === 'arming' ? '請先停止追蹤' : recordingBusy ? '請先結束錄影並等待收尾' : busy ? '正在重新連接，請稍候' : metrics.disk_pressure === 'stop' ? '請先處理儲存空間' : '';
  reconnectAllowed = !reason;
  $('reconnectButton').disabled = commandPending || !reconnectAllowed;
  display('reconnectReason', reason || '僅重新建立影像連線');
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
    controlsOnline = false;
    controlStreamDirty = true;
    if (!embedded) { $('controlStream').style.visibility = 'hidden'; $('controlNoSignal').hidden = false; }
    display('controlNoSignal', '等待服務連線恢復');
    updateControlButtons();
    reconnectAllowed = false;
    $('reconnectButton').disabled = true;
    display('operatorIssue', '網頁無法連接 Jetson 服務，追蹤狀態未知。可嘗試停止追蹤；連線恢復後請重新確認。');
    $('operatorIssue').hidden = false;
    display('connection', '服務連線中斷');
    $('connection').className = 'badge';
    display('reconnectReason', '等待服務恢復連線');
    display('updated', '資料已過期，恢復後會自動更新');
    for (const id of ['previewFps', 'inferenceFps', 'frameAge', 'targetId', 'trackingPhase']) display(id, '—');
  } finally { pending = false; }
}
$('reconnectButton').addEventListener('click', async () => {
  if (commandPending || !reconnectAllowed) return;
  commandPending = true;
  $('reconnectButton').disabled = true;
  display('commandResult', '送出重新連接請求…');
  try {
    const response = await fetch('/api/camera/reconnect', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}', signal: AbortSignal.timeout(5000)});
    const result = await response.json();
    display('commandResult', result.detail || '請求未完成');
  } catch (_) { display('commandResult', '回覆中斷，請等待狀態更新後再確認結果'); }
  finally { commandPending = false; await refresh(); }
});
refresh();
setInterval(refresh, 2000);

let controlsOnline = false, controlsOfflinePreview = true, controlsArmed = true;
let lensState = null, lensPending = false, lensRetryAt = 0, cameraReady = false;
let manualRequested = false, recordPending = false, recordState = 'unavailable';
let tuningLoaded = false, tuningPending = false, tuningDirection = null, tuningQueued = null;
let controlStreamDirty = true, controlStreamRetryAt = 0;
const mode = new URLSearchParams(location.search).get('mode') === 'mobile' ? 'mobile' : 'kiosk';
if ($('backLink')) $('backLink').href = `/${mode}`;
$('controlStream')?.addEventListener('error', () => { controlStreamDirty = true; controlStreamRetryAt = Date.now()+1000; });
function renderControls(status, online, diagnostic) {
  controlsOnline = online;
  controlsOfflinePreview = status.offline_preview === true;
  controlsArmed = status.tracking.armed || status.tracking.mode === 'arming';
  cameraReady = status.components.camera.state === 'ready';
  tuningDirection = status.tuning.pan_direction;
  const targetRate = diagnostic.inference?.max_fps ?? 2;
  if (!ratePending && document.activeElement !== $('inferenceRate')) {
    $('inferenceRate').value = targetRate;
    display('inferenceRateValue',`${Number(targetRate).toFixed(0)} Hz`);
  }
  const recording = status.manual_recording || {};
  recordState = recording.state || 'unavailable';
  manualRequested = recording.manual_requested === true;
  $('recordButton').classList.toggle('recording-active',manualRequested);
  const label = manualRequested ? '結束手動錄影' : recordState === 'finalizing' ? '正在完成影片' : '開始手動錄影';
  $('recordButton').setAttribute('aria-label',label);
  $('recordButton').setAttribute('aria-pressed',String(manualRequested));
  $('recordButton').title = label;
  display('recordLabel',manualRequested ? '手動錄影中' : recording.tracking_requested ? '自動錄影中' : recordState === 'finalizing' ? '正在完成影片' : '尚未錄影');
  display('recordStatus',(recording.detail || (controlsOfflinePreview ? '離線預覽' : '')) + (['starting','recording'].includes(recordState) ? ` · ${recording.elapsed_seconds || 0} 秒` : ''));
  const angle = status.metrics.servo_angle_deg;
  if ($('servoAngleSlider')) $('servoAngleSlider').value = Number.isFinite(angle) ? angle : 90;
  display('servoTargetAngle',Number.isFinite(angle) ? `${angle.toFixed(1)}°` : '—');
  if (!tuningLoaded) {
    $('panGain').value = status.tuning.pan_gain_deg;
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
  $('recordButton').disabled = !controlsOnline || recordPending || recordState === 'unavailable' || (recordState === 'finalizing' && !manualRequested) || (!manualRequested && !cameraReady);
  for (const id of ['panGain','deadband','zoomMax','inferenceRate']) $(id).disabled = !controlsOnline && !controlsOfflinePreview;
  if (controlsArmed) display('cameraMessage','請先停止追蹤，再調整鏡頭');
  else if (!controlsOnline) display('cameraMessage',controlsOfflinePreview ? '離線預覽無法調整鏡頭' : '等待服務連線');
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
    display('cameraMessage','每次小幅調整後自動停止');
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
    display('cameraMessage',result.ok ? '已完成調整，鏡頭已停止' : result.detail || '調整未完成');
  } catch (_) { display('cameraMessage','回覆中斷，請確認鏡頭已停止'); }
  finally { lensPending = false; updateControlButtons(); }
});
$('recordButton').addEventListener('click',async () => {
  if (!controlsOnline || recordPending) return;
  recordPending = true;
  updateControlButtons();
  try {
    const response = await fetch('/api/recording',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:manualRequested ? 'stop' : 'start'}),signal:AbortSignal.timeout(5000)});
    const result = await response.json();
    display('recordStatus',result.detail || (result.ok ? '指令已接受' : '指令未完成'));
  } catch (_) { display('recordStatus','回覆中斷，請確認錄影狀態'); }
  finally { recordPending = false; await refresh(); updateControlButtons(); }
});
function showTuning() {
  display('panGainValue',Number($('panGain').value).toFixed(0));
  display('deadbandValue',Number($('deadband').value).toFixed(2));
  display('zoomMaxValue',`${Number($('zoomMax').value).toFixed(1)}×`);
}
for (const id of ['panGain','deadband','zoomMax']) {
  $(id).addEventListener('input',showTuning);
  $(id).addEventListener('change',applyTuning);
}
async function applyTuning() {
  if (!controlsOnline || controlsOfflinePreview || !tuningLoaded) return;
  tuningQueued = {pan_gain_deg:Number($('panGain').value),pan_deadband:Number($('deadband').value),pan_direction:tuningDirection,zoom_maximum:Number($('zoomMax').value)};
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
      display('tuningMessage','參數已生效');
    }
  } catch (error) { display('tuningMessage',error.message || '無法確認參數，請重試'); }
  finally { tuningPending = false; tuningQueued = null; }
}

let ratePending = false, queuedRate = null;
$('inferenceRate').addEventListener('input', () => display('inferenceRateValue',`${Number($('inferenceRate').value).toFixed(0)} Hz`));
$('inferenceRate').addEventListener('change', async () => {
  if (!controlsOnline || controlsOfflinePreview) return;
  queuedRate = Number($('inferenceRate').value);
  if (ratePending) return;
  ratePending = true;
  try {
    while (queuedRate !== null && controlsOnline) {
      const max_fps = queuedRate;
      queuedRate = null;
      display('inferenceRateMessage','套用中…');
      const response = await fetch('/api/inference/rate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_fps}),signal:AbortSignal.timeout(5000)});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || '辨識頻率未套用');
      const check = await fetch('/api/diagnostics',{cache:'no-store',signal:AbortSignal.timeout(5000)});
      if (!check.ok || (await check.json()).inference?.max_fps !== max_fps) throw new Error('辨識頻率讀回不同，請重試');
      display('inferenceRateMessage',`${max_fps} Hz 已生效`);
    }
  } catch(error) { display('inferenceRateMessage',error.message || '無法確認辨識頻率'); }
  finally { ratePending = false; queuedRate = null; }
});

let stopPending = false;
$('controlStop')?.addEventListener('click', async () => {
  if (stopPending || controlsOfflinePreview) return;
  stopPending = true;
  updateControlButtons();
  try {
    const response = await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(5000)});
    const result = await response.json();
    display('controlStopMessage',result.ok ? '已停止傳送追蹤目標' : result.detail || '停止指令未確認');
  } catch (_) { display('controlStopMessage','停止回覆未確認，請檢查服務連線'); }
  finally { stopPending = false; await refresh(); updateControlButtons(); }
});
