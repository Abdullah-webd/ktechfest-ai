"""Drive two live sessions (responder left, resident right), record both, and log narration offsets."""
import asyncio, json, time, wave, sys, os
sys.path.insert(0, "demo"); import _dns  # noqa: E402  public DNS for this Mac's flaky resolver
from playwright.async_api import async_playwright

BASE = "https://saferoad-production.up.railway.app"
import dns.resolver
def _pin_rules():
    rules = []
    for host in ["saferoad-production.up.railway.app", "cdn.tailwindcss.com", "fonts.googleapis.com", "fonts.gstatic.com"]:
        try:
            ip = dns.resolver.resolve(host, "A")[0].address; rules.append(f"MAP {host} {ip}")
        except Exception as e:  # noqa: BLE001
            print("pin failed for", host, e)
    return "--host-resolver-rules=" + ",".join(rules)
PIN = _pin_rules(); print(PIN)
OUT = "demo/rec"; os.makedirs(OUT, exist_ok=True)
NARR = json.load(open("demo/narration.json"))
W, H = 540, 960
T0 = None; events = []
def mark(name):
    events.append({"name": name, "t": round(time.time() - T0, 2)}); print(f"[{events[-1]['t']:6.1f}s] {name}")
async def narrate(key):
    mark(f"vo:{key}"); return NARR[key]["seconds"]

MIC_JS = """
(() => {
  window.__setMicClip = (b64) => { window.__micClip = b64; };
  navigator.mediaDevices.getUserMedia = async () => {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const bytes = Uint8Array.from(atob(window.__micClip), (c) => c.charCodeAt(0));
    const buf = await ctx.decodeAudioData(bytes.buffer.slice(0));
    const src = ctx.createBufferSource(); src.buffer = buf;
    const dest = ctx.createMediaStreamDestination(); src.connect(dest); src.start();
    return dest.stream;
  };
})();
"""
CURSOR_JS = """
(() => {
  const c = document.createElement('div'); c.id = '__cur';
  c.style.cssText = 'position:fixed;z-index:99999;width:22px;height:22px;pointer-events:none;left:-50px;top:-50px;transform:translate(-3px,-2px);transition:left .05s linear,top .05s linear;filter:drop-shadow(0 1px 2px rgba(0,0,0,.4))';
  c.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22"><path d="M5 3l14 8.5-6.2 1.6L9.6 19z" fill="#111" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const b = document.createElement('div'); b.id='__lab';
  b.style.cssText = 'position:fixed;z-index:99998;top:0;left:0;right:0;height:26px;background:#0B1526;color:#fff;font:600 12px/26px Inter,system-ui,sans-serif;text-align:center;letter-spacing:.04em';
  b.textContent = window.__LABEL || '';
  const clk = document.createElement('span'); clk.style.cssText = 'position:absolute;right:10px;top:0;font-variant-numeric:tabular-nums;opacity:.85'; b.appendChild(clk);
  setInterval(() => { const d = new Date(); clk.textContent = d.toLocaleTimeString('en-GB') + '.' + String(Math.floor(d.getMilliseconds()/100)); }, 100);
  const add = () => { if (!document.body) return requestAnimationFrame(add); document.body.appendChild(c); document.body.appendChild(b); document.documentElement.style.setProperty('--sat','26px'); };
  add();
  window.__moveCursor = (x, y) => { c.style.left = x + 'px'; c.style.top = y + 'px'; };
  window.__clickFx = () => { c.animate([{transform:'translate(-3px,-2px) scale(1)'},{transform:'translate(-3px,-2px) scale(.7)'},{transform:'translate(-3px,-2px) scale(1)'}], {duration:220}); };
})();
"""

