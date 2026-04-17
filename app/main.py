import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import feedparser
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.getenv("NEWS_AGENT_DB", "news_agent.db")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

app = FastAPI(title="Tech News Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS interests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                rss_url TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                published_at TEXT,
                content TEXT,
                created_at TEXT NOT NULL,
                hash TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES sources(id)
            );
            CREATE TABLE IF NOT EXISTS insights (
                article_id INTEGER PRIMARY KEY,
                summary TEXT NOT NULL,
                why_it_matters TEXT NOT NULL,
                question TEXT NOT NULL,
                relevance_score REAL NOT NULL,
                FOREIGN KEY(article_id) REFERENCES articles(id)
            );
            """
        )


class InterestIn(BaseModel):
    name: str


class SourceIn(BaseModel):
    name: str
    rss_url: str


class ChatIn(BaseModel):
    message: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def simple_relevance(text: str, interests: list[str]) -> float:
    lower = text.lower()
    if not interests:
        return 0.25
    matches = sum(1 for i in interests if i.lower() in lower)
    return min(1.0, 0.15 + (matches / max(len(interests), 1)))


def local_insight(title: str, content: str, interests: list[str]) -> dict[str, Any]:
    text = (content or "").strip()
    words = text.split()
    summary = " ".join(words[:36]) if words else title
    if len(words) > 36:
        summary += "..."
    focus = ", ".join(interests[:3]) if interests else "your tech priorities"
    why = f"This may impact {focus} by signaling near-term direction and potential second-order effects."
    question = f"If this trend accelerates over 12 months, what changes should you make first?"
    score = simple_relevance(f"{title} {content}", interests)
    return {
        "summary": summary,
        "why_it_matters": why,
        "question": question,
        "relevance_score": score,
    }


def ai_insight(title: str, content: str, interests: list[str]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return local_insight(title, content, interests)
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        prompt = {
            "title": title,
            "content": content[:5000],
            "interests": interests,
            "task": "Return JSON with summary, why_it_matters, question, relevance_score(0..1). Keep concise.",
        }
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=[{"role": "user", "content": json.dumps(prompt)}],
            temperature=0.2,
        )
        text = resp.output_text
        parsed = json.loads(text)
        for key in ["summary", "why_it_matters", "question", "relevance_score"]:
            if key not in parsed:
                raise ValueError(f"Missing key {key}")
        parsed["relevance_score"] = float(parsed["relevance_score"])
        return parsed
    except Exception:
        return local_insight(title, content, interests)


def all_interest_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM interests ORDER BY id ASC").fetchall()
    return [r["name"] for r in rows]


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/interests")
def create_interest(payload: InterestIn):
    with db_conn() as conn:
        try:
            cur = conn.execute("INSERT INTO interests(name) VALUES (?)", (payload.name.strip(),))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Interest already exists")
        return {"id": cur.lastrowid, "name": payload.name.strip()}


@app.get("/interests")
def list_interests():
    with db_conn() as conn:
        rows = conn.execute("SELECT id, name FROM interests ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


@app.post("/sources")
def create_source(payload: SourceIn):
    with db_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO sources(name, rss_url) VALUES (?, ?)",
                (payload.name.strip(), payload.rss_url.strip()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Source already exists")
        return {"id": cur.lastrowid, "name": payload.name.strip(), "rss_url": payload.rss_url.strip()}


@app.get("/sources")
def list_sources():
    with db_conn() as conn:
        rows = conn.execute("SELECT id, name, rss_url FROM sources ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


@app.post("/ingest/run")
def run_ingest(limit_per_source: int = 15):
    added = 0
    with db_conn() as conn:
        interests = all_interest_names(conn)
        sources = conn.execute("SELECT id, name, rss_url FROM sources ORDER BY id ASC").fetchall()
        for source in sources:
            feed = feedparser.parse(source["rss_url"])
            entries = (feed.entries or [])[:limit_per_source]
            for e in entries:
                url = (e.get("link") or "").strip()
                title = (e.get("title") or "Untitled").strip()
                content = ""
                if e.get("summary"):
                    content = str(e.get("summary"))
                elif e.get("description"):
                    content = str(e.get("description"))
                if not url:
                    continue
                h = hashlib.sha256(f"{url}::{title}".encode()).hexdigest()
                existing = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
                if existing:
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO articles(source_id, url, title, published_at, content, created_at, hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source["id"],
                        url,
                        title,
                        e.get("published", ""),
                        content,
                        now_iso(),
                        h,
                    ),
                )
                article_id = cur.lastrowid
                insight = ai_insight(title, content, interests)
                conn.execute(
                    """
                    INSERT INTO insights(article_id, summary, why_it_matters, question, relevance_score)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        article_id,
                        insight["summary"],
                        insight["why_it_matters"],
                        insight["question"],
                        insight["relevance_score"],
                    ),
                )
                added += 1
    return {"status": "ok", "added": added}


@app.get("/articles")
def list_articles(limit: int = 50, min_relevance: float = 0.0):
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.url, a.published_at, s.name AS source,
                   i.summary, i.why_it_matters, i.question, i.relevance_score
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            JOIN insights i ON i.article_id = a.id
            WHERE i.relevance_score >= ?
            ORDER BY a.id DESC
            LIMIT ?
            """,
            (min_relevance, limit),
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/chat")
def chat(payload: ChatIn):
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.title, a.url, i.summary, i.why_it_matters, i.question
            FROM articles a
            JOIN insights i ON i.article_id = a.id
            ORDER BY i.relevance_score DESC, a.id DESC
            LIMIT 8
            """
        ).fetchall()
        context = [dict(r) for r in rows]

    api_key = os.getenv("OPENAI_API_KEY")
    if api_key and context:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            prompt = {
                "user_message": message,
                "context": context,
                "task": "Answer based only on provided context. Provide concise discussion points and one follow-up question.",
            }
            resp = client.responses.create(
                model=OPENAI_MODEL,
                input=[{"role": "user", "content": json.dumps(prompt)}],
                temperature=0.3,
            )
            return {"reply": resp.output_text, "context_count": len(context)}
        except Exception:
            pass

    bullets = "\n".join([f"- {r['title']}: {r['question']}" for r in context[:3]])
    reply = (
        "Based on your current news set, here are discussion anchors:\n"
        f"{bullets}\n\n"
        f"Your question: {message}\n"
        "Suggested next step: pick one story and challenge the underlying assumption."
    )
    return {"reply": reply, "context_count": len(context)}
