"""Daily arXiv → Telegram alerter with Telegram-driven config + TF-IDF preference filter.

Each run:
  1. Polls Telegram for new /commands and inline-keyboard votes, mutating
     config.json and votes.json accordingly.
  2. (Daily mode) Fetches arXiv papers from configured categories submitted in
     the last LOOKBACK_HOURS, applies the preference filter (cold-start sends
     all), and alerts on each survivor with 👍/👎 buttons.

Commands (owner only):
  /list                  → show current categories
  /add_cat <arxiv.cat>   → add arXiv category
  /rm_cat <arxiv.cat>    → remove arXiv category
  /reset                 → restore defaults
  /like N [N ...]        → like papers by number from the latest batch
  /dislike N [N ...]     → dislike papers by number from the latest batch
  /later N [N …]         → save papers for later reading
  /read N [N …]          → mark papers as read
  /skip N [N …]          → mark papers as skipped + disliked
  /note N <text>         → attach a note to a paper from the latest batch
  /queue                 → show saved/unread papers
  /notes [query]         → search saved notes
  /digest                → send a weekly-style reading digest now
  /why N                 → explain why paper N matched your profile
  /stats                 → show vote counts + filter status
  /help                  → command list
"""

import argparse
import calendar
import json
import math
import os
import re
import sys
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import recommender

_LATEX2TEXT = None


def latex_to_unicode(text):
    global _LATEX2TEXT
    if _LATEX2TEXT is None:
        from pylatexenc.latex2text import LatexNodes2Text  # lazy; not needed in --commands-only mode
        _LATEX2TEXT = LatexNodes2Text(keep_comments=False, math_mode="text")
    try:
        return _LATEX2TEXT.latex_to_text(text)
    except Exception:
        return text

CONFIG_PATH = Path(__file__).parent / "config.json"
VOTES_PATH = Path(__file__).parent / "votes.json"
READING_LOG_PATH = Path(__file__).parent / "reading_log.json"

DEFAULT_CATEGORIES = [
    "cs.LG",
    "cs.MA",
    "econ.EM",
    "econ.TH",
    "math.NA",
    "math.OC",
    "math-ph",
    "nlin.SI",
    "physics.comp-ph",
    "physics.data-an",
    "physics.flu-dyn",
    "physics.plasm-ph",
    "physics.soc-ph",
    "q-fin.CP",
    "q-fin.MF",
    "q-fin.PM",
    "q-fin.PR",
    "q-fin.RM",
    "q-fin.ST",
    "q-fin.TR",
    "stat.ML"
]

LOOKBACK_HOURS = 36
ARXIV_RSS_URL = "https://rss.arxiv.org/rss/{category}"
ARXIV_MAX_ATTEMPTS = 4
ARXIV_BACKOFF_BASE = 30  # seconds; backoff is 30, 60, 120 between attempts
# arXiv throttles the default python-requests User-Agent; send a descriptive one.
ARXIV_USER_AGENT = "LitFeed/1.0 (https://github.com/shikang61/LitFeed)"
TELEGRAM_SAFE_MESSAGE_CHARS = 3900
GROK_API_BASE = "https://api.x.ai/v1"
GROK_DEFAULT_MODEL = "grok-4.3"
GROK_SUMMARY_TIMEOUT = 30
MAX_SENT_IDS = 500
MIN_VOTES_PER_SIDE = 10
# Set to 0 to disable category quota and let best papers win globally.
PER_CATEGORY_LIMIT = 0
MAX_PAPERS_PER_RUN = 5
SERENDIPITY_SLOTS = 1
RECENCY_HALF_LIFE_DAYS = 45
EARLY_ACTIVE_EXTRA_VOTES = 5
EARLY_ACTIVE_RELEVANCE_FLOOR = -0.03
DIVERSITY_MAX_JACCARD = 0.85
TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"

TOPIC_KEYWORDS = {
    "plasma": ["plasma", "tokamak", "fusion", "mhd", "gyrokinetic", "particle-in-cell"],
    "fluid dynamics": ["fluid", "turbulence", "navier", "stokes", "vorticity", "flow"],
    "PDEs": ["pde", "partial differential", "equation", "finite element", "spectral method"],
    "numerics": ["numerical", "simulation", "solver", "discretization", "monte carlo", "mesh"],
    "inverse problems": ["inverse problem", "bayesian", "uncertainty", "regularization", "reconstruction"],
    "optimization": ["optimization", "optimal control", "gradient", "convex", "variational"],
    "machine learning": ["machine learning", "neural", "transformer", "diffusion", "reinforcement learning"],
    "quantum": ["quantum", "qubit", "hamiltonian", "spectral triple", "nisq"],
    "finance": ["portfolio", "market", "trading", "risk", "volatility", "option"],
    "literature tools": ["literature", "retrieval", "scientific", "paper", "citation"],
}


# ---------- config + votes ----------

def load_config():
    if not CONFIG_PATH.exists():
        return {
            "categories": list(DEFAULT_CATEGORIES),
            "last_update_id": 0,
            "sent_ids": [],
        }
    with CONFIG_PATH.open() as f:
        cfg = json.load(f)
    cfg.setdefault("categories", list(DEFAULT_CATEGORIES))
    cfg.setdefault("last_update_id", 0)
    cfg.setdefault("sent_ids", [])
    return cfg


