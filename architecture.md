# Architecture — Community Safety Agent on WhatsApp

Working name: **SafeRoad** (rename freely).

One WhatsApp number. Two kinds of people talk to it: **responders** (the local vigilante / community security group) and **residents** (Amara). Responders drop what they know, in any language, by voice or text. The agent structures it, broadcasts it to residents in their own language, and answers "is this true?" questions using only what responders have actually said, with a clear confidence state and a timestamp.

---

## 1. What the problem statement is really saying

- "The local vigilante group has a radio but no way to reach everyone quickly" — in most Nigerian towns the vigilante/hunters/Civilian JTF group carries handheld two‑way radios (walkie‑talkies). They can talk **to each other** instantly, but that channel is closed: residents are not on it. So the group often knows what is happening on the road before anyone else and has no fast way to tell 2,000 people. That is the gap we fill: the agent is the bridge from the radio circle to the town.
- "Too much noise, too little verified signal" — the fix is not more information, it is **provenance**. Every answer must say *who* said it and *when*.
- "She needs to trust what she's told" — the agent must never say "true" or "false" on its own authority. It says *confirmed by a responder at 6:31 PM*, *reported by residents but not yet confirmed*, or *no responder has reported this*.

## 2. Corrections to the original plan

| Original idea | Problem | What we do instead |
|---|---|---|
| Two Twilio sandbox numbers, one per role | Twilio gives **one** sandbox per account, and it is the same global number (+1 415 523 8886) with a per‑account `join <word>` phrase. A phone can only be joined to one sandbox at a time, so two accounts would collide on real phones. | One number. Role is decided at onboarding: responders enter a one‑time **invite code** shown on the responder page of the website. Everyone else is a resident. In production this becomes two real WhatsApp Business senders. |
| Ask the vigilante for their phone number | We already have it — WhatsApp gives us the sender number on every message. | Onboarding asks for **name** and **photo** only. Phone is captured automatically. |
| Agent gives residents a vigilante's phone number when it has no answer | Leaks responders' personal numbers to the whole town; some will refuse to join. | Agent **escalates**: forwards the question to all responders ("A resident is asking about X near Y, any info?") and tells the resident it has asked. When a responder answers, the agent replies to the resident and records it as a new alert. A responder can *opt in* to a public hotline number if the group wants one. |
| "The vigilante knows the truth" | They know more than anyone else, but they can be wrong, late, or absent. | Responders are the **highest‑trust source**, not ground truth. Alerts carry the responder's name, time, and an expiry. Multiple resident reports of the same thing get clustered and pushed to responders for confirmation. |
| Gemini Live API for voice | Live is for real‑time streaming audio calls. WhatsApp voice notes are files. | Gemini 2.5 Flash reads the audio file directly (transcribe + translate + understand in one call). Gemini TTS produces the voice reply. |
| Everything works live on the sandbox | Sandbox has a **24‑hour session window**: we can only send a free‑form message to someone who has messaged us in the last 24 h. Broadcasts to people outside the window silently fail. Sandbox also unjoins phones after 72 h of inactivity. | For the demo, every phone messages the bot at the start of the day. We log delivery status per recipient. Production uses an approved alert **template** which has no window. |
| PWA install button works everywhere | Chrome/Android show the native install prompt. iOS Safari never fires it; the user has to tap Share → Add to Home Screen. | Install button on Android/desktop Chrome; on iOS show a small "Add to Home Screen" hint instead. |

## 3. System overview

```mermaid
flowchart LR
    R[Responder phone\nWhatsApp voice/text/photo] -->|inbound| TW[Twilio WhatsApp Sandbox]
    U[Resident phone\nWhatsApp voice/text] -->|inbound| TW
    TW -->|webhook POST| API[FastAPI app\n/webhooks/twilio/whatsapp]
    API -->|typing indicator| TW
    API --> Q[Background task queue]
    Q --> AI[Gemini layer\nunderstand · verify · render]
    AI --> DB[(SQLite → Postgres)]
    Q -->|REST send| TW
    TW -->|broadcast in each language| U
    TW -->|escalations| R
    WEB[Landing page + PWA\nQR codes, install button] --> API
    ADMIN[/admin feed] --> API
```

Single Python process (FastAPI) serves the website, the PWA assets, the Twilio webhook, and a small admin feed. Background work runs in‑process for the hackathon (FastAPI `BackgroundTasks`), swappable for a real queue later.

## 4. Components

### 4.1 Web (FastAPI + Jinja2 + Tailwind CDN)
- `/` — landing page: the story, how it works, two big buttons: **I live here** / **I am a responder**.
- `/join/resident` — QR code + button. Both open `https://wa.me/14155238886?text=join%20<sandbox-word>`. Instructions: "send the join message, then say hello in your language".
- `/join/responder` — same QR/button plus a generated **invite code** (e.g. `RESPONDER-7K2Q`). Page is behind a simple shared passphrase for the demo.
- `/admin` — live feed of alerts, escalations, users, broadcast delivery status. This is what you show the judges on the projector while phones do the talking.
- PWA: `manifest.webmanifest`, `sw.js` (cache shell), install button via `beforeinstallprompt`, iOS hint.
- Everything mobile‑first.