async def glide(pg, sel, *, steps=18, click=True):
    el = pg.locator(sel).first
    await el.wait_for(state="visible", timeout=30000)
    box = await el.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cur = await pg.evaluate("() => { const c=document.getElementById('__cur'); return [parseFloat(c.style.left)||40, parseFloat(c.style.top)||40]; }")
    for i in range(1, steps + 1):
        t = i / steps; e = t * t * (3 - 2 * t)
        await pg.evaluate("([x,y]) => window.__moveCursor(x,y)", [cur[0] + (x - cur[0]) * e, cur[1] + (y - cur[1]) * e])
        await asyncio.sleep(0.03)
    if click:
        await pg.evaluate("() => window.__clickFx()")
        await el.click(force=True)

async def type_slowly(pg, sel, text):
    await glide(pg, sel)
    await pg.type(sel, text, delay=38)
    await pg.evaluate("() => { const t=document.getElementById('text-input'); t.dispatchEvent(new Event('input')); }")

async def wait_reply(pg, previous_count, timeout=60):
    """Wait until a new assistant bubble appears after `previous_count` messages."""
    end = time.time() + timeout
    while time.time() < end:
        n = await pg.evaluate(AI_COUNT)
        if n > previous_count: return n
        await asyncio.sleep(0.4)
    return previous_count

AI_COUNT = "() => [...document.querySelectorAll('#chat .msg.ai')].filter(e => !e.querySelector('.typing')).length"
async def ai_count(pg): return await pg.evaluate(AI_COUNT)

