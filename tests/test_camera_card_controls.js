const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {hidden: false, disabled: false,
    addEventListener(name, fn) { this[name] = fn; }});
  return nodes.get(id);
}
const card = {destination: 'camera_sd', state: 'unknown', available: false, schedule_enabled: null};
const status = {manual_recording: card, tracking: {armed: false}, inference: {enabled: false}};
const actions = [];
let reject = false, mismatch = false, release;
const context = vm.createContext({window: {}, document: {getElementById: node}, AbortSignal,
  fetch: async (path, options) => {
    if (options.method !== 'POST') {
      assert.equal(path, '/api/status');
      return {ok: true, json: async () => status};
    }
    assert.equal(path, '/api/recording');
    const {action} = JSON.parse(options.body);
    actions.push(action);
    await new Promise(resolve => { release = resolve; });
    if (!reject && !mismatch) {
      card.schedule_enabled = action === 'refresh' ? Boolean(card.schedule_enabled) : action === 'start';
      card.available = true;
      card.state = card.schedule_enabled ? 'schedule_enabled' : 'idle';
    }
    return {ok: !reject, json: async () => ({ok: !reject, detail: '無法連線相機'})};
  }});
const script = fs.readFileSync('shore_tracker/web/static/camera-card.js', 'utf8');
vm.runInContext(script, context);
const ui = context.window.shoreCameraCard;
const button = node('cameraCardAction'), refresh = node('cameraCardRefresh'), message = node('cameraCardStatus');
async function click(element) {
  const done = element.click();
  assert.equal(button.disabled, true);
  await button.click();
  release();
  await done;
}
(async () => {
  for (const page of ['index', 'advanced', 'mobile']) {
    const html = fs.readFileSync('shore_tracker/web/static/' + page + '.html', 'utf8');
    assert.ok(html.includes('id="cameraCardControls" hidden'));
    assert.ok(html.includes('<script src="/camera-card.js"></script>'));
  }
  ui.render({manual_recording: {state: 'idle', continuous_enabled: false}});
  assert.equal(node('cameraCardControls').hidden, true);
  assert.equal(node('nanoRecordingMode').hidden, false);
  ui.render(status);
  assert.equal(actions.length, 0);
  assert.equal(node('nanoRecordingMode').hidden, true);
  assert.equal(node('recordStatus').hidden, true);
  assert.equal(button.textContent, '停止相機卡錄影', 'unknown state permits stop');
  await click(refresh);
  assert.equal(button.textContent, '開始相機卡錄影');
  await click(button);
  assert.match(message.textContent, /排程已開啟，寫入狀態待確認/);
  assert.equal(button.textContent, '停止相機卡錄影');
  card.last_card_clip = {started_at_camera: '2026-11-19T10:05:15', size_bytes: 1024};
  card.clip_query_detail = '相機卡當日最近的定時錄影片段';
  ui.render(status);
  assert.match(node('cameraCardClip').textContent, /2026-11-19 10:05:15/);
  reject = true;
  await click(button);
  assert.match(message.textContent, /無法連線相機/);
  reject = false;
  mismatch = true;
  await click(button);
  assert.match(message.textContent, /讀回不一致/);
  mismatch = false;
  await click(button);
  assert.match(message.textContent, /排程已關閉/);
  ui.disconnect();
  await button.click();
  assert.match(message.textContent, /未知/);
  assert.equal(node('cameraCardClip').hidden, true);
  ui.render({...status, offline_preview: true});
  assert.equal(refresh.disabled, true);
  await refresh.click();
  assert.deepEqual(actions, ['refresh', 'start', 'stop', 'stop', 'stop']);
  console.log('Camera card controls: manual commands, readback, failed writes, clip metadata, offline and legacy modes passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