### 4.2 Webhook (`POST /webhooks/twilio/whatsapp`)
Twilio needs a response within ~15 s, and AI work can take longer. So:
1. Validate the Twilio signature.
2. Persist the raw inbound (From, Body, MediaUrl0, MediaContentType0, MessageSid).
3. Fire the **typing indicator** for `MessageSid` (POST `https://messaging.twilio.com/v3/Indicators/Typing.json`, `{channel:"whatsapp", messageId}`). Re‑fire at 20 s if still working.
4. Return empty TwiML immediately.
5. Background task runs the pipeline and sends the reply via the Twilio REST API.

### 4.3 Conversation router (per user state machine)
```
unknown number
  ├─ body matches unused invite code → role=responder, state=onboard_name
  └─ anything else                   → role=resident,  state=onboard_name
onboard_name  → ask name (text or voice, any language) → state=onboard_photo
onboard_photo → ask for a photo (MediaContentType image/*) → state=active
active
  ├─ responder: every message = REPORT (unless intent=question/chat)
  └─ resident:  intent ∈ {question, report, chat}
```
Language is detected from the first message and stored; every reply goes out in that language. User can switch by just writing in another language.

### 4.4 AI layer (Gemini, one API key)
Three narrow calls, each with a strict JSON schema:

**understand(message)** — input: text, or audio bytes, or image + caption; plus user role and last few turns.
Output:
```json
{
  "transcript": "...", "language": "yo", "intent": "report|question|onboarding_answer|chat",
  "english_summary": "...", "location": "Kaduna road, near the market", "category": "robbery|fight|roadblock|fire|movement|all_clear|other",
  "severity": "info|caution|danger", "time_reference": "now"
}
```
Model: `gemini-2.5-flash` (native audio + image input, fast, cheap).

**verify(question, candidate_alerts)** — input: the resident's structured question plus the alerts from the last N hours that match location/category (simple SQL filter first, then let the model rank). Output:
```json
{ "status": "confirmed|contradicted|unconfirmed_reports|no_information",
  "matched_alert_ids": [...], "answer_en": "...", "escalate": true|false }
```
The prompt forbids inventing facts: the model may only cite alerts it was given.

**render(answer_en, language, want_voice)** — translate the answer into the user's language; if the user's last message was a voice note, also generate a voice reply with `gemini-2.5-flash-preview-tts`, convert PCM → OGG/Opus (or MP3) with ffmpeg, host it under `/media/<id>` so Twilio can fetch it, and send as `MediaUrl`.

### 4.5 Alert pipeline (responder sends a report)
1. understand → structured alert.
2. Save alert with `expires_at` (default 6 h; `all_clear` closes matching open alerts).
3. Reply to responder with the structured readback in their language: "Recorded: fight on Kaduna road near the market, danger, 6:31 PM. Sent to 143 residents. Reply 'wrong' to fix."
4. Broadcast: group residents by language, translate once per language, send to each, record delivery status (`queued/sent/delivered/failed`). Message format:
   > ⚠️ **Alert · 6:31 PM** — Fight at the bottom of Kaduna road near the market. Avoid the area. *Confirmed by responder Musa.* Ask me "is Kaduna road safe?" any time.

### 4.6 Question pipeline (resident asks)
1. understand → question with location/category.
2. Fetch open alerts (not expired) in the last 24 h; verify.
3. Reply in the user's language with one of four shapes, always with a time:
   - **confirmed**: "Yes — responder Musa confirmed a fight at Kaduna road 12 minutes ago. Avoid it."
   - **contradicted**: "A responder reported the road clear 8 minutes ago. The message you received is not confirmed."
   - **unconfirmed_reports**: "3 residents reported this in the last 20 minutes, no responder has confirmed. I've asked them; I'll message you when they answer."
   - **no_information**: "No responder has reported anything about Kaduna road today. I've asked them. Stay careful until you hear back."
4. If `escalate`: create an escalation, message all responders with the question. First responder reply becomes an alert and the asker gets the answer.

### 4.7 Resident report pipeline
Residents can also report ("I see people gathering at the junction"). Stored as `source=resident, status=unconfirmed`. Never broadcast on its own. When ≥ 3 unconfirmed reports cluster on the same location+category within 30 minutes, push one message to responders: "3 residents reported X near Y. Confirm?" A responder reply of "yes/confirm" promotes it to a confirmed alert and broadcasts; "no/false" marks it contradicted and the reporters get told.

## 5. Data model (SQLModel; SQLite for the hackathon, Postgres later)

- **users**: id, phone (unique), role (`resident|responder`), name, photo_url, language, state, created_at, last_inbound_at
- **invite_codes**: code, used_by, created_at
- **messages**: id, user_id, direction, message_sid, body, media_url, media_type, transcript, language, intent, created_at
- **alerts**: id, source (`responder|resident|escalation`), author_id, category, severity, location, summary_en, raw_transcript, language, status (`open|closed|contradicted|unconfirmed`), created_at, expires_at, confirmed_by, confirmed_at
- **broadcasts**: id, alert_id, user_id, message_sid, status, error, sent_at
- **questions**: id, user_id, text_en, location, category, status, matched_alert_ids, answer_en, escalated, answered_at
- **escalations**: id, question_id, sent_to (responder ids), resolved_by, resolved_alert_id, created_at

