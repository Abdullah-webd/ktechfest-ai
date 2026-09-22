/* SafeRoad front-end: voice capture (raw audio -> WAV, no browser speech APIs), install prompt,
   Web Push subscription, Server-Sent Events live feed, and small UI helpers. */
(function () {
  const S = (window.SafeRoad = window.SafeRoad || {});
  const $ = (id) => document.getElementById(id);
  S.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ---------- toast ----------
  let toastTimer;
  S.toast = (msg, ms = 4500) => {
    const t = $('toast'); if (!t) return;
    t.innerHTML = `<div class="card px-4 py-3 text-sm shadow-xl fade-in">${S.esc(msg)}</div>`;
    t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), ms);
  };

  // ---------- service worker ----------
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

  // ---------- install ----------
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  let deferredPrompt = null;
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferredPrompt = e; document.querySelectorAll('.install-btn').forEach((b) => (b.hidden = false)); });
  window.addEventListener('appinstalled', () => { deferredPrompt = null; document.querySelectorAll('.install-btn').forEach((b) => (b.hidden = true)); S.toast('✅ SafeRoad installed. Open it from your home screen.'); });
  if (isStandalone) document.querySelectorAll('.install-btn').forEach((b) => (b.hidden = true));
  S.showInstallHelp = () => { if (isIOS) $('ios-hint').hidden = false; else S.toast('On Android/Chrome: tap the ⋮ menu → "Add to Home screen" / "Install app".', 7000); };
  S.install = async () => {
    if (deferredPrompt) { deferredPrompt.prompt(); const r = await deferredPrompt.userChoice; if (r.outcome === 'accepted') deferredPrompt = null; return; }
    S.showInstallHelp();
  };

  // ---------- push ----------
  const b64ToU8 = (b64) => { const p = '='.repeat((4 - (b64.length % 4)) % 4); const s = (b64 + p).replace(/-/g, '+').replace(/_/g, '/'); const raw = atob(s); return Uint8Array.from([...raw].map((c) => c.charCodeAt(0))); };
  async function currentSubscription() { if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null; const reg = await navigator.serviceWorker.ready; return reg.pushManager.getSubscription(); }
  async function refreshPushButton() {
    const btn = $('enable-push'); if (!btn || !window.USER) return;
    const supported = 'PushManager' in window && 'Notification' in window;
    if (!supported) { btn.textContent = isIOS && !isStandalone ? '🔔 Alerts: add to Home Screen first' : '🔔 Alerts unavailable here'; btn.onclick = S.showInstallHelp; return; }
    const sub = await currentSubscription();
    if (Notification.permission === 'granted' && sub) { btn.textContent = '🔔 Alerts on'; btn.classList.replace('btn-amber', 'btn-ghost'); btn.onclick = () => S.testPush(); }
  }
  S.enablePush = async () => {
    if (isIOS && !isStandalone) return S.showInstallHelp();
    if (!('PushManager' in window)) return S.toast('This browser does not support push notifications.');
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') return S.toast('Notifications were not allowed. You can enable them in browser settings.');
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToU8(window.VAPID_PUBLIC_KEY) });
      const r = await fetch('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub.toJSON()) });
      if (!r.ok) throw new Error('subscribe failed');
      S.toast('🔔 Alerts are on. Sending a test…'); await refreshPushButton(); S.testPush();
    } catch (e) { console.error(e); S.toast('Could not enable alerts: ' + e.message); }
  };
  S.testPush = () => fetch('/api/push/test', { method: 'POST' }).catch(() => {});
  if (window.USER) { refreshPushButton(); if (Notification?.permission === 'granted') currentSubscription().then((sub) => { if (sub) fetch('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub.toJSON()) }); }); }

  // ---------- live events ----------
  S.onEvent = S.onEvent || (() => {});
  function connectEvents() {
    if (!window.USER && !S.adminMode) return;
    const es = new EventSource('/events' + (S.adminMode ? '?admin=1' : ''));
    es.onmessage = (m) => { try { const { event, data } = JSON.parse(m.data); S.onEvent(event, data); } catch (e) {} };
    es.onerror = () => { es.close(); setTimeout(connectEvents, 3000); };
  }
  window.addEventListener('load', connectEvents);

  // ---------- voice: raw PCM -> WAV ----------
  let ctx, stream, source, proc, chunks = [], recording = false, startedAt = 0, timer;
  const TARGET_RATE = 16000;
  function downsample(buf, from, to) { if (to >= from) return buf; const ratio = from / to, n = Math.round(buf.length / ratio), out = new Float32Array(n); let o = 0, i = 0; while (o < n) { const next = Math.round((o + 1) * ratio); let sum = 0, c = 0; for (; i < next && i < buf.length; i++) { sum += buf[i]; c++; } out[o++] = c ? sum / c : 0; } return out; }
  function toWav(samples, rate) { const b = new ArrayBuffer(44 + samples.length * 2), v = new DataView(b); const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); }; w(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); w(8, 'WAVE'); w(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, samples.length * 2, true); let o = 44; for (let i = 0; i < samples.length; i++, o += 2) { const s = Math.max(-1, Math.min(1, samples[i])); v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true); } return new Blob([v], { type: 'audio/wav' }); }

  S.toggleRecord = async (endpoint) => {
    const mic = $('mic'), status = $('rec-status');
    if (!recording) {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
        ctx = new (window.AudioContext || window.webkitAudioContext)();
        await ctx.resume();
        source = ctx.createMediaStreamSource(stream);
        proc = ctx.createScriptProcessor(4096, 1, 1);
        chunks = [];
        proc.onaudioprocess = (e) => { if (recording) chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
        source.connect(proc); proc.connect(ctx.destination);
        recording = true; startedAt = Date.now();
        mic.classList.add('rec'); mic.textContent = '■';
        status.hidden = false; status.textContent = 'Listening… tap again when done';
        timer = setInterval(() => { const s = Math.floor((Date.now() - startedAt) / 1000); status.textContent = `Listening… ${s}s · tap ■ when done`; if (s >= 60) S.toggleRecord(endpoint); }, 500);
      } catch (e) { S.toast('Microphone not available: ' + e.message); }
      return;
    }
    recording = false; clearInterval(timer);
    mic.classList.remove('rec'); mic.textContent = '🎤';
    try { proc.disconnect(); source.disconnect(); stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    const rate = ctx.sampleRate; await ctx.close();
    const total = chunks.reduce((n, c) => n + c.length, 0); const all = new Float32Array(total); let off = 0; for (const c of chunks) { all.set(c, off); off += c.length; }
    if (total < rate * 0.4) { status.hidden = true; return S.toast('That was too short. Tap the mic and speak.'); }
    const wav = toWav(downsample(all, rate, TARGET_RATE), TARGET_RATE);
    status.textContent = 'Sending…';
    const fd = new FormData(); fd.append('audio', wav, 'note.wav');
    await S.send(endpoint, fd, '🎤 voice note');
    status.hidden = true;
  };

  S.submitText = (e, endpoint) => { e.preventDefault(); const inp = $('text-input'); const text = inp.value.trim(); if (!text) return false; inp.value = ''; const fd = new FormData(); fd.append('text', text); S.send(endpoint, fd, text); return false; };

  // ---------- chat rendering ----------
  const chat = () => $('chat');
  function bubbleMe(text) { const d = document.createElement('div'); d.className = 'bubble bubble-me fade-in'; d.textContent = text; chat().appendChild(d); scrollDown(); return d; }
  function bubbleTyping() { const d = document.createElement('div'); d.className = 'bubble bubble-ai fade-in typing'; d.innerHTML = '<span></span><span></span><span></span>'; chat().appendChild(d); scrollDown(); return d; }
  function scrollDown() { window.scrollTo({ top: chat().getBoundingClientRect().bottom + window.scrollY - window.innerHeight + 160, behavior: 'smooth' }); }
  let pendingMe = null, pendingTyping = null;
  S.send = async (endpoint, fd, label) => {
    pendingMe = bubbleMe(label); pendingTyping = bubbleTyping();
    try {
      const r = await fetch(endpoint, { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || 'Request failed');
      if (data.transcript && pendingMe) pendingMe.textContent = data.transcript;
      pendingTyping.remove(); pendingTyping = null;
      S.onResult && S.onResult(data);
    } catch (e) { if (pendingTyping) pendingTyping.remove(); S.toast('Something went wrong: ' + e.message, 7000); }
  };

  S.renderAnswer = (q, autoplay, isUpdate) => {
    const d = document.createElement('div'); d.className = 'bubble bubble-ai fade-in'; d.dataset.questionId = q.id;
    const badge = q.status && q.status !== 'n/a' ? `<span class="badge st-${q.status}">${q.status.replace(/_/g, ' ')}</span>` : '';
    const call = q.contact && !q.resolved ? `<a class="btn btn-mint text-xs py-1.5 px-3" href="tel:${S.esc(q.contact.phone)}">📞 Call ${S.esc(q.contact.name.split(' ')[0])} (on duty)</a>` : '';
    d.innerHTML = `${isUpdate ? '<p class="text-[11px] text-amber font-semibold mb-1">UPDATE</p>' : ''}${badge}<p class="mt-1">${S.esc(q.answer_local || q.answer_en)}</p><div class="mt-2 flex flex-wrap gap-2 items-center"><button class="btn btn-ghost text-xs py-1.5 px-3" onclick="SafeRoad.speak('question', '${q.id}', this)">🔊 Listen</button>${call}<span class="text-[11px] text-gray-500">${q.time || ''}</span></div>`;
    chat().appendChild(d); scrollDown();
    if (autoplay) S.speak('question', q.id, d.querySelector('button'));
  };
  S.renderReadback = (r) => {
    const d = document.createElement('div'); d.className = 'bubble bubble-ai fade-in';
    if (!r.is_report) { d.innerHTML = `<p>${S.esc(r.readback_local || 'Noted.')}</p>`; chat().appendChild(d); scrollDown(); return; }
    const a = r.alert;
    d.innerHTML = `<span class="badge ${a.category === 'all_clear' ? 'cat-all_clear' : 'sev-' + a.severity}">${a.category.replace(/_/g, ' ')}</span> <span class="text-xs text-gray-400">📍 ${S.esc(a.location || '-')}</span><p class="mt-1">${S.esc(r.readback_local)}</p><p class="mt-1 text-xs text-gray-400">${S.esc(a.summary_en)}</p><p class="mt-2 text-xs text-mint">✓ Sending to all residents in their languages…</p><div class="mt-2"><button class="btn btn-ghost text-xs py-1.5 px-3" onclick="SafeRoad.speak('alert', '${a.id}', this)">🔊 Listen</button></div>`;
    chat().appendChild(d); scrollDown();
    S.speak('alert', a.id, d.querySelector('button'));
  };

  // ---------- feed rendering ----------
  S.alertCard = (a) => {
    const lang = window.USER ? USER.language : 'en'; const text = a.translations?.[lang] || a.summary_en;
    const badges = a.category === 'all_clear' ? `<span class="badge cat-all_clear">All clear</span>` : `<span class="badge sev-${a.severity}">${a.severity}</span><span class="badge bg-white/5 text-gray-300">${a.category.replace(/_/g, ' ')}</span>`;
    const avatar = a.author_photo ? `<img src="${a.author_photo}" class="w-6 h-6 rounded-full object-cover" alt="">` : `<span class="w-6 h-6 rounded-full bg-amber/20 text-amber flex items-center justify-center text-[10px] font-bold">${S.esc(a.author[0])}</span>`;
    return `<div class="flex items-start justify-between gap-2"><div class="flex gap-2 flex-wrap items-center">${badges}</div><span class="text-xs text-gray-400 whitespace-nowrap">${a.time} · ${a.ago}</span></div>${a.location ? `<p class="mt-2 font-semibold">📍 ${S.esc(a.location)}</p>` : ''}<p class="mt-1 text-gray-200">${S.esc(text)}</p><div class="mt-3 flex items-center justify-between gap-2"><div class="flex items-center gap-2 text-xs text-gray-400">${avatar}<span>Confirmed by <b class="text-gray-200">${S.esc(a.author)}</b></span></div><button class="btn btn-ghost text-xs py-1.5 px-3" onclick="SafeRoad.speak('alert', '${a.id}', this)">🔊 Listen</button></div>`;
  };
  S.prependAlert = (a) => { const feed = $('feed'); if (!feed) return; $('feed-empty')?.remove(); if (feed.querySelector(`[data-alert-id="${a.id}"]`)) return; const el = document.createElement('article'); el.className = 'card p-4 fade-in ring-2 ring-amber/60'; el.dataset.alertId = a.id; el.innerHTML = S.alertCard(a); feed.prepend(el); setTimeout(() => el.classList.remove('ring-2', 'ring-amber/60'), 6000); };
  S.prependQuestion = (q) => { const box = $('questions'); if (!box) return; $('q-empty')?.remove(); if (box.querySelector(`[data-question-id="${q.id}"]`)) return; const el = document.createElement('article'); el.className = 'card p-4 fade-in ring-2 ring-amber/60'; el.dataset.questionId = q.id; el.innerHTML = `<div class="flex justify-between gap-2"><span class="badge st-no_information">waiting</span><span class="text-xs text-gray-400">just now</span></div><p class="mt-2 text-gray-100">❓ ${S.esc(q.text_en)}</p>${q.location ? `<p class="text-xs text-gray-400 mt-1">📍 ${S.esc(q.location)}</p>` : ''}<p class="text-xs text-gray-500 mt-2">Asked by ${S.esc(q.asker)}. Post an update about this and they will be notified automatically.</p>`; box.prepend(el); };

  // ---------- voice playback ----------
  let player = null;
  S.speak = async (kind, id, btn) => {
    try {
      if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
      const fd = new FormData(); fd.append('kind', kind); fd.append('id', id);
      const r = await fetch('/api/speak', { method: 'POST', body: fd }); const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'voice failed');
      if (player) { player.pause(); }
      player = new Audio(d.url); player.play().catch(() => S.toast('Tap 🔊 to hear the reply.'));
      if (btn) { btn.textContent = '🔊 Playing'; player.onended = () => { btn.textContent = '🔊 Listen'; btn.disabled = false; }; }
    } catch (e) { if (btn) { btn.textContent = '🔊 Listen'; btn.disabled = false; } S.toast('Voice unavailable: ' + e.message); }
  };

  // ---------- duty toggle ----------
  S.toggleDuty = async (el) => { const on = !el.classList.contains('on'); el.classList.toggle('on', on); const fd = new FormData(); fd.append('on_duty', on); const r = await fetch('/api/duty', { method: 'POST', body: fd }); if (!r.ok) el.classList.toggle('on', !on); else S.toast(on ? '✅ You are on duty. Residents can call you.' : 'You are off duty.'); };
})();
