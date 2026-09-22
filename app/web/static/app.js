/* SafeRoad front-end. Voice = raw mic audio -> WAV -> server (Gemini does all understanding and speech).
   No browser speech APIs. Live updates over Server-Sent Events. Web Push for background alerts. */
(function () {
  const S = (window.SafeRoad = window.SafeRoad || {});
  const $ = (id) => document.getElementById(id);
  const tpl = (id) => ($(id) ? $(id).innerHTML : '');
  S.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ---------- toast ----------
  let toastTimer;
  S.toast = (msg, ms = 4500) => { const t = $('toast'); if (!t) return; t.innerHTML = `<div class="toast-card">${S.esc(msg)}</div>`; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms); };

  // ---------- small form helpers ----------
  S.demoLogin = (role) => { const a = (window.DEMO_ACCOUNTS || {})[role]; if (!a || !a.email) return; const e = $('email'), p = $('password'); e.value = a.email; p.value = a.password; e.dispatchEvent(new Event('input')); p.dispatchEvent(new Event('input')); S.toast(`Logging in to the ${role} demo…`); setTimeout(() => e.closest('form').requestSubmit(), 350); };
  S.togglePw = (id, btn) => { const i = $(id); i.type = i.type === 'password' ? 'text' : 'password'; btn.classList.toggle('text-brand-600', i.type === 'text'); };
  S.previewPhoto = (input) => { const f = input.files && input.files[0]; if (!f) return; const img = $('photo-preview'); img.src = URL.createObjectURL(f); img.hidden = false; $('photo-placeholder').hidden = true; };
  S.otp = (boxId, hiddenId, formId) => {
    const box = $(boxId); if (!box) return; const inputs = [...box.querySelectorAll('input')]; const hidden = $(hiddenId); let submitted = false;
    const form = formId ? $(formId) : inputs[0].closest('form');
    if (form) form.addEventListener('submit', (e) => { if (submitted) { e.preventDefault(); return; } submitted = true; const b = form.querySelector('button[type=submit]'); if (b) { b.disabled = true; b.textContent = 'Checking…'; } });
    const fillFrom = (start, digits) => { [...digits].forEach((c, j) => { if (inputs[start + j]) inputs[start + j].value = c; }); (inputs[Math.min(start + digits.length, 5)]).focus(); };
    const sync = () => { hidden.value = inputs.map((i) => i.value).join(''); if (formId && hidden.value.length === 6 && !submitted) form.requestSubmit(); };
    inputs.forEach((inp, i) => {
      inp.addEventListener('input', () => { const digits = inp.value.replace(/\D/g, ''); if (digits.length > 1) { inp.value = ''; fillFrom(i, digits.slice(0, 6 - i)); } else { inp.value = digits; if (digits && inputs[i + 1]) inputs[i + 1].focus(); } sync(); });
      inp.addEventListener('keydown', (e) => { if (e.key === 'Backspace' && !inp.value && inputs[i - 1]) inputs[i - 1].focus(); });
      inp.addEventListener('paste', (e) => { const t = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 6); if (!t) return; e.preventDefault(); [...t].forEach((c, j) => { if (inputs[j]) inputs[j].value = c; }); (inputs[t.length] || inputs[5]).focus(); sync(); });
    });
    inputs[0].focus();
  };

  // ---------- service worker + install ----------
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  let deferredPrompt = null;
  const installBtns = () => document.querySelectorAll('.install-btn');
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferredPrompt = e; installBtns().forEach((b) => (b.hidden = false)); });
  window.addEventListener('appinstalled', () => { deferredPrompt = null; installBtns().forEach((b) => (b.hidden = true)); S.toast('SafeRoad is installed. Open it from your home screen.'); });
  if (isStandalone) installBtns().forEach((b) => (b.hidden = true));
  S.showInstallHelp = () => { if (isIOS) $('ios-hint').hidden = false; else S.toast('In Chrome: open the ⋮ menu and choose "Add to Home screen" or "Install app".', 7000); };
  S.install = async () => { if (deferredPrompt) { deferredPrompt.prompt(); const r = await deferredPrompt.userChoice; if (r.outcome === 'accepted') deferredPrompt = null; return; } S.showInstallHelp(); };

  // ---------- push ----------
  const b64ToU8 = (b64) => { const p = '='.repeat((4 - (b64.length % 4)) % 4); const s = (b64 + p).replace(/-/g, '+').replace(/_/g, '/'); const raw = atob(s); return Uint8Array.from([...raw].map((c) => c.charCodeAt(0))); };
  async function currentSub() { if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null; const reg = await navigator.serviceWorker.ready; return reg.pushManager.getSubscription(); }
  async function refreshPushUI() {
    const btn = $('enable-push'), m = $('enable-push-m'); if (!window.USER) return;
    const supported = 'PushManager' in window && 'Notification' in window;
    if (!supported) { if (btn) { btn.querySelector('span').textContent = isIOS && !isStandalone ? 'Add to Home Screen for alerts' : 'Alerts unavailable here'; btn.onclick = S.showInstallHelp; } if (m) m.onclick = S.showInstallHelp; return; }
    const sub = await currentSub();
    if (Notification.permission === 'granted' && sub) { if (btn) { btn.querySelector('span').textContent = 'Alerts are on'; btn.classList.replace('btn-primary', 'btn-ghost'); btn.onclick = () => S.testPush(); } if (m) { m.classList.add('text-brand-600'); m.onclick = () => S.testPush(); } }
  }
  S.enablePush = async () => {
    if (isIOS && !isStandalone) return S.showInstallHelp();
    if (!('PushManager' in window)) return S.toast('This browser does not support push notifications.');
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') return S.toast('Notifications were not allowed. You can enable them in your browser settings.');
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToU8(window.VAPID_PUBLIC_KEY) });
      const r = await fetch('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub.toJSON()) });
      if (!r.ok) throw new Error('could not save subscription');
      S.toast('Alerts are on. Sending you a test notification.'); await refreshPushUI(); S.testPush();
    } catch (e) { S.toast('Could not turn on alerts: ' + e.message); }
  };
  S.testPush = () => { fetch('/api/push/test', { method: 'POST' }).then(() => S.toast('Test notification sent.')).catch(() => {}); };
  if (window.USER) { refreshPushUI(); if (window.Notification && Notification.permission === 'granted') currentSub().then((sub) => { if (sub) fetch('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub.toJSON()) }); }); }

  // ---------- duty ----------
  S.toggleDuty = async (el) => { const on = !el.classList.contains('on'); document.querySelectorAll('.duty-switch').forEach((s) => s.classList.toggle('on', on)); const fd = new FormData(); fd.append('on_duty', on); const r = await fetch('/api/duty', { method: 'POST', body: fd }); if (!r.ok) { document.querySelectorAll('.duty-switch').forEach((s) => s.classList.toggle('on', !on)); S.toast('Could not update duty status.'); } else S.toast(on ? 'You are on duty. Residents can call you.' : 'You are off duty.'); };

  // ---------- live events ----------
  S.onEvent = S.onEvent || (() => {});
  function connectEvents() {
    if (!window.USER && !S.adminMode) return;
    const es = new EventSource('/events' + (S.adminMode ? '?admin=1' : ''));
    es.onmessage = (m) => { try { const { event, data } = JSON.parse(m.data); S.onEvent(event, data); } catch (e) {} };
    es.onerror = () => { es.close(); setTimeout(connectEvents, 3000); };
  }
  window.addEventListener('load', connectEvents);

  // =====================================================================
  // Chat
  // =====================================================================
  const lang = () => (window.USER ? USER.language : 'en');
  const chat = () => $('chat');
  const scrollToEnd = () => { const s = $('scroll'); if (s) requestAnimationFrame(() => (s.scrollTop = s.scrollHeight)); };
  S.autosize = (ta) => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'; $('send-btn').disabled = !ta.value.trim(); };
  S.enterToSend = (e) => { if (e.key === 'Enter' && !e.shiftKey && window.innerWidth >= 1024) { e.preventDefault(); $('composer').requestSubmit(); } };

  const chipFor = (m) => {
    if (m.kind === 'escalation') return `<span class="chip chip-caution mb-1.5">${tpl('icon-help')} Resident asking</span>`;
    if (m.kind === 'update') return `<span class="chip ${m.status ? 'st-' + m.status : 'chip-info'} mb-1.5">${tpl('icon-zap')} Update</span>`;
    if (m.status) return `<span class="chip st-${m.status} mb-1.5">${m.status.replace(/_/g, ' ')}</span>`;
    return '';
  };
  S.alertCardHTML = (a) => {
    const text = (a.translations && a.translations[lang()]) || a.summary_en;
    const chips = a.category === 'all_clear' ? `<span class="chip chip-ok">${tpl('icon-check')} All clear</span>` : `<span class="chip chip-${a.severity === 'danger' ? 'danger' : a.severity === 'caution' ? 'caution' : 'info'}">${a.severity}</span><span class="chip chip-muted">${S.esc(a.category.replace(/_/g, ' '))}</span>`;
    const avatar = a.author_photo ? `<img src="${a.author_photo}" class="w-5 h-5 rounded-full object-cover" alt="">` : `<span class="w-5 h-5 rounded-full bg-brand-50 text-brand-600 text-[10px] font-bold flex items-center justify-center">${S.esc(a.author[0])}</span>`;
    return `<div class="alert-card sev-${a.severity} ${a.category === 'all_clear' ? 'all_clear' : ''}" data-alert-id="${a.id}"><div class="flex items-start justify-between gap-2"><div class="flex flex-wrap gap-1.5">${chips}${a.status === 'closed' ? '<span class="chip chip-muted">closed</span>' : ''}</div><span class="text-xs text-muted whitespace-nowrap">${a.time}</span></div>${a.location ? `<p class="mt-2 text-sm font-semibold flex items-center gap-1">${tpl('icon-pin')} ${S.esc(a.location)}</p>` : ''}<p class="mt-1 text-[.95rem] leading-relaxed">${S.esc(text)}</p><div class="mt-3 flex items-center gap-2 text-xs text-muted">${avatar}<span>Confirmed by <b class="text-ink">${S.esc(a.author)}</b> · ${a.ago}</span></div></div>`;
  };
  const voiceHTML = (src, text) => `<div class="bubble"><div class="voice" data-src="${S.esc(src || '')}"><button class="voice-btn" onclick="SafeRoad.playVoice(this)" aria-label="Play voice note">${tpl('icon-play')}</button><div class="bars">${[6, 10, 14, 18, 12, 8, 16, 20, 11, 7, 6, 10, 14, 18, 12, 8, 16, 20, 11, 7, 9, 13].map((h) => `<i style="height:${h}px"></i>`).join('')}</div></div>${text ? `<p class="mt-2 text-sm opacity-90">${S.esc(text)}</p>` : ''}</div>`;

  S.renderMessage = (m) => {
    const c = chat(); if (!c) return null;
    let el = c.querySelector(`[data-id="${m.id}"]`);
    const fresh = !el;
    if (fresh) { el = document.createElement('div'); el.dataset.id = m.id; }
    el.className = `msg ${m.role === 'user' ? 'me' : 'ai'}`;
    let body;
    if (m.kind === 'alert' && m.alert) body = S.alertCardHTML(m.alert) + `<div class="msg-actions"><button class="btn btn-ghost" onclick="SafeRoad.speak('${m.id}', this)">${tpl('icon-volume')} Listen</button></div>`;
    else if (m.kind === 'voice') body = voiceHTML(m.audio_url, m.text);
    else {
      const actions = m.role === 'assistant' && m.kind !== 'system' ? `<div class="msg-actions"><button class="btn btn-ghost" onclick="SafeRoad.speak('${m.id}', this)" data-audio="${S.esc(m.audio_url || '')}">${tpl('icon-volume')} Listen</button>${m.contact ? `<a class="btn btn-call" href="tel:${S.esc(m.contact.phone)}">${tpl('icon-phone')} Call ${S.esc(m.contact.name.split(' ')[0])}</a>` : ''}</div>` : '';
      body = `<div class="bubble ${m.kind === 'system' ? 'system' : ''}">${chipFor(m)}<p>${S.esc(m.text)}</p>${m.alert ? `<div class="mt-2.5">${S.alertCardHTML(m.alert)}</div>` : ''}${actions}</div>`;
    }
    el.innerHTML = body + `<div class="meta">${m.time || ''}</div>`;
    if (m.created_at) el.dataset.ts = m.created_at; else if (!el.dataset.ts) el.dataset.ts = new Date().toISOString();
    if (fresh) {
      const lastDay = [...c.querySelectorAll('.day')].pop(); if (m.day && (!lastDay || lastDay.textContent !== m.day)) { const d = document.createElement('div'); d.className = 'day'; d.textContent = m.day; c.appendChild(d); }
      // insert by timestamp so order never depends on which message arrived first (live event vs. upload response)
      const msgs = [...c.querySelectorAll('.msg')]; const after = msgs.reverse().find((x) => (x.dataset.ts || '') <= el.dataset.ts);
      if (after && after.nextSibling) after.parentNode.insertBefore(el, after.nextSibling); else c.appendChild(el);
    }
    scrollToEnd();
    return el;
  };
  let typingEl = null;
  const showTyping = () => { hideTyping(); typingEl = document.createElement('div'); typingEl.className = 'msg ai'; typingEl.innerHTML = '<div class="bubble typing"><i></i><i></i><i></i></div>'; chat().appendChild(typingEl); scrollToEnd(); };
  const hideTyping = () => { if (typingEl) { typingEl.remove(); typingEl = null; } };

  // ---------- sending ----------
  let pendingLocal = null, queued = [];
  const flushQueued = () => { const q = queued; queued = []; q.forEach((d) => S.renderMessage(d)); };
  S.submitText = (e) => { e.preventDefault(); const ta = $('text-input'); const text = ta.value.trim(); if (!text) return false; ta.value = ''; S.autosize(ta); const fd = new FormData(); fd.append('text', text); S.send(fd, { text }); return false; };
  S.send = async (fd, local) => {
    const tempId = 'tmp-' + Date.now();
    // stamp the pending bubble as strictly the newest item, so it always lands at the bottom; the server timestamp replaces it on success
    const lastTs = [...chat().querySelectorAll('.msg')].map((x) => x.dataset.ts || '').sort().pop() || '';
    const nowTs = new Date().toISOString();
    pendingLocal = S.renderMessage({ id: tempId, role: 'user', kind: local.audio ? 'voice' : 'text', text: local.text || '', audio_url: local.audio || '', time: 'sending…', created_at: (lastTs > nowTs ? lastTs : nowTs) + '~' });
    showTyping();
    try {
      const r = await fetch('/api/chat', { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Request failed');
      if (pendingLocal) { pendingLocal.dataset.id = data.user_message.id; pendingLocal.dataset.ts = data.user_message.created_at || pendingLocal.dataset.ts; pendingLocal = null; }
      S.renderMessage(data.user_message); hideTyping(); S.renderMessage(data.reply); flushQueued();
      if (data.autoplay) { S.wantAutoplay = data.reply.id; const btn = chat().querySelector(`[data-id="${data.reply.id}"] button[data-audio]`); if (btn && btn.dataset.audio) { S.wantAutoplay = null; S.speak(data.reply.id, btn); } }
    } catch (e) { hideTyping(); if (pendingLocal) { pendingLocal.querySelector('.meta').textContent = 'not sent'; pendingLocal = null; } flushQueued(); S.toast(e.message, 7000); }
  };

  // ---------- voice recording: raw PCM -> WAV ----------
  let ctx, stream, source, proc, chunks = [], recording = false, startedAt = 0, timer;
  const TARGET_RATE = 16000;
  function downsample(buf, from, to) { if (to >= from) return buf; const ratio = from / to, n = Math.round(buf.length / ratio), out = new Float32Array(n); let o = 0, i = 0; while (o < n) { const next = Math.round((o + 1) * ratio); let sum = 0, c = 0; for (; i < next && i < buf.length; i++) { sum += buf[i]; c++; } out[o++] = c ? sum / c : 0; } return out; }
  function toWav(samples, rate) { const b = new ArrayBuffer(44 + samples.length * 2), v = new DataView(b); const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); }; w(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); w(8, 'WAVE'); w(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, samples.length * 2, true); let o = 44; for (let i = 0; i < samples.length; i++, o += 2) { const s = Math.max(-1, Math.min(1, samples[i])); v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true); } return new Blob([v], { type: 'audio/wav' }); }
  const fmtTime = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  S.toggleRecord = async () => {
    const mic = $('mic');
    if (!recording) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
        ctx = new (window.AudioContext || window.webkitAudioContext)(); await ctx.resume();
        source = ctx.createMediaStreamSource(stream); proc = ctx.createScriptProcessor(4096, 1, 1); chunks = [];
        proc.onaudioprocess = (e) => { if (recording) chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
        source.connect(proc); proc.connect(ctx.destination);
        recording = true; startedAt = Date.now();
        mic.classList.add('rec'); mic.innerHTML = tpl('icon-stop'); $('input-wrap').hidden = true; $('send-btn').hidden = true; $('rec-bar').hidden = false;
        timer = setInterval(() => { const s = Math.floor((Date.now() - startedAt) / 1000); $('rec-time').textContent = fmtTime(s); if (s >= 60) S.toggleRecord(); }, 250);
      } catch (e) { S.toast('Microphone not available: ' + e.message); }
      return;
    }
    recording = false; clearInterval(timer);
    mic.classList.remove('rec'); mic.innerHTML = tpl('icon-mic'); $('input-wrap').hidden = false; $('send-btn').hidden = false; $('rec-bar').hidden = true;
    try { proc.disconnect(); source.disconnect(); stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    const rate = ctx.sampleRate; await ctx.close();
    const total = chunks.reduce((n, c) => n + c.length, 0); const all = new Float32Array(total); let off = 0; for (const c of chunks) { all.set(c, off); off += c.length; }
    if (total < rate * 0.5) return S.toast('That was too short. Tap the mic and speak, then tap ■ to send.');
    const wav = toWav(downsample(all, rate, TARGET_RATE), TARGET_RATE);
    const fd = new FormData(); fd.append('audio', wav, 'note.wav');
    S.send(fd, { audio: URL.createObjectURL(wav) });
  };

  // ---------- playback ----------
  let player = null, playingBtn = null;
  const stopPlayer = () => { if (player) { player.pause(); player = null; } document.querySelectorAll('.voice.playing').forEach((v) => v.classList.remove('playing')); if (playingBtn) { playingBtn.innerHTML = playingBtn.dataset.idle || playingBtn.innerHTML; playingBtn.disabled = false; playingBtn = null; } };
  const play = (url, onEnd) => { stopPlayer(); player = new Audio(url); player.onended = () => { stopPlayer(); onEnd && onEnd(); }; player.play().catch(() => S.toast('Tap the speaker button to hear it.')); };
  S.playVoice = (btn) => { const wrap = btn.closest('.voice'); if (wrap.classList.contains('playing')) return stopPlayer(); play(wrap.dataset.src, () => wrap.classList.remove('playing')); wrap.classList.add('playing'); btn.dataset.idle = tpl('icon-play'); btn.innerHTML = tpl('icon-pause'); playingBtn = btn; };
  S.speak = async (id, btn) => {
    try {
      if (playingBtn === btn) return stopPlayer();
      const idle = `${tpl('icon-volume')} Listen`; btn.dataset.idle = idle; btn.disabled = true; btn.innerHTML = `${tpl('icon-loader')} Loading`;
      let url = btn.dataset.audio;
      if (!url) { const fd = new FormData(); fd.append('id', id); const r = await fetch('/api/speak', { method: 'POST', body: fd }); const d = await r.json(); if (!r.ok) throw new Error(d.detail || 'voice failed'); url = d.url; btn.dataset.audio = url; }
      btn.disabled = false; btn.innerHTML = `${tpl('icon-pause')} Playing`; playingBtn = btn; play(url);
    } catch (e) { btn.disabled = false; btn.innerHTML = `${tpl('icon-volume')} Listen`; S.toast('Voice is not available right now: ' + e.message); }
  };

  // ---------- live handlers ----------
  S.chatInit = () => {
    scrollToEnd();
    S.onEvent = (ev, d) => {
      if (ev === 'message') { if (pendingLocal && (d.role === 'user' || d.kind === 'answer' || d.kind === 'readback')) { queued.push(d); return; } S.renderMessage(d); if (d.kind === 'alert') S.toast((d.alert && d.alert.author ? d.alert.author + ': ' : '') + d.text, 6000); if (d.kind === 'escalation') S.toast(d.text, 6000); if (d.kind === 'update') S.toast('Update: ' + d.text, 6000); }
      if (ev === 'message_audio') { const btn = chat().querySelector(`[data-id="${d.id}"] button[data-audio]`); if (btn) btn.dataset.audio = d.audio_url; if (S.wantAutoplay === d.id && btn) { S.wantAutoplay = null; S.speak(d.id, btn); } }
      if (ev === 'new_alert') { const list = $('live-list'); if (list) { $('live-empty')?.remove(); if (!list.querySelector(`[data-alert-id="${d.id}"]`)) list.insertAdjacentHTML('afterbegin', S.alertCardHTML(d)); } }
      if (ev === 'escalation') { const list = $('waiting-list'); if (list) { $('waiting-empty')?.remove(); list.insertAdjacentHTML('afterbegin', `<div class="live-card text-sm" data-question-id="${d.id}"><p class="font-medium">${S.esc(d.text_en)}</p><p class="text-xs text-muted mt-1">${S.esc(d.asker)} · just now${d.location ? ' · ' + S.esc(d.location) : ''}</p></div>`); const c = $('waiting-count'); if (c) c.textContent = list.children.length; } }
      if (ev === 'question_resolved') { const el = document.querySelector(`#waiting-list [data-question-id="${d.id}"]`); if (el) { el.remove(); const c = $('waiting-count'); if (c) c.textContent = $('waiting-list').children.length; } }
    };
  };
})();