## 6. Twilio sandbox specifics

- Number: `+1 415 523 8886`. Join phrase from Console → Messaging → Try it out → WhatsApp.
- Set **"When a message comes in"** to `https://<ngrok-host>/webhooks/twilio/whatsapp` (POST). Set the status callback to `/webhooks/twilio/status` to track delivery.
- Inbound media arrives as `MediaUrl0` (audio is `audio/ogg`, images `image/jpeg`). Fetch with HTTP Basic auth (Account SID / Auth Token).
- Outbound media: give Twilio a public URL (our `/media/...` behind ngrok). Audio goes out as an audio attachment, not a native "voice note" bubble — that is a WhatsApp/Twilio limitation.
- Typing indicator: Public Beta, needs an **API Key SID + Secret** (not the auth token). Indicator lasts ≤ 25 s.
- 24‑hour window and 72‑hour auto‑unjoin, as above.
- Rate: sandbox is fine for a demo of ~20 phones. Broadcasts are sent concurrently with a small semaphore.

## 7. Repo layout

```
ktechfest-ai/
  app/
    main.py              # FastAPI app, routers, static, templates
    config.py            # settings from .env
    db.py                # engine, session, models
    twilio_client.py     # send, typing indicator, media fetch, signature check
    gemini.py            # understand / verify / render / tts
    router.py            # per-user state machine
    pipelines/
      alerts.py          # report → alert → broadcast
      questions.py       # question → verify → answer/escalate
      reports.py         # resident reports + clustering
      onboarding.py
    web/
      templates/         # landing, join pages, admin
      static/            # css, js, manifest, sw.js, icons
  media/                 # generated TTS files (gitignored)
  tests/
  architecture.md
  README.md
  .env.example
  requirements.txt
```

## 8. Keys and accounts to gather

| Item | Where | Env var |
|---|---|---|
| Twilio Account SID | Console home | `TWILIO_ACCOUNT_SID` |
| Twilio Auth Token | Console home | `TWILIO_AUTH_TOKEN` |
| Twilio API Key SID + Secret | Console → Account → API keys & tokens → Create (Standard) | `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET` |
| Sandbox number | Messaging → Try it out → Send a WhatsApp message | `TWILIO_WHATSAPP_FROM=whatsapp:+14155238886` |
| Sandbox join phrase | same page ("join xxx-yyy") | `TWILIO_SANDBOX_JOIN_WORD` |
| Gemini API key | aistudio.google.com → Get API key | `GEMINI_API_KEY` |
| ngrok authtoken | dashboard.ngrok.com | used by `ngrok config add-authtoken` |
| Responder page passphrase | you choose | `RESPONDER_PAGE_PASSPHRASE` |
| App secret | random | `APP_SECRET` |

Local tools needed on the Mac: **Python 3.11+** (system Python is 3.9 — install via `brew install python@3.12` or `uv`), **ffmpeg** (`brew install ffmpeg`), **ngrok** (`brew install ngrok`).

## 9. Build order

1. **Skeleton + webhook echo** — FastAPI, ngrok, sandbox joined, bot echoes text. Typing indicator working.
2. **Understand** — voice note in Yoruba/Hausa/Igbo → transcript + language + intent, reply in same language. Image receipt.
3. **Onboarding** — invite code, name, photo, roles.
4. **Alerts + broadcast** — responder report → structured alert → translated broadcast to all residents. Admin feed shows it live.
5. **Questions + verify** — the four answer states with timestamps. Escalation to responders and the loop back.
6. **Voice replies** — TTS in user's language when they spoke.
7. **Landing page + PWA + QR codes**.
8. **Resident reports + clustering** (if time).
9. Demo rehearsal with 3–4 phones.

## 10. Demo script (what the judges see)

1. Projector shows `/admin`, empty feed. Phone A (resident, Yoruba), Phone B (responder, Hausa), Phone C (resident, English).
2. Phone A asks in Yoruba voice: "Is Kaduna road safe?" → typing… → "No responder has reported anything about Kaduna road today. I've asked them."
3. Phone B receives the escalation, replies with a Hausa voice note: "There is a fight near the market, people should avoid it."
4. Feed shows the alert. Phone A gets the confirmed answer in Yoruba, Phone C gets the broadcast in English, both within seconds.
5. Phone C forwards a rumour: "They say the whole town is being attacked, true?" → "A responder confirmed a fight near the market 2 minutes ago. Nothing about an attack on the town. Avoid the market area."
6. Phone B sends "all clear" → residents get the update.

## 11. Production path (say this in the pitch)

WhatsApp Business API with two verified senders (responders / residents), approved alert templates so broadcasts work outside the 24‑hour window, Postgres + a real queue (Redis/RQ), responder verification through the local government or traditional ruler's office, SMS fallback for feature phones, and per‑ward geofencing so people only get alerts for their area.
