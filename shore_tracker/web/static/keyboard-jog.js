(() => {
  const keys = {ArrowUp:'up', ArrowDown:'down', ArrowLeft:'left', ArrowRight:'right'};
  let enabled = false, epoch = '', token = '', held = '', session = '', sequence = 0;
  let revision = 0, busy = false, timer = null, lastStatus = 0, tokenPending = false;
  const speedInput = document.getElementById?.('manualSpeed');
  const speedMessage = document.getElementById?.('manualSpeedMessage');
  let speedOnline = false, speedBusy = false, speedQueued = null, speedStatus = {};
  function renderSpeed() {
    if (!speedInput) return;
    speedInput.disabled = !speedOnline || !token || speedStatus.available !== true
      || !Number.isFinite(speedStatus.speed_deg_s);
    speedInput.max = String(speedStatus.max_speed_deg_s ?? 5);
    if (!speedBusy && speedQueued === null && document.activeElement !== speedInput
        && Number.isFinite(speedStatus.speed_deg_s)) speedInput.value = speedStatus.speed_deg_s;
  }
  const editable = target => {
    if (target?.tagName === 'INPUT') return !['checkbox','button','submit'].includes(target.type);
    return target?.closest?.('textarea,select,[contenteditable="true"],[role="slider"]')
      || target?.matches?.('textarea,select,[contenteditable="true"],[role="slider"]');
  };
  async function loadToken() {
    if (tokenPending || token) return;
    tokenPending = true;
    try {
      const response = await fetch('/api/keyboard', {cache:'no-store',signal:AbortSignal.timeout(1500)});
      if (response.ok) token = (await response.json()).control_token || '';
    } catch {} finally { tokenPending = false; renderSpeed(); }
  }
  async function post(body) {
    const response = await fetch('/api/keyboard', {method:'POST',
      headers:{'Content-Type':'application/json','X-Pan-Tilt-Token':token},
      body:JSON.stringify(body),keepalive:body.action === 'stop',signal:AbortSignal.timeout(1500)});
    if (response.status === 403) token = '';
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.detail || '方向鍵控制已停止');
    return result;
  }
  async function applySpeed() {
    if (speedBusy) return;
    speedBusy = true;
    try {
      while (speedQueued !== null && speedOnline && token) {
        const value = speedQueued;
        speedQueued = null;
        if (speedMessage) speedMessage.textContent = '套用中…';
        const result = await post({action:'speed',speed_deg_s:value});
        if (result.status?.speed_deg_s !== value) throw new Error('速度讀回不一致，請重試');
        speedStatus = {...speedStatus,...result.status};
        if (speedMessage) speedMessage.textContent = '已套用';
      }
    } catch (error) {
      speedQueued = null;
      if (speedMessage) speedMessage.textContent = error.message || '速度未套用，請重試';
    } finally { speedBusy = false; renderSpeed(); }
  }
  speedInput?.addEventListener('input', () => {
    if (speedInput.disabled || !speedOnline || !token || Date.now()-lastStatus > 1500) return;
    const value = Number(speedInput.value);
    if (!speedInput.value.trim() || !Number.isFinite(value) || value < .1 || value > Number(speedInput.max)) {
      speedQueued = null;
      if (speedMessage) speedMessage.textContent = `請輸入 0.1–${speedInput.max}°/s`;
      return;
    }
    speedQueued = value;
    applySpeed();
  });
  function stop() {
    if (!held) return;
    const oldEpoch = epoch;
    held = session = ''; revision++; busy = false;
    clearInterval(timer); timer = null;
    // A released key requires a fresh epoch before another start.
    enabled = false;
    post({action:'stop',epoch:oldEpoch}).catch(() => {});
  }
  async function renew() {
    if (!held) return;
    if (!enabled || document.hidden || Date.now()-lastStatus > 1500) { stop(); return; }
    if (busy || !session) return;
    const current = revision;
    busy = true;
    try { await post({action:'move',session,sequence:++sequence,direction:keys[held]}); }
    catch { if (current === revision) stop(); }
    finally { if (current === revision) busy = false; }
  }
  window.shoreKeyboardControl = {
    render(status, online = true) {
      if (!token) loadToken();
      const next = status.keyboard || {};
      const allowed = online && !status.offline_preview && next.enabled === true;
      if (held && (!allowed || next.epoch !== epoch)) stop();
      enabled = allowed;
      epoch = next.epoch || '';
      lastStatus = Date.now();
      speedStatus = next;
      speedOnline = online && !status.offline_preview;
      renderSpeed();
    },
    disconnect() { enabled = false; speedOnline = false; speedQueued = null; renderSpeed(); stop(); }
  };
  document.addEventListener('keydown', async event => {
    if (event.key === 'Escape') { stop(); return; }
    if (!keys[event.key] || editable(event.target) || event.altKey || event.ctrlKey || event.metaKey) return;
    if (held) { event.preventDefault(); return; }
    if (!enabled || !token || event.repeat || document.hidden || Date.now()-lastStatus > 1500) return;
    event.preventDefault();
    held = event.key; session = ''; sequence = 0;
    const current = ++revision;
    timer = setInterval(renew, 100);
    try {
      const result = await post({action:'start',epoch,direction:keys[held]});
      if (current === revision && held) session = result.session;
    } catch { if (current === revision) stop(); }
  });
  document.addEventListener('keyup', event => { if (event.key === held) { event.preventDefault(); stop(); } });
  document.addEventListener('focusin', event => { if (editable(event.target)) stop(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('blur', stop);
  window.addEventListener('pagehide', stop);
  loadToken();
})();
