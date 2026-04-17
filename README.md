# Tech News Agent (MVP)

A simple AI-assisted web news dashboard for your interests, with optional chat.

## Features
- Add interests and RSS sources
- Ingest latest articles from RSS feeds
- Auto-generate summary, why-it-matters insight, and a discussion question
- View a simple dashboard
- Chat endpoint grounded on recent ingested items

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open:
- Dashboard: `http://127.0.0.1:8000/`
- API docs: `http://127.0.0.1:8000/docs`

## Notes
- Uses SQLite by default (`news_agent.db`).
- If `OPENAI_API_KEY` is set, OpenAI is used for richer insights/chat.
- Without OpenAI key, deterministic local fallback logic is used.
