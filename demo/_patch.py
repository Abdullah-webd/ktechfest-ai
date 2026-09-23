import sys; sys.path.insert(0, "demo"); import _dns
import io, json, wave, ast
sys.path.insert(0, ".")
from spitch import Spitch
from app.config import settings
c = Spitch(api_key=settings.spitch_api_key)
def fix_wav(b):
    pcm=b[44:]; out=io.BytesIO(); w=wave.open(out,"wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm); w.close(); return out.getvalue()
meta=json.load(open("demo/narration.json"))
text="As you can see, it has dropped on Amara's side, in her own language, with Musa's name and the time. And if she taps Listen, the agent reads it out to her."
b=fix_wav(c.speech.generate(text=text, language="en", voice="lucy", format="wav").read()); open("demo/vo_dropped.wav","wb").write(b)
meta["dropped"]={"voice":"lucy","seconds":round(len(b[44:])/48000,2),"text":text}; json.dump(meta,open("demo/narration.json","w"),indent=1)
print("dropped:", meta["dropped"]["seconds"], "s")

def sub(path, pairs):
    s = open(path).read()
    for old, new in pairs:
        assert old in s, f"missing in {path}: {old[:60]!r}"
        s = s.replace(old, new)
    open(path, "w").write(s)

R = open("demo/record.py").read()
i0 = R.index("    # ---- 3. dropped on Amara's side"); i1 = R.index('    await narrate("nopanic")')
R = R[:i0] + (
'    # ---- 3. dropped on Amara\'s side (the cursor points at Listen; no click, no playback)\n'
'    await narrate("dropped")\n'
'    await asyncio.sleep(1.0)\n'
'    await glide(A, "#chat .msg.ai:last-child .msg-actions .btn-ghost", click=False); mark("points at listen")\n'
'    await finish("dropped", 0.3)\n'
) + R[i1:]
open("demo/record.py", "w").write(R)
sub("demo/record.py", [
    ('        R = await ctxR.new_page(); A = await ctxA.new_page()',
     '        t_pageR = time.time(); R = await ctxR.new_page()\n        t_pageA = time.time(); A = await ctxA.new_page()'),
    ('        T0 = time.time(); mark("start")',
     '        T0 = time.time(); mark("start")\n        meta_lead = {"leadL": T0 - t_pageR, "leadR": T0 - t_pageA}'),
    ('            json.dump(events, open(f"{OUT}/events.json", "w"), indent=1)',
     '            json.dump({"events": events, **meta_lead}, open(f"{OUT}/events.json", "w"), indent=1)'),
])
sub("demo/compose.py", [
    ('events = json.load(open("demo/rec/events.json")); narr = json.load(open("demo/narration.json"))',
     '_ev = json.load(open("demo/rec/events.json")); events = _ev["events"]; narr = json.load(open("demo/narration.json"))'),
    ('leadL, leadR = vidL - total_events, vidR - total_events\nlead = leadL; trimR = max(0.0, leadR - leadL); trimL = max(0.0, leadL - leadR)',
     'leadL, leadR = _ev["leadL"], _ev["leadR"]   # exact: seconds between each recording start and T0\nlead = leadL; trimR = max(0.0, leadR - leadL); trimL = max(0.0, leadL - leadR)'),
    ("sel = \"*\".join(f\"not(between(t,{a + lead0:.3f},{b + lead0:.3f}))\" for a, b in CUTS) or \"1\"\nvcut = f\"select='{sel}',setpts=N/30/TB,\"",
     "def vsel(trim):\n    parts = [f\"gte(t,{trim:.3f})\"] + [f\"not(between(t,{a + lead0 + trim:.3f},{b + lead0 + trim:.3f}))\" for a, b in CUTS]\n    return \"select='\" + \"*\".join(parts) + \"',setpts=N/30/TB,\""),
    ('filt = (f"[0:v]{vcut}scale=540:960[l];[1:v]{vcut}scale=540:960[r];[l][r]hstack=inputs=2,"',
     'filt = (f"[0:v]{vsel(trimL)}scale=540:960[l];[1:v]{vsel(trimR)}scale=540:960[r];[l][r]hstack=inputs=2,"'),
    ('cmd = [FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{trimL:.3f}", "-i", L, "-ss", f"{trimR:.3f}", "-i", R, "-i", "demo/rec/narration.wav",',
     'cmd = [FF, "-y", "-hide_banner", "-loglevel", "error", "-i", L, "-i", R, "-i", "demo/rec/narration.wav",'),
    ("text='LIVE DEMO  ·  real accounts, real AI agent, no edits'", "text='LIVE DEMO'"),
    ('CLIPS = {"note1": "demo/musa_note.wav", "note2": "demo/musa_clear.wav", "listen": "demo/rec/listen.wav"}',
     'CLIPS = {"note1": "demo/musa_note.wav", "note2": "demo/musa_clear.wav"}'),
])
ast.parse(open("demo/record.py").read()); ast.parse(open("demo/compose.py").read()); print("recorder + composer updated")
