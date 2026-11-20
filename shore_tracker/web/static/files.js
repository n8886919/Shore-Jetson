(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const videoExtensions = new Set(['mkv', 'mp4', 'webm', 'mov', 'm4v']);
  let currentPath = new URLSearchParams(location.search).get('path') || '';
  let offset = 0;
  let listing = null;
  let request = null;
  let serial = 0;

  function bytes(value) {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let amount = Number(value) || 0;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
    return `${amount.toLocaleString('zh-TW', { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
  }
  function fileUrl(path, download = false) {
    const query = new URLSearchParams({ path });
    if (download) query.set('download', '1');
    return `/api/files/content?${query}`;
  }
  function videoFile(name) { return videoExtensions.has(name.split('.').pop().toLowerCase()); }
  function breadcrumbs() {
    const nav = $('breadcrumbs');
    nav.replaceChildren();
    const parts = currentPath ? currentPath.split('/') : [];
    ['記憶卡', ...parts].forEach((part, index) => {
      if (index) {
        const slash = document.createElement('span'); slash.className = 'separator'; slash.textContent = '/';
        nav.append(slash);
      }
      const button = document.createElement('button'); button.type = 'button'; button.className = 'crumb'; button.textContent = part;
      if (index === parts.length) button.setAttribute('aria-current', 'page');
      button.addEventListener('click', () => navigate(parts.slice(0, index).join('/')));
      nav.append(button);
    });
  }
  function navigate(path, push = true) {
    currentPath = path; offset = 0; $('search').value = '';
    if (push) history.pushState(null, '', path ? `/files?${new URLSearchParams({ path })}` : '/files');
    load();
  }
  function preview(entry) {
    $('previewTitle').textContent = entry.name;
    $('previewDownload').href = fileUrl(entry.path, true);
    $('playbackNote').textContent = '若瀏覽器無法播放 MKV／H.265，請下載後使用 VLC 等播放器開啟。';
    $('video').src = fileUrl(entry.path);
    $('preview').showModal();
  }
  function render() {
    if (!listing) return;
    const needle = $('search').value.toLocaleLowerCase();
    const entries = listing.entries.filter((entry) => entry.name.toLocaleLowerCase().includes(needle));
    const body = $('entries'); body.replaceChildren();
    for (const entry of entries) {
      const row = document.createElement('tr');
      const name = document.createElement('td');
      const open = document.createElement('button'); open.type = 'button'; open.className = 'file-name';
      const icon = document.createElement('span'); icon.className = `icon ${entry.type}`; icon.setAttribute('aria-hidden', 'true');
      icon.textContent = entry.type === 'directory' ? '▱' : videoFile(entry.name) ? '▶' : '檔';
      const label = document.createElement('span'); label.className = 'name-text'; label.textContent = entry.name;
      open.append(icon, label);
      open.addEventListener('click', () => {
        if (entry.type === 'directory') navigate(entry.path);
        else if (videoFile(entry.name)) preview(entry);
        else {
          const link = document.createElement('a'); link.href = fileUrl(entry.path, true); link.download = entry.name;
          document.body.append(link); link.click(); link.remove();
        }
      });
      name.append(open); row.append(name);
      const size = document.createElement('td'); size.className = 'size'; size.textContent = entry.size === null ? '資料夾' : bytes(entry.size); row.append(size);
      const modified = document.createElement('td'); modified.className = 'modified';
      modified.textContent = new Date(entry.modified * 1000).toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); row.append(modified);
      const actions = document.createElement('td');
      if (entry.type === 'file') {
        const download = document.createElement('a'); download.className = 'download'; download.href = fileUrl(entry.path, true); download.download = entry.name; download.textContent = '下載'; download.setAttribute('aria-label', `下載 ${entry.name}`); actions.append(download);
      }
      row.append(actions); body.append(row);
    }
    $('fileTable').hidden = entries.length === 0;
    $('message').hidden = entries.length > 0;
    $('message').textContent = needle ? '沒有符合的檔案' : '這個資料夾目前沒有檔案';
    $('message').className = 'message';
    $('itemCount').textContent = `${listing.total.toLocaleString('zh-TW')} 個項目${listing.total > listing.limit ? `，第 ${Math.floor(offset / listing.limit) + 1} 頁` : ''}${listing.truncated ? '（僅列出前 10,000 項）' : ''}`;
    $('previous').disabled = offset === 0;
    $('next').disabled = offset + listing.limit >= listing.total;
  }
  async function load() {
    const sequence = ++serial;
    if (request) request.abort();
    const controller = new AbortController();
    request = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    listing = null;
    breadcrumbs(); $('refresh').disabled = true;
    $('message').hidden = false; $('message').className = 'message'; $('message').textContent = '正在讀取檔案…';
    $('fileTable').hidden = true; $('previous').disabled = true; $('next').disabled = true;
    try {
      const response = await fetch(`/api/files?${new URLSearchParams({ path: currentPath, offset })}`, { cache: 'no-store', signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '無法讀取檔案');
      if (sequence !== serial) return;
      listing = data;
      $('storageUsed').textContent = `已使用 ${bytes(data.storage.used)}／共 ${bytes(data.storage.total)}`;
      $('storageFree').textContent = `可用 ${bytes(data.storage.free)}`;
      $('storageBar').value = data.storage.total ? data.storage.used / data.storage.total * 100 : 0;
      render();
    } catch (error) {
      if (sequence !== serial) return;
      listing = null; $('itemCount').textContent = '';
      $('storageUsed').textContent = '尚未取得空間資訊'; $('storageFree').textContent = ''; $('storageBar').value = 0;
      $('message').className = 'message error';
      $('message').textContent = error.name === 'AbortError' ? '讀取逾時，請重新整理' : error.message;
    } finally {
      clearTimeout(timeout);
      if (sequence === serial) $('refresh').disabled = false;
    }
  }
  $('search').addEventListener('input', render);
  $('refresh').addEventListener('click', load);
  $('previous').addEventListener('click', () => { offset = Math.max(0, offset - 200); load(); });
  $('next').addEventListener('click', () => { offset += 200; load(); });
  $('closePreview').addEventListener('click', () => $('preview').close());
  $('preview').addEventListener('close', () => { $('video').pause(); $('video').removeAttribute('src'); $('video').load(); });
  $('video').addEventListener('error', () => { $('playbackNote').textContent = '瀏覽器無法播放這段影片，請下載後使用 VLC 等播放器開啟。'; });
  addEventListener('popstate', () => navigate(new URLSearchParams(location.search).get('path') || '', false));
  load();
})();
