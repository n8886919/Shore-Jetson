'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const tick = async () => {await new Promise(setImmediate); await new Promise(setImmediate);};

function harness() {
  const elements = new Map(), events = {}, posts = [], timers = new Map();
  let polling = null;
  let nextTimer = 0, pendingMove = null, deferMove = false;
  let pendingArm = null, deferArm = false, pendingPoll = null, deferPoll = false;
  let current = {available: true, control_token: 'test-token', tracking_active: false, simulate: true, connected: false, armed: false, epoch: 0, address: 1};
  function element(id) {
    if (!elements.has(id)) elements.set(id, {textContent: '', disabled: false, value: '8',
      dataset: {}, getAttribute(name) {return this[name];}, contains(target) {return target.inPanel === true;}, classList: {toggle() {}}, setPointerCapture() {},
      addEventListener(name, fn) {this[name] = fn;}});
    return elements.get(id);
  }
  const buttons = ['left', 'right', 'up', 'down'].map(direction => {
    const button = element(direction); button.dataset.panTiltDirection = direction; return button;
  });
  const context = vm.createContext({document: {
    getElementById: element, querySelectorAll: () => buttons,
    querySelector: () => ({content: 'test-token'}),
    body: {classList: {toggle() {}}}, hidden: false,
    addEventListener(name, fn) {events[name] = fn;},
  }, window: {addEventListener(name, fn) {events[name] = fn;}},
  setInterval(fn) {polling = fn;}, clearTimeout(id) {timers.delete(id);},
  setTimeout(fn) {timers.set(++nextTimer, fn); return nextTimer;},
  fetch: async (path, options) => {
    if (!options) {
      const snapshot = {...current};
      if (deferPoll) {
        deferPoll = false;
        return new Promise(resolve => {pendingPoll = () => resolve({ok: true, json: async () => snapshot});});
      }
      return {ok: true, json: async () => snapshot};
    }
    const body = JSON.parse(options.body); posts.push(body);
    assert.equal(options.headers['X-Pan-Tilt-Token'], 'test-token');
    if (body.action === 'connect') current.connected = true;
    if (body.action === 'disconnect') {current.connected = false; current.armed = false;}
    if (body.action === 'arm') {assert.equal(body.epoch, current.epoch); current.armed = true;}
    if (body.action === 'disarm') {current.armed = false; current.epoch++;}
    const result = {ok: true, status: {...current}, session: 'owner'};
    if (body.action === 'arm' && deferArm) {
      return new Promise(resolve => {pendingArm = () => resolve({ok: true, json: async () => result});});
    }
    if (body.action === 'move' && deferMove) {
      return new Promise(resolve => {pendingMove = () => resolve({ok: true, json: async () => result});});
    }
    return {ok: true, json: async () => result};
  }});
  vm.runInContext(fs.readFileSync('shore_tracker/web/static/pan-tilt.js', 'utf8'), context);
  return {element, events, posts, timers, context,
    poll() {return polling();},
    setStatus(values) {Object.assign(current, values);},
    defer() {deferMove = true;}, resolveMove() {pendingMove();},
    deferArm() {deferArm = true;}, resolveArm() {pendingArm();},
    deferPoll() {deferPoll = true;}, resolvePoll() {pendingPoll();},
    down(direction) {element(direction).pointerdown({button: 0, pointerId: 1, preventDefault() {}});},
    async arm() {await element('panTiltConnect').click(); await element('panTiltArm').click();},
  };
}

