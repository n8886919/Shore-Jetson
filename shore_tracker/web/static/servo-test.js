(() => {
'use strict';
const $ = id => document.getElementById(id);
const panel = $('servoTestPanel'), button = $('servoTestButton'), drawer = $('controlsPanel');
let token = null, status = {}, online = false, session = null;
let sequence = 0, revision = 0, busy = false, stopping = false, polling = false, nextPoll = 0, error = '';

function render() {
  panel.hidden = online && !status.available && !status.offline_preview;
  const active = busy || status.running;
  const step = stopping ? '停止中' : busy ? '啟動中' : {
    center:'置中', left:'左 20°', right:'右 40°', up:'抬頭 40°', down:'低頭 40°'
  }[status.phase] || '測試中';
  button.textContent = active ? `${step}\n停止測試` : '雲台測試';
  button.classList.toggle('testing', !!active);
  button.disabled = !token || status.offline_preview || (!busy && !status.running
    && (!online || !status.available || !status.calibrated || status.tracking_active || status.connected));
  const detail = error || status.error || (status.phase === 'stopped' && /逾時/.test(status.notice) ? status.notice : '');
  button.title = status.offline_preview ? '離線預覽不執行舵機動作'
    : !online ? '等待舵機測試狀態'
    : status.tracking_active ? '請先停止自動跟拍'
    : !status.calibrated && status.available ? '請先設定水平 M0、垂直 M1 並確認兩軸方向'
    : '';
  $('servoTestState').textContent = detail;
  $('servoTestState').hidden = !$('servoTestState').textContent;
}

async function request(body, keepalive = false) {
  const response = await fetch('/api/servo-test', {method:'POST', keepalive,
    headers:{'Content-Type':'application/json','X-Pan-Tilt-Token':token},
    body:JSON.stringify(body), signal:AbortSignal.timeout(4000)});
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.detail || '測試請求失敗');
  return result;
}

async function stop() {
  ++revision; session = null;
  if (!token || status.offline_preview) return;
  const current = revision;
  busy = true; stopping = true; render();
  try {
    const result = await request({action:'stop'}, true);
    if (current === revision) {status = result.status; online = true;}
  } catch (_) {
    if (current === revision) {online = false; error = '停止回覆中斷；系統會因測試連線逾時停送';}
  } finally {if (current === revision) {busy = false; stopping = false; render();}}
}

async function toggle() {
  if (busy || status.running) return stop();
  if (button.disabled || drawer?.getAttribute('aria-hidden') === 'true') return;
  const current = ++revision;
  busy = true; stopping = false; error = ''; render();
  try {
    const result = await request({action:'start', epoch:status.epoch});
    if (current !== revision) return; // A dismiss/STOP has already fenced this start.
    status = result.status; session = result.session; sequence = 0; online = true;
  } catch (failure) {
    if (current === revision) {
      await stop();
      if (revision === current + 1) {error = failure.message; render();}
    }
  } finally {if (current === revision) {busy = false; render();}}
}

function cancel() {if (session || busy || status.running) stop();}

async function refresh() {
  if (polling || busy || (!session && Date.now() < nextPoll)) return;
  polling = true; nextPoll = Date.now() + 1000;
  const current = revision;
  try {
    let result;
    if (session) {
      result = (await request({action:'heartbeat',session,sequence:++sequence})).status;
    } else {
      const response = await fetch('/api/servo-test', {cache:'no-store',signal:AbortSignal.timeout(3000)});
      if (!response.ok) throw new Error('無法讀取測試狀態');
      result = await response.json();
    }
    if (current !== revision || busy) return;
    token = result.control_token || token; status = result; online = true;
    if (!status.running) session = null;
    render();
  } catch (error) {
    if (current !== revision) return;
    cancel(); online = false; status = {...status, error:error.message}; render();
  } finally {polling = false;}
}

button.addEventListener('click', toggle);
for (const event of ['blur','pagehide']) window.addEventListener(event, cancel);
document.addEventListener('visibilitychange', () => {if (document.hidden) cancel();});
document.addEventListener('keydown', event => {if (event.key === 'Escape') cancel();});
if (drawer) new MutationObserver(() => {
  if (drawer.getAttribute('aria-hidden') === 'true') cancel();
}).observe(drawer, {attributes:true,attributeFilter:['aria-hidden']});
refresh(); setInterval(refresh, 250); render();
})();
