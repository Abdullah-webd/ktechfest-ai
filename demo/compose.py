"""Compose the final MP4: responder | resident side by side on a white canvas, narration on the logged offsets."""
import json, subprocess, wave, io, glob, sys
import imageio_ffmpeg
FF = imageio_ffmpeg.get_ffmpeg_exe()
L = glob.glob("demo/rec/responder/*.webm")[0]; R = glob.glob("demo/rec/resident/*.webm")[0]
events = json.load(open("demo/rec/events.json")); narr = json.load(open("demo/narration.json"))
def dur(path):
    out = subprocess.run([FF, "-i", path], capture_output=True, text=True).stderr
    import re; m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out); return int(m[1])*3600+int(m[2])*60+float(m[3])
vidL, vidR = dur(L), dur(R)
total_events = events[-1]["t"]
# both contexts close within a fraction of a second of each other, so each video's extra length is its own head start before T0
leadL, leadR = vidL - total_events, vidR - total_events
lead = leadL; trimR = max(0.0, leadR - leadL); trimL = max(0.0, leadL - leadR)
print(f"video L {vidL:.1f}s R {vidR:.1f}s | events {total_events:.1f}s | leadL {leadL:.2f}s leadR {leadR:.2f}s -> trimL {trimL:.2f} trimR {trimR:.2f}")
# narration timeline (24 kHz mono) placed at event offsets + lead
RATE = 24000; length = int((min(vidL - trimL, vidR - trimR) + 1) * RATE); track = bytearray(length * 2)
for ev in events:
    if not ev["name"].startswith("vo:"): continue
    key = ev["name"][3:]; pcm = open(f"demo/vo_{key}.wav", "rb").read()[44:]
    start = int((ev["t"] + min(leadL, leadR)) * RATE) * 2
    end = min(start + len(pcm), len(track)); track[start:end] = pcm[:end - start]
w = wave.open("demo/rec/narration.wav", "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(bytes(track)); w.close()
out_len = min(vidL - trimL, vidR - trimR)
cmd = [FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{trimL:.3f}", "-i", L, "-ss", f"{trimR:.3f}", "-i", R, "-i", "demo/rec/narration.wav",
       "-filter_complex", "[0:v]scale=540:960[l];[1:v]scale=540:960[r];[l][r]hstack=inputs=2,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=#F7F8FA,format=yuv420p[v]",
       "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-t", f"{out_len:.2f}", "-movflags", "+faststart", "demo/saferoad-demo.mp4"]
subprocess.run(cmd, check=True)
print("written demo/saferoad-demo.mp4", f"{dur('demo/saferoad-demo.mp4'):.1f}s")