async def main():
    global T0
    async with async_playwright() as p:
        b_resp = await p.chromium.launch(args=[PIN, "--autoplay-policy=no-user-gesture-required"])
        b_res = await p.chromium.launch(args=[PIN])
        ctxR = await b_resp.new_context(viewport={"width": W, "height": H}, device_scale_factor=2, is_mobile=True, permissions=["microphone"], record_video_dir=OUT + "/responder", record_video_size={"width": W, "height": H})
        ctxA = await b_res.new_context(viewport={"width": W, "height": H}, device_scale_factor=2, is_mobile=True, record_video_dir=OUT + "/resident", record_video_size={"width": W, "height": H})
        await ctxR.add_init_script("window.__LABEL='RESPONDER · Musa Bello · vigilante group';" + CURSOR_JS + MIC_JS)
        await ctxA.add_init_script("window.__LABEL='RESIDENT · Amara Okafor';" + CURSOR_JS)
        t_pageR = time.time(); R = await ctxR.new_page()
        t_pageA = time.time(); A = await ctxA.new_page()
        for pg in (R, A): pg.set_default_timeout(30000); pg.set_default_navigation_timeout(45000)
        await R.goto(BASE + "/login", wait_until="domcontentloaded"); await A.goto(BASE + "/login", wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        T0 = time.time(); mark("start")
        meta_lead = {"leadL": T0 - t_pageR, "leadR": T0 - t_pageA}
        try:
            await asyncio.wait_for(run_story(R, A), timeout=300)
        finally:
            mark("end")
            await ctxR.close(); await ctxA.close(); await b_resp.close(); await b_res.close()
            json.dump({"events": events, **meta_lead}, open(f"{OUT}/events.json", "w"), indent=1)
            print("total:", events[-1]["t"], "s")

async def run_story(R, A):
    global T0
    import base64
    clip = lambda path: base64.b64encode(open(path, "rb").read()).decode()
    def clip_seconds(path):
        b = open(path, "rb").read(); return max(0.5, (len(b) - 44) / (24000 * 2))   # header-independent

    async def voice_note(pg, path, label):
        await pg.evaluate("(b) => window.__setMicClip(b)", clip(path))
        await glide(pg, "#mic"); mark(f"clip:{label}")            # the clip is audible in the final mix from this moment
        await asyncio.sleep(clip_seconds(path) + 0.6); await glide(pg, "#mic")
        mark(f"{label} sent")

    async def wait_alert_card(pg, previous, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            n = await pg.evaluate("() => document.querySelectorAll('#chat .alert-card').length")
            if n > previous: return n
            await asyncio.sleep(0.4)
        return previous
    async def cards(pg): return await pg.evaluate("() => document.querySelectorAll('#chat .alert-card').length")
    def since(name): return time.time() - T0 - next(e["t"] for e in reversed(events) if e["name"] == name)
    async def finish(key, pad=0.4): await asyncio.sleep(max(0, NARR[key]["seconds"] - since(f"vo:{key}") + pad))

    # ---- 1. stage: log both in during the opening line
    await narrate("stage"); await asyncio.sleep(0.8)
    await glide(R, "#email"); await R.fill("#email", "musa.demo@saferoad.app"); await R.fill("#password", "demo1234"); await glide(R, "button[type=submit]")
    await asyncio.sleep(0.6)
    await glide(A, "#email"); await A.fill("#email", "amara.demo@saferoad.app"); await A.fill("#password", "demo1234"); await glide(A, "button[type=submit]")
    await R.wait_for_url("**/chat", wait_until="domcontentloaded"); await A.wait_for_url("**/chat", wait_until="domcontentloaded")
    await finish("stage"); mark("both logged in")

    # ---- 2. Musa's voice note (audible), then the explanation while the agent works
    await narrate("voice_intro"); await finish("voice_intro", 0.2)
    cA = await cards(A)
    await voice_note(R, "demo/musa_note.wav", "note1")
    await narrate("voice_after")
    await wait_alert_card(A, cA, 60); mark("alert 1 on resident")
    await finish("voice_after", 0.3)

    # ---- 3. dropped on Amara's side (the cursor points at Listen; no click, no playback)
    await narrate("dropped")
    await asyncio.sleep(1.0)
    await glide(A, "#chat .msg.ai:last-child .msg-actions .btn-ghost", click=False); mark("points at listen")
    await finish("dropped", 0.3)
    await narrate("nopanic"); await finish("nopanic", 0.3)

    # ---- 4. coast clear: second voice note (audible)
    await narrate("clear"); await finish("clear", 0.2)
    cA = await cards(A)
    await voice_note(R, "demo/musa_clear.wav", "note2")
    await wait_alert_card(A, cA, 60); mark("all clear on resident")
    await narrate("cleared"); await finish("cleared", 0.3)

    # ---- 5. the forwarded rumour
    await narrate("rumour")
    base = await ai_count(A); baseR = await ai_count(R)
    await asyncio.sleep(0.5)
    await type_slowly(A, "#text-input", "A friend just forwarded this: 'There is a serious fight at Ikorodu bus stop, people are running.' Is it true?")
    await glide(A, "#send-btn"); mark("rumour sent")
    await finish("rumour", 0.2)
    await narrate("agent")
    await wait_reply(A, base, 45); mark("agent answered")
    await asyncio.sleep(1.0)
    await glide(A, "#chat .msg.ai:last-child .btn-call", click=False); mark("call button shown")
    await finish("agent", 0.3)

    # ---- 6. Musa gets the question and confirms, in Pidgin
    await narrate("musa")
    await wait_reply(R, baseR, 30); mark("escalation on responder")
    base = await ai_count(A)
    await asyncio.sleep(0.6)
    await type_slowly(R, "#text-input", "Ikorodu bus stop dey calm, I dey here now. No fight at all.")
    await glide(R, "#send-btn"); mark("responder confirmed")

    # ---- 7. Amara gets it in real time
    for _ in range(90):
        n = await A.evaluate("() => document.querySelectorAll('#chat .msg.ai .chip.st-confirmed, #chat .msg.ai .chip.st-contradicted').length")
        if n > 0: break
        await asyncio.sleep(0.5)
    mark("resident confirmed")
    await narrate("confirmed"); await finish("confirmed", 0.4)

    # ---- 8. outro + live
    await narrate("outro"); await finish("outro", 0.3)
    await narrate("live"); await finish("live", 1.0)

asyncio.run(main())
