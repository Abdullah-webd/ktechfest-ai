"""Drive two live sessions (responder left, resident right), record both, and log narration offsets."""
import asyncio, json, time, wave, sys, os
from playwright.async_api import async_playwright

BASE = "https://saferoad-production.up.railway.app"
PIN = "--host-resolver-rules=MAP saferoad-production.up.railway.app 69.46.46.55"
OUT = "demo/rec"; os.makedirs(OUT, exist_ok=True)
NARR = json.load(open("demo/narration.json"))
W, H = 540, 960
T0 = None; events = []
def mark(name):
    events.append({"name": name, "t": round(time.time() - T0, 2)}); print(f"[{events[-1]['t']:6.1f}s] {name}")
async def narrate(key):
    mark(f"vo:{key}"); return NARR[key]["seconds"]

CURSOR_JS = """
(() => {
  const c = document.createElement('div'); c.id = '__cur';
  c.style.cssText = 'position:fixed;z-index:99999;width:22px;height:22px;pointer-events:none;left:-50px;top:-50px;transform:translate(-3px,-2px);transition:left .05s linear,top .05s linear;filter:drop-shadow(0 1px 2px rgba(0,0,0,.4))';
  c.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22"><path d="M5 3l14 8.5-6.2 1.6L9.6 19z" fill="#111" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  const b = document.createElement('div'); b.id='__lab';
  b.style.cssText = 'position:fixed;z-index:99998;top:0;left:0;right:0;height:26px;background:#0B1526;color:#fff;font:600 12px/26px Inter,system-ui,sans-serif;text-align:center;letter-spacing:.04em';
  b.textContent = window.__LABEL || '';
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
        b_resp = await p.chromium.launch(args=[PIN, "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--use-file-for-fake-audio-capture=demo/musa_note.wav"])
        b_res = await p.chromium.launch(args=[PIN])
        ctxR = await b_resp.new_context(viewport={"width": W, "height": H}, device_scale_factor=2, is_mobile=True, permissions=["microphone"], record_video_dir=OUT + "/responder", record_video_size={"width": W, "height": H})
        ctxA = await b_res.new_context(viewport={"width": W, "height": H}, device_scale_factor=2, is_mobile=True, record_video_dir=OUT + "/resident", record_video_size={"width": W, "height": H})
        await ctxR.add_init_script("window.__LABEL='RESPONDER · Musa Bello · vigilante group';" + CURSOR_JS)
        await ctxA.add_init_script("window.__LABEL='RESIDENT · Amara Okafor';" + CURSOR_JS)
        R = await ctxR.new_page(); A = await ctxA.new_page()
        await R.goto(BASE + "/login", wait_until="domcontentloaded"); await A.goto(BASE + "/login", wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        T0 = time.time(); mark("start")
        try:
            await run_story(R, A)
        finally:
            mark("end")
            await ctxR.close(); await ctxA.close(); await b_resp.close(); await b_res.close()
            json.dump(events, open(f"{OUT}/events.json", "w"), indent=1)
            print("total:", events[-1]["t"], "s")

async def run_story(R, A):
        global T0

        # ---- 1. intro: log both in via the form (demo accounts for the video)
        d = await narrate("intro")
        await asyncio.sleep(1.0)
        await glide(R, "#email"); await R.fill("#email", "musa.demo@saferoad.app"); await R.fill("#password", "demo1234"); await glide(R, "button[type=submit]")
        await asyncio.sleep(0.8)
        await glide(A, "#email"); await A.fill("#email", "amara.demo@saferoad.app"); await A.fill("#password", "demo1234"); await glide(A, "button[type=submit]")
        await R.wait_for_url("**/chat", wait_until="domcontentloaded"); await A.wait_for_url("**/chat", wait_until="domcontentloaded")
        await asyncio.sleep(max(0, d - (time.time() - T0 - events[-1]["t"]) - 1.0))
        mark("both logged in")

        # ---- 2. responder voice note (fake mic plays the Hausa clip)
        d = await narrate("post")
        base = await ai_count(R); baseA = await ai_count(A)
        await glide(R, "#mic"); await asyncio.sleep(7.0); await glide(R, "#mic")
        mark("voice note sent")
        await wait_reply(R, base, 45); mark("responder readback")
        # wait for the alert card inside the readback
        for _ in range(60):
            if await R.evaluate("() => !!document.querySelector('#chat .msg.ai .alert-card')"): break
            await asyncio.sleep(0.5)
        mark("alert published")
        await asyncio.sleep(1.0)

        # ---- 3. alert arrives on the resident side
        d = await narrate("arrive")
        await wait_reply(A, baseA, 30); mark("alert on resident")
        await asyncio.sleep(3.0)
        await glide(A, ".topbar .install-btn", click=False); await asyncio.sleep(1.2)
        await glide(A, "#enable-push-m", click=False); await asyncio.sleep(0.8)
        await asyncio.sleep(max(0, d - (time.time() - T0 - events[-1]["t"]) - 7.0))

        # ---- 4. rumour
        d = await narrate("rumour")
        base = await ai_count(A)
        await type_slowly(A, "#text-input", "My cousin just sent this: 'The whole town is under attack, everyone should run.' Is it true?")
        await glide(A, "#send-btn"); mark("rumour sent")
        await asyncio.sleep(max(0, d - (time.time() - T0 - events[-1]["t"]) - 2.0))

        # ---- 5. verified
        d = await narrate("verified")
        await wait_reply(A, base, 45); mark("rumour answered")
        await asyncio.sleep(max(0, d - (time.time() - T0 - events[-1]["t"]) - 1.0))

        # ---- 6. unknown road
        d = await narrate("unknown")
        base = await ai_count(A); baseR = await ai_count(R)
        await type_slowly(A, "#text-input", "Is Ikorodu road safe to pass now?")
        await glide(A, "#send-btn"); mark("unknown sent")
        await wait_reply(A, base, 45); mark("unknown answered")
        await asyncio.sleep(max(0, d - (time.time() - T0 - events[-1]["t"]) - 1.0))

        # ---- 7. escalation reaches responder; he replies
        d = await narrate("escalate")
        await wait_reply(R, baseR, 30); mark("escalation on responder")
        await asyncio.sleep(1.5)
        base = await ai_count(A); baseR = await ai_count(R)
        await type_slowly(R, "#text-input", "Ikorodu road is clear, I just passed there now. No problem.")
        await glide(R, "#send-btn"); mark("responder reply sent")

        # ---- 8. resolve + all clear
        d = await narrate("resolve")
        await wait_reply(A, base, 45); mark("resident updated")
        await asyncio.sleep(max(0, d - (time.time() - T0 - events[-1]["t"]) + 1.0))

        # ---- 9. outro
        d = await narrate("outro")
        await asyncio.sleep(d + 0.5)

asyncio.run(main())
