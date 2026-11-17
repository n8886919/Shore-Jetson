(() => {
'use strict';
const $ = id => document.getElementById(id);
const directions = [...document.querySelectorAll('[data-pan-tilt-direction]')];
const panel = $('panTiltPanel');
const drawer = $('controlsPanel');
let token = null, online = false;
let status = {}, session = null, sequence = 0, held = null, timer = null, busy = false;

function render() {
  panel.hidden = online && !status.available && !status.offline_preview;
  const blocked = !online || status.offline_preview || !status.available || status.tracking_active;
  $('panTiltConnect').textContent = status.connected ? '斷開雲台' : '連接雲台';
  $('panTiltConnect').disabled = busy || blocked;
  $('panTiltArm').textContent = session && status.armed ? '停用手動' : '啟用手動';
  $('panTiltArm').disabled = busy || blocked || !status.connected || !!status.error || (status.armed && !session);
  $('panTiltSpeed').disabled = blocked || !!held;
  $('panTiltStop').disabled = status.offline_preview || !token;
  directions.forEach(button => {
    button.disabled = busy || blocked || !session || !status.armed || !!status.error;
    button.classList.toggle('active', button.dataset.panTiltDirection === held);
  });
  $('panTiltState').textContent = status.error || (status.offline_preview ? '離線預覽' : !online ? '雲台狀態失聯' : !status.available ? '未設定 Pelco-D 雲台' : status.tracking_active ? '自動跟拍中，請先停止追蹤' : status.armed && !session ? '其他頁面正在手動控制' : status.notice || (status.connected ? '雲台已連接' : '連接後可手動調整方向'));
  $('panTiltState').classList.toggle('fault', !!status.error || !online);
}

async function request(body, keepalive = false) {
  const response = await fetch('/api/pan-tilt', {method: 'POST', keepalive,
    headers: {'Content-Type': 'application/json', 'X-Pan-Tilt-Token': token},
    body: JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok || result.ok === false) throw new Error(result.detail || '控制失敗');
  return result;
}

function fail(error) {
  online = false;
  clearTimeout(timer); held = null; session = null;
  status = {...status, armed: false, error: error.message}; render();
}

function stop(disarm = false) {
  held = null; clearTimeout(timer);
  if (!token || status.offline_preview) return Promise.resolve();
  const command = {action: disarm ? 'disarm' : 'stop', session, sequence: ++sequence};
  if (disarm) session = null;
  render();
  return request(command, true).then(result => {
    if (command.sequence === sequence) {status = result.status; render();}
  }).catch(fail);
}

async function pulse(direction) {
  const command = {action: 'move', session, sequence: ++sequence, direction, speed: Number($('panTiltSpeed').value)};
  try {
    const result = await request(command);
    if (held !== direction || command.sequence !== sequence) return;
    status = result.status; render();
    timer = setTimeout(() => {if (held === direction) pulse(direction);}, 120);
  } catch (error) {
    // Revoke locally and try STOP even if a motion response was lost.
    stop(true); fail(error);
  }
}

function start(direction) {
  if (!online || status.offline_preview || status.tracking_active || busy || held || !session || !status.armed || status.error || drawer?.getAttribute('aria-hidden') === 'true') return;
  held = direction; render(); pulse(direction);
}

async function toggleConnection() {
  if (!online || status.offline_preview || status.tracking_active || busy) return;
  busy = true; render();
  try {
    if (held) await stop(true);
    const revision = ++sequence;
    const result = await request({action: status.connected ? 'disconnect' : 'connect'});
    if (revision === sequence) {status = result.status; session = null;}
  } catch (error) {fail(error);}
  finally {busy = false; render();}
}

async function toggleArm() {
  if (!online || status.offline_preview || status.tracking_active || busy) return;
  busy = true; render();
  try {
    if (session) await stop(true);
    else {
      const revision = ++sequence;
      const result = await request({action: 'arm', epoch: status.epoch});
      if (revision === sequence) {session = result.session; status = result.status;}
    }
  } catch (error) {fail(error);}
  finally {busy = false; render();}
}

$('panTiltConnect').addEventListener('click', toggleConnection);
$('panTiltArm').addEventListener('click', toggleArm);
$('panTiltStop').addEventListener('click', () => stop(true));
$('panTiltSpeed').addEventListener('input', () => {$('panTiltSpeedValue').textContent = `${$('panTiltSpeed').value} / 63`;});
directions.forEach(button => {
  button.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault(); button.setPointerCapture(event.pointerId); start(button.dataset.panTiltDirection);
  });
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    button.addEventListener(name, () => {if (held === button.dataset.panTiltDirection) stop();});
  }
});
const keys = {ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right'};
window.addEventListener('keydown', event => {
  if (drawer?.getAttribute('aria-hidden') === 'true' || document.hidden) return;
  if (['Escape', ' '].includes(event.key)) {event.preventDefault(); if (!event.repeat) stop(true); return;}
  if (!keys[event.key] || !panel.contains(event.target) || ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) return;
  event.preventDefault(); if (!event.repeat) start(keys[event.key]);
});
window.addEventListener('keyup', event => {if (keys[event.key] === held) stop();});
window.addEventListener('pan-tilt-panel-close', () => {if (held || session || busy) stop(true);});
window.addEventListener('blur', () => {if (held || session || busy) stop(true);});
window.addEventListener('pagehide', () => {if (held || session || busy) stop(true);});
document.addEventListener('visibilitychange', () => {if (document.hidden && (held || session || busy)) stop(true);});

async function refresh() {
  const revision = sequence;
  try {
    const response = await fetch('/api/pan-tilt');
    if (!response.ok) throw new Error('無法讀取狀態');
    const current = await response.json();
    token = current.control_token;
    online = true;
    // A poll started before a command must not overwrite its newer response.
    if (!busy && !held && revision === sequence) {status = current; if (!status.armed) session = null; render();}
  } catch (error) {if (held || session || busy) stop(true); fail(error);}
}
refresh();
setInterval(refresh, 700);
render();

})();
