# Lead Finder

**Find local businesses that need a website — with AI-generated design prompts and outreach emails ready to send.**

A self-hosted lead-generation dashboard for web designers and agencies. Search any city or landmark for clinics, gyms, firms, shops — the app scores each business's *chance-to-win*, enriches it with web research for contact people, and uses a **fully local LLM** (Ollama) to write a UI/UX design brief and a personalized pitch email for every lead.

![Python](https://img.shields.io/badge/python-3.11%2B-blue) ![LLM](https://img.shields.io/badge/LLM-Ollama%20gemma4-purple) ![License](https://img.shields.io/badge/license-MIT-green)

---

## Why this exists

Small businesses in Tier 2/3 cities often have zero web presence — they're the perfect clients for web designers, but finding them and preparing personalized pitches is slow manual work. Lead Finder automates the entire pipeline:

```
Search "dental clinic in Jaipur"
        │
        ▼
┌─────────────────────────────────────────────────────┐
│ 1. DISCOVER   Google Places API (or free OpenStreetMap fallback)
│ 2. SCORE      Chance-to-win 0–100 (no website, reviews, city tier)
│ 3. ENRICH     DuckDuckGo research → owner / decision-maker clues
│ 4. GENERATE   Local gemma4 writes: UI/UX prompt · outreach email · pitch assessment
│ 5. ACT        Copy the email, build the demo from the prompt, hit send
└─────────────────────────────────────────────────────┘
```

## Features

| Feature | Description |
|---|---|
| 🔍 Flexible search | Any business type × any city / landmark / locality |
| 🎯 Chance scoring | Rule-based 0–100 score: missing website +25, review volume, rating strength, phone availability, **Tier 2/3 location bonus** |
| 📊 Pitch suggestions | *"You can pitch N businesses"* summary + top-ranked suggestion cards |
| 🕵️ Contact enrichment | DuckDuckGo finds owner/founder/director clues per lead |
| 🎨 UI/UX prompt generator | Complete design brief (palette hex codes, typography, page structure) you can feed into any AI builder |
| ✉️ Email drafting | Short, personalized B2B outreach emails tailored to each business type |
| 🗂️ Sort & filter | By chance score, service type, rating, reviews, name |
| 💯 Private LLM | All AI text runs on your machine via Ollama — no AI subscriptions |

## Quick Start

### Prerequisites

| Requirement | Notes |
|---|---|
| [Python 3.11+](https://www.python.org/downloads/) | |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Python package manager |
| [Ollama](https://ollama.com) | Running locally |
| `gemma4` model pulled | `ollama pull gemma4` |

### Install (Windows / Linux)

```bash
git clone https://github.com/YOUR_USERNAME/lead-finder-agent.git
cd lead-finder-agent
uv sync
```

### Configure (optional but recommended)

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

The app **works immediately without any API key** using free OpenStreetMap search.
For richer data (phone numbers, ratings, more results), add a free **Google Places API** key:

1. Open [Google Cloud Console](https://console.cloud.google.com/) → create/select a project
2. **APIs & Services → Library** → enable **"Places API (New)"**
3. **Credentials → Create Credentials → API key**
4. Paste into `.env`:

```ini
GOOGLE_PLACES_API_KEY=your_real_key_here
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma4
```

### Run

```bash
uv run python server.py
```

Open **http://127.0.0.1:8080** 🎉

> First analysis takes 1–3 minutes while gemma4 loads into memory — be patient.

## Using the Dashboard

1. **Search** — enter a business type (`dental clinic`, `gym`, `cafe`) + location (`Jaipur`, `Indore`, `Andheri West`)
2. **Review scores** — each lead gets a color-coded chance badge:
   - 🟢 **70–100 High** — pitch these first
   - 🟡 **45–69 Medium**
   - ⚪ **<45 Low**
3. **Pitch Suggestions panel** — see how many businesses you can pitch overall + top cards ranked by opportunity
4. **Click a lead → Analyze** — runs web enrichment + generates the UI/UX prompt, email draft, and pitch assessment
5. **Copy & act** — copy the email into Gmail, paste the prompt into your favorite AI site-builder to mock up a demo

### How the score works

| Signal | Points |
|---|---|
| No website found | +25 |
| Has website (redesign pitch only) | +5 |
| ≥200 reviews | +15 |
| ≥50 reviews | +8 |
| Rating ≥ 4.3 | +10 |
| Rating ≥ 3.5 | +5 |
| Phone listed | +5 |
| Tier 2/3 city (non-metro) | +10 |
| Baseline | 30 |

## Optional: Chat Mode (ADK Agent)

This project is built on [Google's ADK](https://adk.dev), so you can also drive the same pipeline conversationally:

```bash
uv run adk web
# or: uv run agents-cli playground
```

Try: *"find dental clinics in Jaipur"* → *"analyze lead 3"*

## API Reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/search` | POST | `{business_type, location, max_results}` → discovers & scores leads |
| `/api/leads` | GET | List leads (`?sort=score\|rating\|reviews\|name&service=Dentist`) |
| `/api/services` | GET | Distinct business types (for filters) |
| `/api/suggestions` | GET | Pitch summary + top suggestions (`?min_score=45&limit=8`) |
| `/api/leads/{id}` | GET | Full lead detail incl. generated assets |
| `/api/leads/{id}/analyze` | POST | Enrich + generate prompt/email/pitch-note |
| `/api/leads/{id}` | DELETE | Remove a lead |

## Project Structure

```
lead-finder-agent/
├── server.py            # FastAPI backend + JSON API
├── static/index.html    # Single-file dashboard UI
├── app/
│   ├── agent.py         # ADK agent (chat mode, LiteLLM→Ollama)
│   └── tools.py         # Search, scoring, enrichment, generation, SQLite
├── leads.db             # Your saved leads (created at runtime, gitignored)
├── .env                 # Secrets (gitignored)
└── tests/
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Search returns few results | You're on the free OpenStreetMap fallback — add a Places key for full coverage |
| Analyze fails / hangs | Ensure Ollama is running (`ollama serve`) and gemma4 is pulled |
| Port 8080 busy | Kill stale process or change port at bottom of `server.py` |
| Garbled text in console logs | Cosmetic Windows cp1252 issue only; the web app handles Unicode fine |

## Disclaimer

Use responsibly: respect Google/OSM terms of service, don't spam businesses, follow your local anti-spam laws (e.g., CAN-SPAM, GDPR). This tool drafts outreach — you own what you send.

## License

MIT