def save_config(cfg):
    with CONFIG_PATH.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def load_votes():
    if not VOTES_PATH.exists():
        return {"liked": [], "disliked": [], "last_batch": {}}
    with VOTES_PATH.open() as f:
        votes = json.load(f)
    votes.setdefault("liked", [])
    votes.setdefault("disliked", [])
    votes.setdefault("last_batch", {})  # batch number (str) → {"key","text"} for latest run
    # Migrate older state to compact shape and drop large legacy caches.
    sent_cache = votes.get("sent_cache", {}) if isinstance(votes.get("sent_cache"), dict) else {}
    migrated_batch = {}
    for n, entry in votes["last_batch"].items():
        if isinstance(entry, dict):
            migrated_batch[n] = {"key": entry.get("key"), "text": entry.get("text")}
            continue
        if isinstance(entry, str):
            legacy = sent_cache.get(entry, {})
            migrated_batch[n] = {"key": entry, "text": legacy.get("text")}
    if migrated_batch:
        votes["last_batch"] = migrated_batch
    votes.pop("sent_cache", None)
    return votes


def save_votes(votes):
    with VOTES_PATH.open("w") as f:
        json.dump(votes, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_reading_log():
    if not READING_LOG_PATH.exists():
        return {"papers": {}}
    with READING_LOG_PATH.open() as f:
        log = json.load(f)
    log.setdefault("papers", {})
    return log


def save_reading_log(log):
    with READING_LOG_PATH.open("w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _title_from_text(text):
    return (text or "").split("\n", 1)[0].strip()


def _ensure_paper_log_entry(log, key, text=None, title=None, url=None, categories=None, score=None):
    papers = log.setdefault("papers", {})
    entry = papers.setdefault(key, {"key": key, "created_ts": _now_iso(), "notes": []})
    if text and not entry.get("text"):
        entry["text"] = text
    if title or text:
        entry["title"] = title or entry.get("title") or _title_from_text(text)
    if url:
        entry["url"] = url
    if categories:
        entry["categories"] = list(categories)
    if score is not None and not math.isnan(score):
        entry["score"] = round(float(score), 4)
    entry.setdefault("notes", [])
    return entry


def record_sent_paper(log, paper, score=None):
    key = paper_key(paper)
    entry = _ensure_paper_log_entry(
        log,
        key,
        text=paper_text(paper),
        title=latex_to_unicode(paper.title.strip().replace("\n", " ")),
        url=paper.entry_id,
        categories=paper.categories,
        score=score,
    )
    entry["sent_ts"] = _now_iso()
    entry.setdefault("status", "sent")
    return entry


def update_reading_status(log, key, text, status, score=None):
    entry = _ensure_paper_log_entry(log, key, text=text, score=score)
    entry["status"] = status
    entry["status_ts"] = _now_iso()
    return entry


def add_paper_note(log, key, text, note):
    entry = _ensure_paper_log_entry(log, key, text=text)
    entry.setdefault("notes", []).append({"text": note, "ts": _now_iso()})
    entry["status"] = entry.get("status", "saved")
    entry["status_ts"] = _now_iso()
    return entry


def cached_summary_for_paper(log, key):
    entry = log.get("papers", {}).get(key, {})
    summary = entry.get("grok_summary")
    if isinstance(summary, dict):
        text = summary.get("text")
        if text:
            return text
    if isinstance(summary, str):
        return summary
    return None


def store_grok_summary(log, key, text, model):
    entry = _ensure_paper_log_entry(log, key)
    entry["grok_summary"] = {
        "text": text,
        "model": model,
        "ts": _now_iso(),
    }
    return entry


# ---------- telegram ----------

def telegram_call(token, method, **params):
    url = TELEGRAM_BASE.format(token=token, method=method)
    try:
        resp = requests.post(url, data=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[telegram] {method} failed: {e}", file=sys.stderr)
        return None


def send_message(token, chat_id, text, markdown=True, reply_markup=None):
    """Returns Telegram message_id on success, None on failure."""
    params = {"chat_id": chat_id, "text": text}
    if markdown:
        params["parse_mode"] = "Markdown"
        params["disable_web_page_preview"] = False
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup)
    result = telegram_call(token, "sendMessage", **params)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def edit_message_keyboard(token, chat_id, message_id, reply_markup):
    return telegram_call(
        token,
        "editMessageReplyMarkup",
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=json.dumps(reply_markup),
    )


def delete_message(token, chat_id, message_id):
    return telegram_call(token, "deleteMessage", chat_id=chat_id, message_id=message_id)


def grok_summarize_paper(paper):
    api_key = os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY")
    if not api_key:
        return None

    model = os.environ.get("GROK_MODEL") or GROK_DEFAULT_MODEL
    api_base = os.environ.get("GROK_API_BASE") or GROK_API_BASE
    url = f"{api_base.rstrip('/')}/chat/completions"
    prompt = (
        "Summarize this arXiv paper for a physics PhD student who wants to decide "
        "whether to read it. Use this exact structure, keep it concise, and avoid hype:\n\n"
        "TL;DR: <one sentence>\n"
        "Why it may matter: <one sentence>\n"
        "Best for: <short phrase>\n\n"
        f"Title: {paper.title.strip()}\n"
        f"Categories: {', '.join(paper.categories)}\n"
        f"Abstract: {paper.summary.strip()}"
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a concise research assistant. Be concrete, technical, "
                    "and honest about uncertainty."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 220,
    }
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=GROK_SUMMARY_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return {"text": text, "model": model}
    except (KeyError, IndexError, requests.RequestException, ValueError) as e:
        print(f"[grok] summarization failed for {paper_key(paper)}: {e}", file=sys.stderr)
        return None


def fetch_updates(token, offset):
    """Return list of new updates (messages + callback_query) with id > offset."""
    result = telegram_call(
        token, "getUpdates",
        offset=offset + 1,
        timeout=0,
        allowed_updates=json.dumps(["message", "edited_message", "callback_query"]),
    )
    if result is None or not result.get("ok"):
        return []
    return result.get("result", [])


# ---------- commands ----------

def handle_command(text, cfg, votes, reading_log):
    """Mutate cfg in place. Return (cfg_changed: bool, reply: str | None)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/list", "/list@"):
        return False, _format_list(cfg)

    if cmd == "/stats":
        return False, _format_stats(votes, reading_log)

    if cmd == "/help":
        return False, (
            "*Commands*\n"
            "/list — show categories\n"
            "/add\\_cat <arxiv.cat> — add category\n"
            "/rm\\_cat <arxiv.cat> — remove category\n"
            "/reset — restore default categories\n"
            "/like N [N …] — like papers by batch number\n"
            "/dislike N [N …] — dislike papers by batch number\n"
            "/later N [N …] — save papers for later reading\n"
            "/read N [N …] — mark papers as read\n"
            "/skip N [N …] — skip papers and train them as negative examples\n"
            "/note N <text> — attach a note to a latest-batch paper\n"
            "/queue — show saved/unread papers\n"
            "/notes [query] — search your paper notes\n"
            "/digest — send a weekly-style reading digest now\n"
            "/why N — explain why a paper matched\n"
            "/stats — vote counts + filter status\n"
            "/help — this message"
        )

    if cmd in ("/later", "/read", "/skip"):
        changed, reply = handle_reading_status_command(text, votes, reading_log)
        return changed, reply

    if cmd == "/note":
        changed, reply = handle_note_command(text, votes, reading_log)
        return changed, reply

    if cmd == "/queue":
        return False, format_reading_queue(reading_log)

    if cmd == "/notes":
        return False, format_notes(reading_log, arg)

    if cmd == "/digest":
        return False, format_weekly_digest(votes, reading_log)

    if cmd == "/why":
        if not arg:
            return False, "Usage: `/why <number>` — number from the latest batch."
        entry = votes.get("last_batch", {}).get(arg)
        if not isinstance(entry, dict):
            return False, f"Paper `{arg}` not found in the latest batch."
        text_value = entry.get("text")
        if not text_value:
            return False, "No context available for that paper."
        if not filter_active(votes):
            return False, (
                f"*Why {arg}?*\n"
                "Filter is in *cold start*, so selection is currently freshness-first.\n"
                "Once you have enough 👍 and 👎 votes, personalized matching will kick in."
            )
        model = build_model(votes)
        floor = current_relevance_floor(votes)
        score_value = entry.get("score")
        if score_value is None:
            score_value = recommender.score(text_value, model)
        reasons = recommender.explain(text_value, model, top_n=5)
        reason_text = ", ".join(f"`{escape_markdown(t)}`" for t, _ in reasons) if reasons else "_no strong token signals_"
        return False, (
            f"*Why {arg}?*\n"
            f"Score: `{score_value:.3f}`\n"
            f"Selection threshold: `{floor:.3f}`\n"
            f"Top matched terms: {reason_text}"
        )

    if cmd == "/add_cat":
        if not arg:
            return False, "Usage: `/add_cat <arxiv.category>`"
        if arg in cfg["categories"]:
            return False, f"Category already present: `{arg}`"
        cfg["categories"].append(arg)
        return True, f"Added category: `{arg}`"

    if cmd == "/rm_cat":
        if not arg:
            return False, "Usage: `/rm_cat <arxiv.category>`"
        if arg not in cfg["categories"]:
            return False, f"Category not found: `{arg}`"
        cfg["categories"].remove(arg)
        return True, f"Removed category: `{arg}`"

    if cmd == "/reset":
        cfg["categories"] = list(DEFAULT_CATEGORIES)
        return True, "Config reset to defaults."

    return False, None


def _format_list(cfg):
    cats = "\n".join(f"• `{c}`" for c in cfg["categories"]) or "_(none)_"
    return f"*Categories*\n{cats}"


def _format_stats(votes, reading_log):
    nl, nd = len(votes["liked"]), len(votes["disliked"])
    active = nl >= MIN_VOTES_PER_SIDE and nd >= MIN_VOTES_PER_SIDE
    status = "active" if active else f"cold start (need ≥{MIN_VOTES_PER_SIDE} each)"
    reading_counts = reading_status_counts(reading_log)
    topics = format_topic_summary(reading_log)
    return (
        f"*Votes*\n👍 {nl}\n👎 {nd}\n\n"
        f"*Filter:* {status}\n\n"
        f"*Reading*\n"
        f"Saved: {reading_counts.get('saved', 0)}\n"
        f"Read: {reading_counts.get('read', 0)}\n"
        f"Skipped: {reading_counts.get('skipped', 0)}\n\n"
        f"*Topics:* {topics}"
    )


def reading_status_counts(reading_log):
    counts = {}
    for entry in reading_log.get("papers", {}).values():
        status = entry.get("status", "sent")
        counts[status] = counts.get(status, 0) + 1
    return counts


def infer_topics(text):
    lowered = (text or "").lower()
    matched = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            matched.append(topic)
    return matched


def top_topics_for_entries(entries, limit=5):
    counts = {}
    for entry in entries:
        text = f"{entry.get('title', '')}\n{entry.get('text', '')}"
        for topic in infer_topics(text):
            counts[topic] = counts.get(topic, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def format_topic_summary(reading_log):
    meaningful = [
        entry
        for entry in reading_log.get("papers", {}).values()
        if entry.get("status") in ("saved", "read")
    ]
    topics = top_topics_for_entries(meaningful)
    if not topics:
        return "_not enough reading data yet_"
    return ", ".join(f"`{escape_markdown(topic)}` ({count})" for topic, count in topics)


def _sorted_papers(reading_log):
    return sorted(
        reading_log.get("papers", {}).values(),
        key=lambda entry: entry.get("status_ts") or entry.get("sent_ts") or entry.get("created_ts", ""),
        reverse=True,
    )


def format_reading_queue(reading_log, limit=10):
    queue = [
        entry for entry in _sorted_papers(reading_log)
        if entry.get("status") in ("saved", "sent")
    ][:limit]
    if not queue:
        return "*Reading queue*\n_empty_"
    lines = ["*Reading queue*"]
    for i, entry in enumerate(queue, start=1):
        title = escape_markdown(entry.get("title") or _title_from_text(entry.get("text", "")) or entry["key"])
        status = entry.get("status", "sent")
        lines.append(f"{i}. `{entry['key']}` [{status}] {title}")
    return "\n".join(lines)


def format_notes(reading_log, query="", limit=10):
    query_lc = query.lower()
    matches = []
    for entry in _sorted_papers(reading_log):
        haystack = f"{entry.get('title', '')}\n{entry.get('text', '')}".lower()
        for note in entry.get("notes", []):
            note_text = note.get("text", "")
            if query_lc and query_lc not in haystack and query_lc not in note_text.lower():
                continue
            matches.append((entry, note_text))
            break
        if len(matches) >= limit:
            break
    if not matches:
        return "*Notes*\n_no matching notes yet_"
    lines = ["*Notes*"]
    for entry, note_text in matches:
        title = escape_markdown(entry.get("title") or _title_from_text(entry.get("text", "")) or entry["key"])
        lines.append(f"• `{entry['key']}` {title}\n  _{escape_markdown(note_text)}_")
    return "\n".join(lines)


def format_weekly_digest(votes, reading_log):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    recent = []
    for entry in reading_log.get("papers", {}).values():
        ts = entry.get("status_ts") or entry.get("sent_ts") or entry.get("created_ts")
        try:
            parsed = datetime.fromisoformat(ts) if ts else None
        except ValueError:
            parsed = None
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= cutoff:
            recent.append(entry)

    counts = {}
    for entry in recent:
        status = entry.get("status", "sent")
        counts[status] = counts.get(status, 0) + 1

    queue = [
        entry for entry in _sorted_papers(reading_log)
        if entry.get("status") in ("saved", "sent")
    ]
    deep_read = choose_deep_read_candidate(votes, reading_log)
    topics = top_topics_for_entries(recent or queue)

    lines = [
        "*Weekly reading digest*",
        f"Saved: {counts.get('saved', 0)}",
        f"Read: {counts.get('read', 0)}",
        f"Skipped: {counts.get('skipped', 0)}",
    ]
    if topics:
        lines.append("Themes: " + ", ".join(f"`{escape_markdown(t)}`" for t, _ in topics))
    if deep_read:
        title = escape_markdown(deep_read.get("title") or _title_from_text(deep_read.get("text", "")) or deep_read["key"])
        lines.append(f"\n*Deep read pick*\n`{deep_read['key']}` {title}")
        if deep_read.get("url"):
            lines.append(deep_read["url"])
    if queue:
        lines.append("\n*Unread queue*")
        for entry in queue[:5]:
            title = escape_markdown(entry.get("title") or _title_from_text(entry.get("text", "")) or entry["key"])
            lines.append(f"• `{entry['key']}` {title}")
    return "\n".join(lines)


# ---------- votes ----------

def _record_vote(votes, key, text, action):
    """Move a paper into the liked/disliked bucket, replacing any prior vote."""
    votes["liked"] = [v for v in votes["liked"] if v["key"] != key]
    votes["disliked"] = [v for v in votes["disliked"] if v["key"] != key]
    bucket = "liked" if action == "like" else "disliked"
    votes[bucket].append({
        "key": key,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def handle_vote_command(text, votes):
    """Handle `/like` or `/dislike` followed by batch numbers from the latest
    run. Returns (votes_changed: bool, reply: str)."""
    parts = text.strip().split()
    action = "like" if parts[0].lower().startswith("/like") else "dislike"
    nums = parts[1:]
    if not nums:
        return False, f"Usage: `/{action} <number> [number …]` — numbers from the latest batch."

    last_batch = votes.get("last_batch", {})
    if not last_batch:
        return False, "No batch to vote on yet — wait for the next run."

    recorded, missing = [], []
    for n in nums:
        entry = last_batch.get(n)
        key = None
        paper_text_value = None
        if isinstance(entry, dict):
            key = entry.get("key")
            paper_text_value = entry.get("text")

        if not key or not paper_text_value:
            missing.append(n)
            continue
        _record_vote(votes, key, paper_text_value, action)
        recorded.append(n)

    emoji = "👍" if action == "like" else "👎"
    lines = []
    if recorded:
        lines.append(f"{emoji} recorded for: {', '.join(recorded)}")
    if missing:
        lines.append(f"⚠️ not found: {', '.join(missing)}")
    return bool(recorded), "\n".join(lines) or "Nothing recorded."


def _latest_batch_entry(votes, n):
    entry = votes.get("last_batch", {}).get(n)
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    text_value = entry.get("text")
    if not key or not text_value:
        return None
    return entry


def handle_reading_status_command(text, votes, reading_log):
    """Handle `/later`, `/read`, and `/skip` for latest-batch numbers."""
    parts = text.strip().split()
    cmd = parts[0].lower()
    nums = parts[1:]
    if not nums:
        return False, f"Usage: `{cmd} <number> [number …]` — numbers from the latest batch."

    status_by_cmd = {"/later": "saved", "/read": "read", "/skip": "skipped"}
    status = status_by_cmd[cmd]
    verb = {"saved": "saved for later", "read": "marked read", "skipped": "skipped"}[status]
    recorded, missing = [], []
    for n in nums:
        entry = _latest_batch_entry(votes, n)
        if entry is None:
            missing.append(n)
            continue
        update_reading_status(
            reading_log,
            entry["key"],
            entry["text"],
            status,
            score=entry.get("score"),
        )
        if status == "skipped":
            _record_vote(votes, entry["key"], entry["text"], "dislike")
        recorded.append(n)

    lines = []
    if recorded:
        lines.append(f"{verb}: {', '.join(recorded)}")
    if missing:
        lines.append(f"not found: {', '.join(missing)}")
    return bool(recorded), "\n".join(lines) or "Nothing recorded."


def handle_note_command(text, votes, reading_log):
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return False, "Usage: `/note <number> <note>` — number from the latest batch."
    _, n, note = parts
    entry = _latest_batch_entry(votes, n)
    if entry is None:
        return False, f"Paper `{n}` not found in the latest batch."
    add_paper_note(reading_log, entry["key"], entry["text"], note.strip())
    return True, f"Note saved for `{n}`."


def handle_callback(token, chat_id, callback, votes, reading_log):
    """Record owner per-paper vote/reading buttons. Returns (votes_changed, reading_changed)."""
    cb_id = callback["id"]
    parts = callback.get("data", "").split(":", 2)
    valid_vote = len(parts) == 3 and parts[0] == "v" and parts[1] in ("like", "dislike")
    valid_habit = (
        len(parts) == 3
        and parts[0] == "h"
        and parts[1] in ("later", "read", "skip", "delete", "confirm_delete", "cancel_delete")
    )
    if not valid_vote and not valid_habit:
        telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id)
        return False, False
    kind, action, key = parts

    message = callback.get("message") or {}
    message_id = message.get("message_id")

    if kind == "h" and action in ("delete", "confirm_delete", "cancel_delete"):
        if not message_id:
            telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id, text="Message unavailable.")
            return False, False
        if action == "delete":
            edit_message_keyboard(token, chat_id, message_id, delete_confirmation_keyboard(key))
            telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id, text="Confirm deletion?")
            return False, False
        if action == "cancel_delete":
            edit_message_keyboard(token, chat_id, message_id, vote_keyboard(key))
            telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id, text="Deletion cancelled.")
            return False, False
        result = delete_message(token, chat_id, message_id)
        if result and result.get("ok"):
            telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id, text="Deleted.")
        else:
            telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id, text="Could not delete message.")
        return False, False

    text_value = None
    # Primary source: latest batch payload (small and durable).
    for entry in votes.get("last_batch", {}).values():
        if isinstance(entry, dict) and entry.get("key") == key:
            text_value = entry.get("text")
            if text_value:
                break
    if not text_value:
        telegram_call(
            token, "answerCallbackQuery",
            callback_query_id=cb_id,
            text="Paper context expired; vote from latest batch.",
        )
        return False, False

    if kind == "v":
        _record_vote(votes, key, text_value, action)
        toast = f"Recorded {'👍' if action == 'like' else '👎'}"
        votes_changed = True
        reading_changed = False
    else:
        status = {"later": "saved", "read": "read", "skip": "skipped"}[action]
        update_reading_status(reading_log, key, text_value, status)
        if action == "skip":
            _record_vote(votes, key, text_value, "dislike")
        toast = {"later": "Saved for later", "read": "Marked read", "skip": "Skipped"}[action]
        votes_changed = action == "skip"
        reading_changed = True
    telegram_call(
        token, "answerCallbackQuery",
        callback_query_id=cb_id,
        text=toast,
    )
    return votes_changed, reading_changed


def process_telegram_updates(token, chat_id, cfg, votes, reading_log):
    """Poll Telegram; apply owner commands + vote callbacks.
    Returns (cfg_changed, votes_changed, reading_changed)."""
    try:
        owner_id = int(chat_id)
    except (TypeError, ValueError):
        print("CHAT_ID is not an integer; skipping update processing.", file=sys.stderr)
        return False, False, False

    updates = fetch_updates(token, cfg["last_update_id"])
    cfg_changed = False
    votes_changed = False
    reading_changed = False

    for upd in updates:
        cfg["last_update_id"] = max(cfg["last_update_id"], upd.get("update_id", 0))

        cb = upd.get("callback_query")
        if cb:
            sender = cb.get("from", {}).get("id")
            if sender != owner_id:
                continue
            vote_mutated, reading_mutated = handle_callback(token, chat_id, cb, votes, reading_log)
            if vote_mutated:
                votes_changed = True
            if reading_mutated:
                reading_changed = True
            continue

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        sender = msg.get("from", {}).get("id")
        if sender != owner_id:
            continue
        text = msg.get("text", "")
        if not text.startswith("/"):
            continue
        if text.strip().split()[0].lower() in ("/like", "/dislike"):
            changed, reply = handle_vote_command(text, votes)
            send_message(token, chat_id, reply)
            if changed:
                votes_changed = True
            continue
        command = text.strip().split()[0].lower()
        mutated, reply = handle_command(text, cfg, votes, reading_log)
        if reply:
            send_message(token, chat_id, reply)
        if mutated:
            if command in ("/later", "/read", "/skip", "/note"):
                reading_changed = True
                if command == "/skip":
                    votes_changed = True
            else:
                cfg_changed = True

    return cfg_changed, votes_changed, reading_changed


# ---------- arxiv ----------

_Author = namedtuple("_Author", ["name"])


class _Paper:
    """Minimal stand-in for arxiv.Result — only the attributes main.py touches.

    Built from an arXiv RSS feed entry (rss.arxiv.org), whose schema differs
    from the legacy Atom API.
    """

    def __init__(self, entry):
        self.entry_id = entry.get("link", "")  # e.g. https://arxiv.org/abs/2401.12345
        self.title = entry.get("title", "")
        # RSS summary is prefixed with "arXiv:... Announce Type: ...\nAbstract: <text>"
        summary = entry.get("summary", "")
        if "Abstract:" in summary:
            summary = summary.split("Abstract:", 1)[1]
        self.summary = summary.strip()
        # RSS crams every author into a single comma-separated string
        raw_authors = entry.get("author", "")
        self.authors = [_Author(n.strip()) for n in raw_authors.split(",") if n.strip()]
        self.categories = [t.get("term", "") for t in entry.get("tags", [])]
        self.primary_category = self.categories[0] if self.categories else ""
        # feedparser exposes the published date as a UTC struct_time
        self.published = datetime.fromtimestamp(
            calendar.timegm(entry.published_parsed), tz=timezone.utc
        )
        # one of: new, cross, replace, replace-cross
        self.announce_type = entry.get("arxiv_announce_type", "")

    def get_short_id(self):
        # entry_id looks like https://arxiv.org/abs/2401.12345
        return self.entry_id.rsplit("/abs/", 1)[-1]


def _arxiv_get(url):
    """GET an arXiv RSS feed with backoff; honors the Retry-After header on 429."""
    for attempt in range(1, ARXIV_MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(
                url,
                timeout=30,
                headers={"User-Agent": ARXIV_USER_AGENT},
            )
        except requests.RequestException as e:
            if attempt == ARXIV_MAX_ATTEMPTS:
                raise
            wait = ARXIV_BACKOFF_BASE * (2 ** (attempt - 1))
            print(
                f"[arxiv] request error attempt {attempt}/{ARXIV_MAX_ATTEMPTS}: {e}; "
                f"retrying in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            if attempt == ARXIV_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"arXiv still HTTP 429 after {ARXIV_MAX_ATTEMPTS} attempts"
                )
            retry_after = resp.headers.get("Retry-After", "")
            wait = (
                int(retry_after)
                if retry_after.isdigit()
                else ARXIV_BACKOFF_BASE * (2 ** (attempt - 1))
            )
            print(
                f"[arxiv] HTTP 429 attempt {attempt}/{ARXIV_MAX_ATTEMPTS}; "
                f"retrying in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        resp.raise_for_status()
        return resp.content

    raise RuntimeError("arXiv fetch failed")  # unreachable


def fetch_recent_papers(categories, hours):
    """Fetch newly announced papers via per-category arXiv RSS feeds.

    The RSS endpoint is a separate, cached host with far looser rate limits
    than the legacy API. Each feed is one category's latest daily announcement,
    so `hours` only acts as a defensive lower bound on the published date.
    """
    if not categories:
        return []
    import feedparser  # lazy import; not needed in --commands-only mode
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    recent = []
    seen = set()
    for cat in categories:
        feed = feedparser.parse(_arxiv_get(ARXIV_RSS_URL.format(category=cat)))
        for entry in feed.entries:
            paper = _Paper(entry)
            # RSS feeds also carry version replacements; keep only new listings
            # (a cross-listing is new to this category, so it counts).
            if paper.announce_type not in ("new", "cross"):
                continue
            if paper.published < cutoff:
                continue
            key = paper.get_short_id()
            if key in seen:  # cross-listed papers appear in multiple feeds
                continue
            seen.add(key)
            recent.append(paper)
        time.sleep(1)  # be polite between feed requests
    return recent


def paper_key(paper):
    """Stable ID for dedup; strip version suffix so v1/v2 don't both alert."""
    sid = paper.get_short_id()
    return sid.rsplit("v", 1)[0] if "v" in sid else sid


def paper_text(paper):
    return f"{paper.title}\n\n{paper.summary}"


def escape_markdown(text):
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def format_paper(paper, index, grok_summary=None):
    title = escape_markdown(latex_to_unicode(paper.title.strip().replace("\n", " ")))
    names = [a.name for a in paper.authors]
    if len(names) <= 3:
        authors = ", ".join(names)
    else:
        authors = f"{names[0]}, {names[1]}, …, {names[-1]}"
    authors = escape_markdown(latex_to_unicode(authors))
    abstract = latex_to_unicode(paper.summary.strip().replace("\n", " "))
    escaped_abstract = escape_markdown(abstract)
    categories = " ".join(f"\\[{c}]" for c in paper.categories)
    prefix = (
        f"*[{index}] {title}*\n"
        f"_{authors}_\n"
        f"{categories}\n\n"
    )
    if grok_summary:
        prefix += f"*Grok summary*\n{escape_markdown(grok_summary.strip())}\n\n"
    suffix = f"\n\n[arXiv:{paper.get_short_id()}]({paper.entry_id})"
    available = max(TELEGRAM_SAFE_MESSAGE_CHARS - len(prefix) - len(suffix), 0)
    if len(escaped_abstract) > available:
        escaped_abstract = escaped_abstract[:available].rsplit(" ", 1)[0] + "…"
    return f"{prefix}{escaped_abstract}{suffix}"


def vote_keyboard(key):
    return {
        "inline_keyboard": [
            [
                {"text": "Delete", "callback_data": f"h:delete:{key}"},
            ],
        ]
    }


def delete_confirmation_keyboard(key):
    return {
        "inline_keyboard": [
            [
                {"text": "Confirm delete", "callback_data": f"h:confirm_delete:{key}"},
                {"text": "Cancel", "callback_data": f"h:cancel_delete:{key}"},
            ],
        ]
    }


def format_run_summary(selected_count, candidate_count, filter_is_active):
    mode = "active" if filter_is_active else "cold start"
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"*Run summary*\n"
        f"Selected: *{selected_count}* of *{candidate_count}* candidates\n"
        f"Filter mode: *{mode}*\n"
        f"Run: `{run_time}`"
    )


# ---------- recommender wiring ----------

def filter_active(votes):
    return (
        len(votes["liked"]) >= MIN_VOTES_PER_SIDE
        and len(votes["disliked"]) >= MIN_VOTES_PER_SIDE
    )


def _parse_vote_time(vote):
    raw = vote.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def vote_recency_weight(vote, now):
    ts = _parse_vote_time(vote)
    if ts is None:
        return 1.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = max((now - ts).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def build_model(votes):
    now = datetime.now(timezone.utc)
    liked = votes["liked"]
    disliked = votes["disliked"]
    return recommender.fit(
        [v["text"] for v in liked],
        [v["text"] for v in disliked],
        liked_weights=[vote_recency_weight(v, now) for v in liked],
        disliked_weights=[vote_recency_weight(v, now) for v in disliked],
    )


def current_relevance_floor(votes):
    min_side = min(len(votes["liked"]), len(votes["disliked"]))
    if min_side < MIN_VOTES_PER_SIDE:
        return None
    if min_side < (MIN_VOTES_PER_SIDE + EARLY_ACTIVE_EXTRA_VOTES):
        return EARLY_ACTIVE_RELEVANCE_FLOOR
    return 0.0


def tokenize_for_diversity(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard_similarity(a_tokens, b_tokens):
    if not a_tokens or not b_tokens:
        return 0.0
    inter = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    return inter / union if union else 0.0


def apply_diversity_guardrail(scored_papers, threshold):
    """Keep ranking order but skip near-duplicates by text similarity."""
    kept = []
    kept_tokens = []
    for paper, score_value in scored_papers:
        tokens = tokenize_for_diversity(f"{paper.title} {paper.summary}")
        if any(jaccard_similarity(tokens, prev) >= threshold for prev in kept_tokens):
            continue
        kept.append((paper, score_value))
        kept_tokens.append(tokens)
    return kept


def cap_per_category(papers, limit):
    """Keep first `limit` papers per primary_category, preserve input order."""
    if limit <= 0:
        return papers
    counts = {}
    kept = []
    for p in papers:
        cat = p.primary_category
        if counts.get(cat, 0) >= limit:
            continue
        counts[cat] = counts.get(cat, 0) + 1
        kept.append(p)
    return kept


def cap_total(papers, limit):
    """Keep at most `limit` papers, preserving input order."""
    return papers[:limit]


def select_with_serendipity(scored_papers, floor, total_limit, slots):
    """Reserve a small slot for near-miss papers when the preference filter is active."""
    if slots <= 0 or total_limit <= 1:
        return [p for p, s in scored_papers if s >= floor]

    relevant = [(p, s) for p, s in scored_papers if s >= floor]
    near_misses = [(p, s) for p, s in scored_papers if s < floor]
    base_limit = max(total_limit - slots, 1)
    selected = relevant[:base_limit]
    used = {paper_key(p) for p, _ in selected}

    wildcards = []
    for paper, score_value in near_misses:
        if paper_key(paper) in used:
            continue
        wildcards.append((paper, score_value))
        used.add(paper_key(paper))
        if len(wildcards) >= slots:
            break

    if wildcards:
        return [p for p, _ in selected + wildcards]
    return [p for p, _ in relevant]


def choose_deep_read_candidate(votes, reading_log):
    candidates = [
        entry for entry in reading_log.get("papers", {}).values()
        if entry.get("status") in ("saved", "sent") and entry.get("text")
    ]
    if not candidates:
        return None

    model = build_model(votes) if filter_active(votes) else None
    scored = []
    for entry in candidates:
        if model is not None:
            score_value = recommender.score(entry["text"], model)
        else:
            score_value = float(entry.get("score", 0.0) or 0.0)
        status_bonus = 0.25 if entry.get("status") == "saved" else 0.0
        scored.append((score_value + status_bonus, entry.get("status_ts") or entry.get("sent_ts") or "", entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def summary_for_paper(paper, reading_log):
    key = paper_key(paper)
    cached = cached_summary_for_paper(reading_log, key)
    if cached:
        return cached
    summary = grok_summarize_paper(paper)
    if not summary:
        return None
    store_grok_summary(reading_log, key, summary["text"], summary["model"])
    return summary["text"]


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commands-only",
        action="store_true",
        help="Only process queued Telegram updates (commands + votes); skip arXiv fetch.",
    )
    parser.add_argument(
        "--weekly-digest",
        action="store_true",
        help="Send the reading digest and skip arXiv fetch.",
    )
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID env vars.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    votes = load_votes()
    reading_log = load_reading_log()

    # 1. process pending /commands + callback votes
    cfg_changed, votes_changed, reading_changed = process_telegram_updates(token, chat_id, cfg, votes, reading_log)
    save_config(cfg)  # always — last_update_id advances
    if votes_changed:
        save_votes(votes)
    if reading_changed:
        save_reading_log(reading_log)
    if cfg_changed:
        print("Config mutated by Telegram command(s) this run.")
    if votes_changed:
        print("Votes mutated by callback(s) this run.")
    if reading_changed:
        print("Reading log mutated by Telegram action(s) this run.")

    if args.weekly_digest:
        send_message(token, chat_id, format_weekly_digest(votes, reading_log))
        return

    if args.commands_only:
        return

    # 2. fetch + filter + alert
    try:
        papers = fetch_recent_papers(cfg["categories"], LOOKBACK_HOURS)
    except Exception as e:
        print(f"[arxiv] fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    sent_ids = set(cfg["sent_ids"])
    fresh = [p for p in papers if paper_key(p) not in sent_ids]
    score_by_key = {}

    if filter_active(votes):
        model = build_model(votes)
        floor = current_relevance_floor(votes)
        scored = [(p, recommender.score(paper_text(p), model)) for p in fresh]
        # Primary objective: personal relevance. Secondary: freshness.
        scored.sort(key=lambda ps: (ps[1], ps[0].published), reverse=True)
        scored = apply_diversity_guardrail(scored, DIVERSITY_MAX_JACCARD)
        kept = select_with_serendipity(scored, floor, MAX_PAPERS_PER_RUN, SERENDIPITY_SLOTS)
        score_by_key = {paper_key(p): s for p, s in scored}
        print(
            f"Fetched {len(papers)} papers; {len(fresh)} new after dedup; "
            f"{len(kept)} selected with {SERENDIPITY_SLOTS} serendipity slot(s) "
            f"(threshold {floor:.2f})."
        )
        to_send = kept
    else:
        # Cold start: no relevance model yet, so use freshest-first globally.
        fresh.sort(key=lambda p: p.published, reverse=True)
        cold_scored = [(p, math.nan) for p in fresh]
        cold_scored = apply_diversity_guardrail(cold_scored, DIVERSITY_MAX_JACCARD)
        print(
            f"Fetched {len(papers)} papers; {len(fresh)} new after dedup; "
            f"filter cold (likes={len(votes['liked'])}, dislikes={len(votes['disliked'])}) "
            f"— sending freshest with diversity guardrail ({len(cold_scored)} survivors)."
        )
        to_send = [p for p, _ in cold_scored]

    if PER_CATEGORY_LIMIT > 0:
        before_cap = len(to_send)
        to_send = cap_per_category(to_send, PER_CATEGORY_LIMIT)
        print(f"Capped per-category ({PER_CATEGORY_LIMIT}): {before_cap} → {len(to_send)}.")
    else:
        print("Per-category cap disabled: best papers win globally.")

    before_total_cap = len(to_send)
    to_send = cap_total(to_send, MAX_PAPERS_PER_RUN)
    print(f"Capped total per run ({MAX_PAPERS_PER_RUN}): {before_total_cap} → {len(to_send)}.")

    if not papers:
        run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        send_message(
            token,
            chat_id,
            f"_No papers fetched from arXiv_\nRun: `{run_time}`",
        )

    if to_send:
        send_message(
            token,
            chat_id,
            format_run_summary(
                selected_count=len(to_send),
                candidate_count=len(fresh),
                filter_is_active=filter_active(votes),
            ),
        )

    newly_sent = []
    last_batch = {}
    for i, paper in enumerate(to_send, start=1):
        key = paper_key(paper)
        text_value = paper_text(paper)
        grok_summary = summary_for_paper(paper, reading_log)
        msg_id = send_message(
            token, chat_id, format_paper(paper, i, grok_summary=grok_summary),
            reply_markup=vote_keyboard(key),
        )
        if msg_id is not None:
            newly_sent.append(key)
            record_sent_paper(reading_log, paper, score=score_by_key.get(key, math.nan))
            # Keep only the latest batch payload for command/callback voting.
            last_batch[str(i)] = {"key": key, "text": text_value}
            if key in score_by_key:
                last_batch[str(i)]["score"] = round(score_by_key[key], 4)
        time.sleep(1)  # be polite to Telegram

    if newly_sent:
        cfg["sent_ids"] = (cfg["sent_ids"] + newly_sent)[-MAX_SENT_IDS:]
        save_config(cfg)
        votes["last_batch"] = last_batch
        save_votes(votes)
        save_reading_log(reading_log)
        print(f"Sent {len(newly_sent)} messages.")


if __name__ == "__main__":
    main()
