# 🌸 AYANA — WhatsApp AI Companion for Elderly Care

> **AI-powered daily check-ins for elderly parents, delivered over WhatsApp in Indian languages.**

AYANA helps adult children who live away from their parents stay connected through automated, voice-like WhatsApp conversations. The AI checks in daily, listens for concerns, and alerts family members when something needs attention.

---

## 🎯 What It Does

- **Daily AI check-ins** — Sends personalised morning messages to elderly parents over WhatsApp
- **Multilingual support** — Conversations in Telugu, Hindi, Tamil, and other Indian languages via Sarvam AI
- **Voice-first design** — Natural, warm tone designed for elderly users unfamiliar with apps
- **Family dashboard** — Next.js web app for family members to monitor check-in history and alerts
- **Smart scheduling** — APScheduler-powered cron jobs send messages at the right local time
- **Health & mood tracking** — Tracks responses over time to surface patterns and flag concerns

---

## 🏗️ Architecture

```
┌─────────────────┐     WhatsApp      ┌──────────────────┐
│  Elderly Parent │ ◄────────────── ► │   Twilio API     │
└─────────────────┘                   └────────┬─────────┘
                                               │ Webhook
                                      ┌────────▼─────────┐
                                      │  FastAPI Backend  │
                                      │   (Railway)       │
                                      └────────┬─────────┘
                              ┌────────────────┼────────────────┐
                              │                │                │
                    ┌─────────▼──────┐ ┌───────▼──────┐ ┌──────▼───────┐
                    │  Supabase DB   │ │  Google       │ │  Sarvam AI   │
                    │  (Postgres)    │ │  Gemini AI    │ │  (Language)  │
                    └────────────────┘ └───────────────┘ └──────────────┘
                                               │
                                      ┌────────▼─────────┐
                                      │  Next.js Dashboard│
                                      │   (Vercel)        │
                                      └──────────────────┘
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Uvicorn |
| Database | Supabase (PostgreSQL) |
| Messaging | Twilio WhatsApp API |
| AI / LLM | Google Gemini (google-genai) |
| Language | Sarvam AI (Indian language translation) |
| Scheduling | APScheduler |
| Frontend | Next.js + Tailwind CSS |
| Deployment | Railway (backend) + Vercel (frontend) |
| Validation | Pydantic v2 |
| Logging | Structlog |

---

## 🚀 Getting Started

### Prerequisites

- Python 3.11+
- A [Supabase](https://supabase.com) project
- A [Twilio](https://twilio.com) account with WhatsApp sandbox or approved sender
- A [Google AI Studio](https://aistudio.google.com) API key (Gemini)
- A [Sarvam AI](https://sarvam.ai) API key
- [Railway](https://railway.app) account for deployment

### 1. Clone the repo

```bash
git clone https://github.com/GunaKanumuri/Ayana-bot.git
cd Ayana-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up environment variables

```bash
cp .env.example .env
```

Fill in your `.env`:

```env
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_anon_key
TWILIO_ACCOUNT_SID=your_twilio_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
GEMINI_API_KEY=your_gemini_api_key
SARVAM_API_KEY=your_sarvam_api_key
```

### 4. Set up the database

Run the SQL setup in your Supabase SQL editor:

```bash
# Run in Supabase SQL editor:
# supabase_complete_setup.sql   — core schema
# supabase_storage_setup.sql   — storage buckets
```

### 5. Seed test data

```bash
python seed_family.py
```

### 6. Run locally

```bash
uvicorn app.main:app --reload
```

### 7. Test the flow

```bash
python test_flow.py
```

---

## 📁 Project Structure

```
Ayana-bot/
├── app/                          # FastAPI application
│   ├── main.py                   # App entry point + routes
│   ├── scheduler.py              # APScheduler check-in jobs
│   ├── whatsapp.py               # Twilio webhook handler
│   ├── ai.py                     # Gemini + Sarvam AI integration
│   └── database.py               # Supabase client
├── supabase_complete_setup.sql   # Full DB schema
├── supabase_storage_setup.sql    # Storage bucket setup
├── seed_family.py                # Seed script for test data
├── test_flow.py                  # End-to-end flow test
├── requirements.txt              # Python dependencies
├── Procfile                      # Railway process config
├── railway.toml                  # Railway deployment config
└── .env.example                  # Environment variable template
```

---

## 🌍 Deployment

### Railway (Backend)

The repo includes `Procfile` and `railway.toml` for zero-config Railway deployment:

```bash
# Connect your GitHub repo to Railway
# Set environment variables in Railway dashboard
# Railway auto-deploys on push to main
```

### Vercel (Frontend Dashboard)

The Next.js dashboard is deployed separately on Vercel. See the [AYANA Dashboard repo](#) for setup.

---

## 💡 Use Cases

- **NRI families** — Stay connected with parents in India from abroad
- **Elder care platforms** — White-label for elder care service providers
- **Healthcare follow-ups** — Post-discharge patient check-ins
- **Insurance companies** — Wellness check-ins for senior policyholders

---

## 🗺️ Roadmap

- [ ] Voice message responses (Sarvam TTS)
- [ ] Emergency alert escalation to multiple family members
- [ ] Health metric tracking (BP, sugar reminders)
- [ ] Caregiver coordination module
- [ ] WhatsApp Business API (production-grade sender)

---

## 👤 Author

**Guna Kanumuri** — Full-Stack & AI Engineer  
MS Computer Science, Purdue University  
[LinkedIn](https://linkedin.com/in/gunakanumuri) · [Upwork](https://www.upwork.com/freelancers/gunakanumuri)

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
