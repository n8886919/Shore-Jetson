// Optional browser regression check: run with Playwright and Chromium available.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

(async () => {
  const server = spawn('python3', ['deploy/preview-web.py', '--port', '0'], {stdio: ['ignore', 'pipe', 'inherit']});
  let browser;
  try {
    const url = await new Promise((resolve, reject) => {
      server.stdout.once('data', data => resolve(data.toString().trim()));
      server.once('error', reject);
      server.once('exit', code => reject(new Error(`Preview exited: ${code}`)));
    });
    browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH || undefined});
    const page = await browser.newPage({viewport: {width: 375, height: 812}, isMobile: true});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${url}/kiosk?layout=nano`);
    await page.waitForFunction(() => document.querySelector('.kiosk.responsive-layout'));
    // Keep the resize transition in progress long enough to inspect its paint order.
    await page.locator('.viewer').evaluate(viewer => { viewer.style.transitionDuration = '10s'; });
    // Exercise the real video element with locally generated frames, without hardware.
    await page.evaluate(async () => {
      const source = document.createElement('canvas');
      source.width = 960; source.height = 540;
      const context = source.getContext('2d');
      context.fillStyle = '#164c63'; context.fillRect(0, 0, 960, 540);
      const video = document.getElementById('rtcStream');
      video.srcObject = source.captureStream(10);
      video.hidden = false;
      await video.play();
    });
    await page.setViewportSize({width: 667, height: 230});
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    async function checkVideo(label) {
      const fits = await page.evaluate(() => {
        const video = document.getElementById('rtcStream').getBoundingClientRect();
        const stage = document.querySelector('.stage').getBoundingClientRect();
        return video.top >= stage.top - 1 && video.bottom <= stage.bottom + 1 &&
          Math.abs(video.width / video.height - 16 / 9) < .01;
      });
      assert.ok(fits, `${label}: complete 16:9 image fits above footer`);
    }
    async function checkButtons(label) {
      const result = await page.evaluate(() => {
        const footer = document.querySelector('.status-bar').getBoundingClientRect();
        return ['armButton', 'controlToggle'].map(id => {
          const button = document.getElementById(id), box = button.getBoundingClientRect();
          const visible = box.top >= footer.top && box.bottom <= innerHeight && box.left >= 0 && box.right <= innerWidth;
          const uncovered = [box.left + 2, (box.left + box.right) / 2, box.right - 2].every(x =>
            button.contains(document.elementFromPoint(x, (box.top + box.bottom) / 2)));
          return {id, visible, uncovered};
        });
      });
      for (const button of result) {
        assert.ok(button.visible, `${label}: ${button.id} stays in the visible footer`);
        assert.ok(button.uncovered, `${label}: video/settings must not cover ${button.id}`);
      }
    }
    await checkButtons('during portrait-to-landscape resize');
    await checkVideo('during portrait-to-landscape resize');
    await page.locator('.viewer').evaluate(viewer => { viewer.style.transition = 'none'; });
    for (const [width, height] of [[375, 812], [667, 230], [844, 390], [1024, 600], [1920, 1080]]) {
      await page.setViewportSize({width, height});
      for (const open of [false, true, false]) {
        if ((await page.locator('#controlToggle').getAttribute('aria-expanded')) !== String(open)) {
          await page.locator('#controlToggle').click();
        }
        await page.evaluate(() => Promise.all(document.getAnimations().map(animation => animation.finished)));
        await checkButtons(`${width}x${height}, settings ${open}`);
        await checkVideo(`${width}x${height}, settings ${open}`);
      }
    }
    assert.deepEqual(errors, [], 'no browser JavaScript errors');
    console.log('Kiosk browser layout: portrait, landscape, resize, video and settings passed');
  } finally {
    await browser?.close();
    server.kill();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
