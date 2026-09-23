"""Generate narration clips (Spitch: John = responder side, Lucy = resident side) and the responder's Hausa voice note."""
import io, json, wave, sys
from spitch import Spitch
sys.path.insert(0, ".")
from app.config import settings
c = Spitch(api_key=settings.spitch_api_key)

LINES = [
 ("intro",    "john", "This is SafeRoad, an AI agent for community safety. It has two doors. On the left, a responder from the local vigilante group. On the right, a resident, Amara. Both are live right now, with real accounts."),
 ("post",     "john", "Musa, the responder, sees a fight at the market. He simply holds the mic and speaks Hausa. The agent transcribes it, works out what happened and where, structures it into an alert, and publishes it."),
 ("arrive",   "lucy", "On Amara's side the alert has already arrived, translated into her own language, with the responder's name and the exact time. If she has installed SafeRoad, her phone buzzes even when the app is closed. That is why it installs from the browser in one tap."),
 ("rumour",   "lucy", "Now the real problem. A cousin forwards a message: the whole town is under attack. Amara does not know if it is true. She just forwards it to the agent."),
 ("verified", "lucy", "The agent checks it against what responders have actually confirmed. There is a fight at the market, confirmed by Musa minutes ago. Nothing about an attack on the town. It says so, and it names its source."),
 ("unknown",  "lucy", "She asks about a road nobody has reported. The agent will not guess. It says no responder has reported anything, escalates the question, and offers a direct call to the responder on duty."),
 ("escalate", "john", "Musa's chat lights up with her question in real time. He replies from the road, in his own words."),
 ("resolve",  "john", "And Amara is told automatically, with his name and the time. Finally Musa reports that the market has calmed down. The alert closes for everyone, in every language."),
 ("outro",    "lucy", "Too much noise, too little verified signal, and no time. SafeRoad turns responders' words into verified answers for a whole town, in seconds, in your language. SafeRoad."),
]

def fix_wav(b: bytes) -> bytes:
    """Spitch streams WAV with a placeholder length; rewrite the header with the real PCM size."""
    pcm = b[44:]
    out = io.BytesIO(); w = wave.open(out, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm); w.close()
    return out.getvalue()

meta = {}
for key, voice, text in LINES:
    b = fix_wav(c.speech.generate(text=text, language="en", voice=voice, format="wav").read())
    open(f"demo/vo_{key}.wav", "wb").write(b)
    dur = len(b[44:]) / (24000 * 2)
    meta[key] = {"voice": voice, "seconds": round(dur, 2), "text": text}
    print(f"{key:9} {voice:5} {dur:5.1f}s")
print("total narration:", round(sum(m["seconds"] for m in meta.values()), 1), "s")
# responder's Hausa voice note for the fake microphone (16 kHz mono WAV, which the recorder path also produces)
hausa = fix_wav(c.speech.generate(text="Akwai faɗa a kasuwa kusa da hanyar Kaduna. Mutane su guji wurin yanzu, har sai mun sanar.", language="ha", voice="hasan", format="wav").read())
open("demo/musa_note.wav", "wb").write(hausa)
print("hausa note:", round(len(hausa[44:])/(24000*2),1), "s")
json.dump(meta, open("demo/narration.json", "w"), indent=1)
