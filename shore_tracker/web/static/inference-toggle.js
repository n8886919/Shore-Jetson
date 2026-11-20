(() => {
  const toggle = document.getElementById('inferenceEnabled');
  if (!toggle) return;
  const message = document.getElementById('inferenceToggleMessage');
  let applied = false, locked = false, online = false, pending = false;
  function buttons() {
    toggle.checked = applied;
    toggle.disabled = !online || locked || pending;
    toggle.title = locked ? '錄影期間保持開啟' : 'AI 辨識';
  }
  window.shoreInferenceControl = {
    render(status, connected = true) {
      online = connected && !status.offline_preview && typeof status.inference?.enabled === 'boolean'
        && ['idle','ready','stale'].includes(status.components?.inference?.state);
      applied = status.inference?.enabled === true;
      locked = status.inference?.locked === true;
      message.textContent = locked ? '錄影期間保持開啟' : status.keyboard?.enabled ? '方向鍵長按移動，放開停止' : '';
      message.hidden = !message.textContent;
      buttons();
    },
    disconnect() { online = false; buttons(); }
  };
  toggle.addEventListener('change', async () => {
    if (!online || locked || pending) { buttons(); return; }
    const enabled = toggle.checked;
    pending = true;
    buttons();
    try {
      const response = await fetch('/api/inference', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({enabled}),signal:AbortSignal.timeout(5000)});
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || 'AI 辨識設定未套用');
      const check = await fetch('/api/status', {cache:'no-store',signal:AbortSignal.timeout(5000)});
      if (!check.ok) throw new Error('無法確認 AI 辨識設定');
      const status = await check.json();
      window.shoreInferenceControl.render(status);
      if (applied !== enabled && !locked) throw new Error('AI 辨識設定讀回不一致');
    } catch (error) {
      message.textContent = error.message || 'AI 辨識設定未確認';
      message.hidden = false;
    } finally { pending = false; buttons(); }
  });
})();
