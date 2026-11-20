(() => {
  const element = id => document.getElementById(id);
  const controls = element('cameraCardControls');
  if (!controls) return;
  const actionButton = element('cameraCardAction');
  const refreshButton = element('cameraCardRefresh');
  const message = element('cameraCardStatus');
  const clipInfo = element('cameraCardClip');
  let recording = null, online = false, pending = '', error = '';

  function canStart() {
    return recording?.state === 'idle' && recording.schedule_enabled === false;
  }

  function update() {
    if (!recording) return;
    const changing = ['starting', 'stopping', 'checking'].includes(recording.state);
    actionButton.textContent = canStart() ? '開始相機卡錄影' : '停止相機卡錄影';
    actionButton.disabled = !online || Boolean(pending) || changing || (canStart() && !recording.available);
    refreshButton.disabled = !online || Boolean(pending) || changing;
    const clip = recording.last_card_clip;
    clipInfo.hidden = !online || !recording.clip_query_detail;
    if (!clipInfo.hidden) {
      clipInfo.textContent = clip
        ? `上次查到的卡內片段（相機時間）${clip.started_at_camera.replace('T', ' ')} · ${clip.size_bytes.toLocaleString()} 位元組`
        : recording.clip_query_detail;
    }
    if (!online) message.textContent = '連線中斷，相機卡錄影狀態未知';
    else if (pending || changing) message.textContent = '正在向相機確認錄影設定…';
    else {
      let status = '相機卡錄影排程未知';
      if (recording.schedule_enabled === true) status = '上次讀回相機卡錄影排程已開啟，寫入狀態待確認';
      if (recording.schedule_enabled === false) status = '上次讀回相機卡錄影排程已關閉';
      const detail = error || (!recording.available && recording.detail);
      message.textContent = detail ? status + '。' + detail : status;
    }
  }

  function render(status) {
    const card = status.manual_recording?.destination === 'camera_sd';
    controls.hidden = !card;
    element('nanoRecordingMode').hidden = card;
    if (element('recordStatus')) element('recordStatus').hidden = card;
    recording = card ? status.manual_recording : null;
    online = card && !status.offline_preview;
    if (!card) error = '';
    update();
  }

  async function command(action) {
    if (!online || pending || (action === 'refresh' ? refreshButton : actionButton).disabled) return;
    pending = action;
    error = '';
    update();
    try {
      const response = await fetch('/api/recording', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action}), signal: AbortSignal.timeout(20000)});
      const result = await response.json();
      if (!response.ok || !result.ok) error = result.detail || '相機未確認這次操作';
      const check = await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (!check.ok) throw new Error();
      render(await check.json());
      if (!error && action !== 'refresh' && recording?.schedule_enabled !== (action === 'start')) {
        error = '錄影設定讀回不一致，請重新查詢卡片';
      }
    } catch (_) {
      online = false;
      error = '無法確認這次操作，請重新查詢卡片';
    } finally {
      pending = '';
      update();
    }
  }

  actionButton.addEventListener('click', () => command(canStart() ? 'start' : 'stop'));
  refreshButton.addEventListener('click', () => command('refresh'));
  window.shoreCameraCard = {render, disconnect() { online = false; update(); }};
})();
