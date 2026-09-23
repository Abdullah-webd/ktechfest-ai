"""Compose the final MP4: responder | resident side by side on a white canvas, narration on the logged offsets."""
import json, subprocess, wave, io, glob, sys
import imageio_ffmpeg
FF = imageio_ffmpeg.get_ffmpeg_exe()
L = glob.glob("demo/rec/responder/*.webm")[0]; R = glob.glob("demo/rec/resident/*.webm")[0]
_ev = json.load(open("demo/rec/events.json")); events = _ev["events"]; narr = json.load(open("demo/narration.json"))
def dur(path):
    out = subprocess.run([FF, "-i", path], capture_output=True, text=True).stderr
    import re; m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out); return int(m[1])*3600+int(m[2])*60+float(m[3])
vidL, vidR = dur(L), dur(R)
total_events = events[-1]["t"]
# both contexts close within a fraction of a second of each other, so each video's extra length is its own head start before T0
leadL, leadR = _ev["leadL"], _ev["leadR"]   # exact: seconds between each recording start and T0
lead = leadL; trimR = max(0.0, leadR - leadL); trimL = max(0.0, leadL - leadR)
print(f"video L {vidL:.1f}s R {vidR:.1f}s | events {total_events:.1f}s | leadL {leadL:.2f}s leadR {leadR:.2f}s -> trimL {trimL:.2f} trimR {trimR:.2f}")
# ---- dead-time cuts (video and audio): long silent waits where nothing moves on either screen
E = {e["name"]: e["t"] for e in events}
CUTS = []
gap_start = E["vo:musa"] + narr["musa"]["seconds"] + 0.6
gap_end = E["escalation on responder"] - 1.0
if gap_end - gap_start > 3.0:
    CUTS.append((gap_start, gap_end))
removed_before = lambda t: sum(b - a for a, b in CUTS if b <= t) + sum(t - a for a, b in CUTS if a < t < b)
def shift(t):  # event time -> time in the cut video
    return t - removed_before(t)
print("cuts:", [(round(a,1), round(b,1)) for a, b in CUTS], "| removed", round(sum(b-a for a,b in CUTS),1), "s")

# audio timeline (24 kHz mono): narration + the voice notes + Amara's listen, each at its logged moment
def pcm_of(path):
    w = wave.open(path); frames = w.readframes(w.getnframes())
    if w.getframerate() != 24000 or w.getnchannels() != 1: raise SystemExit(f"unexpected format in {path}")
    return frames
CLIPS = {"note1": "demo/musa_note.wav", "note2": "demo/musa_clear.wav"}
RATE = 24000; length = int((min(vidL - trimL, vidR - trimR) + 1) * RATE); track = bytearray(length * 2)
for ev in events:
    if ev["name"].startswith("vo:"):
        pcm = open(f"demo/vo_{ev['name'][3:]}.wav", "rb").read()[44:]
    elif ev["name"].startswith("clip:"):
        pcm = pcm_of(CLIPS[ev["name"][5:]])
    else:
        continue
    if any(a <= ev["t"] < b for a, b in CUTS):
        continue
    start = int((shift(ev["t"]) + min(leadL, leadR)) * RATE) * 2
    end = min(start + len(pcm), len(track)); track[start:end] = pcm[:end - start]
w = wave.open("demo/rec/narration.wav", "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(bytes(track)); w.close()
out_len = min(vidL - trimL, vidR - trimR) - sum(b - a for a, b in CUTS)
lead0 = min(leadL, leadR)
def vsel(trim):
    parts = [f"gte(t,{trim:.3f})"] + [f"not(between(t,{a + lead0 + trim:.3f},{b + lead0 + trim:.3f}))" for a, b in CUTS]
    return "fps=30,select='" + "*".join(parts) + "',setpts=N/30/TB,"   # fps=30 first: constant frame rate, so idle time is never compressed
FONT_B = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"; FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
filt = (f"[0:v]{vsel(trimL)}scale=540:960[l];[1:v]{vsel(trimR)}scale=540:960[r];[l][r]hstack=inputs=2,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#F7F8FA,"
        f"drawtext=fontfile='{FONT_B}':text='saferoad-production.up.railway.app':fontcolor=#0B1526:fontsize=30:x=(w-text_w)/2:y=22,"
        f"drawtext=fontfile='{FONT}':text='LIVE DEMO':fontcolor=#5B6B82:fontsize=20:x=(w-text_w)/2:y=h-46,"
        "format=yuv420p[v]")
cmd = [FF, "-y", "-hide_banner", "-loglevel", "error", "-i", L, "-i", R, "-i", "demo/rec/narration.wav",
       "-filter_complex", filt, "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", "30",
       "-c:a", "aac", "-b:a", "160k", "-t", f"{out_len:.2f}", "-movflags", "+faststart", "demo/saferoad-demo.mp4"]
subprocess.run(cmd, check=True)
print("written demo/saferoad-demo.mp4", f"{dur('demo/saferoad-demo.mp4'):.1f}s")
