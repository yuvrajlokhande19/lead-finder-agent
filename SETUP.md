# Lead Finder — Setup & Usage Guide

AI-powered prospecting tool for your web design business. Finds medium-scale
businesses (clinics, gyms, firms...) in any city, scores your chance to win
them, finds likely contact people, generates UI/UX website-design prompts and
drafts outreach emails. 100% local LLM (Ollama gemma4) — no paid AI APIs.

## One-time setup

1. **Google Places API key** (free tier, no card charge within monthly credit):
   - Go to https://console.cloud.google.com/
   - Create/select a project → **APIs & Services → Library** → search
     **"Places API (New)"** → **Enable**
   - **APIs & Services → Credentials** → **Create Credentials → API key**
   - Open the `.env` file in this folder and paste it:
     `GOOGLE_PLACES_API_KEY=your_real_key`
2. **Ollama** must be running (it usually auto-starts on login). Verify:
   ```
   ollama list        → gemma4:latest should appear
   ```

## Daily use

```
cd C:\Users\lokha\website-lead-agent
uv run python server.py
```

Open **http://127.0.0.1:8080** in your browser.

### Workflow

1. **Search**: enter business type + city/landmark → leads saved with an
   automatic **Chance score** (no-website +25, strong reviews, Tier 2/3 bonus).
2. **Pitch Suggestions panel**: shows how many businesses you can pitch +
   top-ranked cards. Click any card for full data.
3. **Analyze** a lead (button in the table): runs DuckDuckGo enrichment for the
   owner/contact person, then gemma4 writes the **UI/UX design prompt**,
   **outreach email draft**, and an **AI pitch assessment**. Takes ~1–3 min on
   local GPU/CPU.
4. Click a row anytime → detail modal with contact info, web findings,
   copy-ready prompt & email.

### Sorting / filtering

- Sort by **Chance to win**, rating, reviews or name.
- Filter by **service type** dropdown (populated from your saved leads).

## Troubleshooting

| Problem | Fix |
|---|---|
| "Places API key missing" banner | Paste key into `.env`, restart server |
| Analyze fails | Is Ollama running? Run `ollama serve`, keep terminal open |
| Slow analysis | Normal on CPU; first call also loads the model into memory |
| No results for a search | Try broader location ("Indore" vs a small locality) |

## Optional: chat with the agent directly

The ADK agent is also available as a chatbot:

```
uv run agents-cli playground     (or: uv run adk web)
```

Ask it things like *"find dental clinics in Jaipur"* or *"analyze lead 3"*.
