# SafeRoad — verified community safety alerts on WhatsApp

Built for the KTechFest AI hackathon. See [architecture.md](architecture.md) for the full design.

**Stack:** Python 3.11+, FastAPI + Jinja2 + Tailwind, SQLModel/SQLite, Google Gemini (2.5 Flash for understanding + verification, Flash TTS for voice replies), Web Push (pywebpush/VAPID), Resend for email, installable PWA.

## Local setup
```bash
brew install python@3.12 ngrok
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
uvicorn app.main:app --reload --port 8000
ngrok http 8000        # open the https URL on your phone to test install + push
```