(async () => {
  const h = harness(); await tick();
  assert.equal(h.posts.length, 0, 'loading only polls; never opens or actuates');
  assert.equal(h.element('left').disabled, true);
  await h.element('panTiltConnect').click();
  assert.equal(h.element('left').disabled, true, 'connecting alone does not arm');
  await h.element('panTiltArm').click();
  assert.equal(h.element('left').disabled, false);
  h.down('left'); await tick();
  assert.equal(h.posts.at(-1).action, 'move');
  assert.equal(h.posts.at(-1).speed, 8);
  h.element('left').pointerup(); await tick();
  assert.equal(h.posts.at(-1).action, 'stop');
  assert.equal(h.timers.size, 0);
  assert.ok(h.posts.at(-1).sequence > h.posts.at(-2).sequence);
  h.down('up'); await tick(); h.events.blur(); await tick();
  assert.equal(h.posts.at(-1).action, 'disarm');
  assert.equal(h.element('up').disabled, true);

  const race = harness(); await tick(); await race.arm(); race.defer();
  race.down('right'); await tick(); race.element('right').pointercancel(); await tick();
  race.resolveMove(); await tick();
  assert.equal(race.posts.at(-1).action, 'stop');
  assert.equal(race.timers.size, 0, 'a late move response cannot restart released motion');
  assert.equal(race.element('panTiltSpeed').disabled, false);

  const armRace = harness(); await tick(); await armRace.element('panTiltConnect').click();
  armRace.deferArm(); const arming = armRace.element('panTiltArm').click(); await tick();
  await armRace.element('panTiltStop').click(); armRace.resolveArm(); await arming;
  assert.equal(armRace.element('left').disabled, true, 'late arm response cannot undo emergency stop');

  const blurRace = harness(); await tick(); await blurRace.element('panTiltConnect').click();
  blurRace.deferArm(); const blurArming = blurRace.element('panTiltArm').click(); await tick();
  blurRace.events.blur(); await tick(); blurRace.resolveArm(); await blurArming;
  assert.equal(blurRace.element('left').disabled,true,'blur while ARM is pending revokes late ownership');

  const pollRace = harness(); await tick(); pollRace.deferPoll();
  const polling = pollRace.poll(); await tick();
  await pollRace.element('panTiltConnect').click(); pollRace.resolvePoll(); await polling;
  assert.equal(pollRace.element('panTiltArm').disabled, false, 'old poll cannot overwrite a newer connection result');

  const keyboard = harness(); await tick(); await keyboard.arm();
  const key = {key: 'ArrowDown', repeat: false, target: {tagName: 'BUTTON', inPanel: true}, preventDefault() {}};
  keyboard.events.keydown(key); await tick();
  assert.equal(keyboard.posts.at(-1).direction, 'down');
  keyboard.events.keyup(key); await tick();
  assert.equal(keyboard.posts.at(-1).action, 'stop');
  keyboard.events.keydown({...key, key: 'Escape'}); await tick();
  assert.equal(keyboard.posts.at(-1).action, 'disarm');
  keyboard.events.keydown(key); await tick();
  assert.equal(keyboard.posts.at(-1).action, 'disarm', 'disarmed keyboard cannot move');
  const hidden = harness(); await tick(); await hidden.arm(); hidden.down('left'); await tick();
  hidden.events['pan-tilt-panel-close'](); await tick();
  assert.equal(hidden.posts.at(-1).action, 'disarm');
  assert.equal(hidden.element('left').disabled, true, 'closing settings revokes manual motion');
  const automatic = harness(); await tick(); automatic.setStatus({tracking_active:true}); await automatic.poll();
  await automatic.element('panTiltConnect').click();
  assert.equal(automatic.posts.length,0,'automatic tracking blocks manual connection');
  const preview = harness(); await tick(); preview.setStatus({offline_preview:true}); await preview.poll();
  await preview.element('panTiltConnect').click(); await preview.element('panTiltStop').click();
  assert.equal(preview.posts.length,0,'offline preview never opens or stops hardware');
  const scoped = harness(); await tick(); await scoped.arm();
  scoped.events.keydown({...key, target:{tagName:'BODY'}}); await tick();
  assert.ok(!scoped.posts.some(p=>p.action==='move'),'arrow keys outside the manual panel cannot move');
  console.log('Pelco controls: read-only load, arm gate, release/cancel stop, late response and keyboard safety passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
