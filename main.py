"""Daily arXiv → Telegram alerter with TF-IDF preference filter.

Each daily run fetches arXiv papers from configured categories, applies the
preference filter (cold-start sends the freshest), and alerts on survivors with
inline 👍/👎/Read/Delete buttons.

Telegram commands and button callbacks are handled by the Cloudflare Worker
(``worker/index.js``). GitHub Actions runs ``python main.py --apply-update`` for
/digest, /reset, and confirmed /clear only (Worker → repository_dispatch).

Weekly digest: ``python main.py --weekly-digest``.
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
import state_store

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
GROK_SUMMARY_LABELS = ("TL;DR:", "Topics:")
MAX_SENT_IDS = 500
MIN_VOTES_PER_SIDE = 10
# Set to 0 to disable category quota and let best papers win globally.
PER_CATEGORY_LIMIT = 0
MAX_PAPERS_PER_RUN = 10
SERENDIPITY_SLOTS = 1
PRIORITY_PAPERS_PER_RUN = 7
RECENCY_HALF_LIFE_DAYS = 45
EARLY_ACTIVE_EXTRA_VOTES = 5
EARLY_ACTIVE_RELEVANCE_FLOOR = -0.03
DIVERSITY_MAX_JACCARD = 0.85
READ_SIGNAL_WEIGHT = 1.5  # saved (Read) papers in the liked corpus
# cron-job.org fires daily_papers 3× (11:00, 14:00, 20:00) → up to RUNS_PER_DAY batches.
RUNS_PER_DAY = 3
THEME_LOOKBACK_DAYS = 7
# Max papers per inferred theme in the lookback window (~one full day's sends).
THEME_WEEKLY_CAP = MAX_PAPERS_PER_RUN * RUNS_PER_DAY
CATEGORY_PREF_ALPHA = 0.08
TFIDF_BLEND = 0.5
TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"

PRIORITY_CATEGORIES = {
    "cs.MA",
    "math.NA",
    "math-ph",
    "physics.comp-ph",
    "physics.flu-dyn",
    "physics.plasm-ph",
    "q-fin.CP",
    "q-fin.PM",
    "q-fin.TR",
    "q-fin.RM",
}

with (Path(__file__).resolve().parent / "shared" / "topic_keywords.json").open(
    encoding="utf-8"
) as _topic_keywords_file:
    TOPIC_KEYWORDS = json.load(_topic_keywords_file)

# ---------- config + votes ----------
#
# All persistence lives in Cloudflare D1 via ``state_store`` (see
# ``state_store.py`` and ``docs/architecture.md``). Per-row mutations
# (votes, reading_log entries, last_update_id) are written in real time
# through the narrow mutators (record_vote, upsert_paper_log,
# set_last_update_id, …); ``save_config`` / ``save_votes`` flush
# wholesale-replaced state (last_batch table, sent_ids ring buffer).
# The only file the app writes is ``config.json``, and only when
# ``categories`` changes (i.e. on /reset).

state_store.set_default_categories(DEFAULT_CATEGORIES)

load_config = state_store.load_config
save_config = state_store.save_config
load_votes = state_store.load_votes
save_votes = state_store.save_votes
load_reading_log = state_store.load_reading_log


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _title_from_text(text):
    return (text or "").split("\n", 1)[0].strip()


def _ensure_paper_log_entry(log, key, text=None, title=None, url=None, categories=None, score=None):
    papers = log.setdefault("papers", {})
    entry = papers.setdefault(key, {"key": key, "created_ts": _now_iso()})
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
    state_store.upsert_paper_log(
        key,
        title=entry.get("title"),
        url=entry.get("url"),
        text=entry.get("text"),
        categories=entry.get("categories"),
        score=entry.get("score"),
        status=entry.get("status"),
        sent_ts=entry.get("sent_ts"),
        created_ts=entry.get("created_ts"),
    )
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
    state_store.upsert_paper_log(
        key,
        grok_summary=entry["grok_summary"],
        created_ts=entry.get("created_ts"),
    )
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


def edit_message_text(token, chat_id, message_id, text, reply_markup=None):
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup)
    return telegram_call(token, "editMessageText", **params)


def grok_summarize_paper(paper):
    api_key = os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY")
    if not api_key:
        return None

    model = os.environ.get("GROK_MODEL") or GROK_DEFAULT_MODEL
    api_base = os.environ.get("GROK_API_BASE") or GROK_API_BASE
    url = f"{api_base.rstrip('/')}/chat/completions"
    prompt = (
        "Summarize this arXiv paper for a physics PhD student deciding whether to read it. "
        "Optimize for fast scanning: short, plain sentences with no hype. "
        "Do not omit important technical terms — keep the precise jargon (methods, models, "
        "observables) that tells a reader what kind of paper this is. The first time any "
        "acronym or specialist term appears, gloss it inline in parentheses, e.g. "
        "'GRB (gamma-ray burst)' or 'reconnection (magnetic field-line rearrangement)'. "
        "After the first mention you may use the short form. Use this exact structure:\n\n"
        "TL;DR: <one sentence>\n"
        "Topics: <3 or 4 short keywords or phrases, comma-separated, "
        "drawn from the paper itself — concrete methods/models/objects, not generic words>\n\n"
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
        "max_tokens": 280,
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


# ---------- commands ----------

CLEAR_DONE_TEXT = (
    "*LitFeed cleared*\n\n"
    "Removed all votes, reading history, sent-paper dedup, "
    "last batch, and category preferences. Categories reset to defaults.\n\n"
    "_Next daily run starts cold (filter off until you vote again)._"
)


def _execute_clear(cfg, votes, reading_log):
    """Wipe D1 + in-memory state and reset categories to defaults."""
    state_store.clear_all_state()
    cfg["categories"] = list(DEFAULT_CATEGORIES)
    cfg["sent_ids"] = []
    votes["liked"].clear()
    votes["disliked"].clear()
    votes["last_batch"].clear()
    reading_log.setdefault("papers", {}).clear()


def handle_command(text, cfg, votes, reading_log):
    """Mutate cfg in place. Return (cfg_changed, reply, reply_markup).

    Only /digest and /reset reach GitHub Actions (via Worker dispatch).
    /stats, /help, /clear, votes, Read, and Delete are handled in worker/index.js.
    """
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()

    if cmd == "/digest":
        return False, format_weekly_digest(votes, reading_log), None

    if cmd == "/reset":
        cfg["categories"] = list(DEFAULT_CATEGORIES)
        return True, "Config reset to defaults.", None

    return False, None, None


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


def _sorted_papers(reading_log):
    return sorted(
        reading_log.get("papers", {}).values(),
        key=lambda entry: entry.get("status_ts") or entry.get("sent_ts") or entry.get("created_ts", ""),
        reverse=True,
    )


def format_weekly_digest(votes, reading_log):
    """Weekly digest from D1-backed ``reading_log`` (no JSON vote files)."""
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

def _primary_category_from_log(reading_log, key):
    entry = reading_log.get("papers", {}).get(key, {})
    cats = entry.get("categories") or []
    return cats[0] if cats else "_global"


def _primary_category_from_paper(paper):
    if paper.primary_category:
        return paper.primary_category
    return paper.categories[0] if paper.categories else "_global"


def normalized_category_bonus(category_prefs, category):
    if not category_prefs or category in ("", "_global"):
        return 0.0
    vals = list(category_prefs.values())
    if not vals:
        return 0.0
    raw = category_prefs.get(category, 0.0)
    span = max(max(vals), min(vals), 1.0)
    if span == 0:
        return 0.0
    return CATEGORY_PREF_ALPHA * (raw / span)


def handle_clear_confirm_callback(token, chat_id, callback, votes, reading_log, cfg):
    """Wipe D1 after the owner confirms /clear in the Worker UI.

    The Worker already answered the callback with a \"Clearing…\" toast; we only
    mutate state and edit the confirmation message here.
    Returns ``(votes_changed, reading_changed, cfg_changed)``."""
    parts = callback.get("data", "").split(":", 2)
    if len(parts) != 3 or parts[0] != "h" or parts[1] != "confirm_clear":
        return False, False, False

    message = callback.get("message") or {}
    message_id = message.get("message_id")
    source_chat_id = message.get("chat", {}).get("id") or chat_id
    if not message_id:
        return False, False, False

    _execute_clear(cfg, votes, reading_log)
    edit_message_text(
        token,
        source_chat_id,
        message_id,
        CLEAR_DONE_TEXT,
        reply_markup={"inline_keyboard": []},
    )
    return False, False, True


def apply_webhook_update(token, chat_id, update, cfg, votes, reading_log):
    """Process a single update dispatched by the Cloudflare Worker → GitHub Actions."""
    try:
        owner_id = int(chat_id)
    except (TypeError, ValueError):
        print("CHAT_ID is not an integer; skipping webhook update.", file=sys.stderr)
        return False, False, False

    cfg["last_update_id"] = max(cfg.get("last_update_id", 0), update.get("update_id", 0))

    cb = update.get("callback_query")
    if cb:
        if cb.get("from", {}).get("id") != owner_id:
            return False, False, False
        vote_mutated, reading_mutated, cfg_mutated = handle_clear_confirm_callback(
            token, chat_id, cb, votes, reading_log, cfg,
        )
        return cfg_mutated, vote_mutated, reading_mutated

    msg = update.get("message") or update.get("edited_message")
    if not msg or msg.get("from", {}).get("id") != owner_id:
        return False, False, False
    text = msg.get("text", "")
    if not text.startswith("/"):
        return False, False, False
    mutated, reply, reply_markup = handle_command(text, cfg, votes, reading_log)
    if reply:
        send_message(token, chat_id, reply, reply_markup=reply_markup)
    return mutated, False, False


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


def extract_topics(grok_summary):
    """Return (topics_list, body_without_topics_line).

    Looks for the first line starting with ``Topics:`` (case-insensitive) in the
    Grok summary, splits its tail on commas, and returns the keyword list plus
    the remaining body text with that line removed. Keywords are stripped and
    de-duplicated while preserving order. If no Topics line is found, returns
    ``([], grok_summary)`` unchanged.
    """
    if not grok_summary:
        return [], grok_summary or ""
    body_lines: list[str] = []
    topics: list[str] = []
    seen: set[str] = set()
    found = False
    for line in grok_summary.splitlines():
        stripped = line.strip()
        if not found and stripped.lower().startswith("topics:"):
            found = True
            tail = stripped.split(":", 1)[1] if ":" in stripped else ""
            for raw in tail.split(","):
                kw = raw.strip().strip(".;").strip()
                key = kw.lower()
                if kw and key not in seen:
                    seen.add(key)
                    topics.append(kw)
            continue
        body_lines.append(line)
    return topics, "\n".join(body_lines).strip("\n")


def format_grok_summary(text):
    """Bold the section labels (e.g. TL;DR) for Telegram Markdown."""
    lines = []
    for line in text.strip().splitlines():
        stripped = line.lstrip()
        leading = line[: len(line) - len(stripped)]
        matched = None
        for label in GROK_SUMMARY_LABELS:
            if stripped.startswith(label):
                matched = label
                break
        if matched is None:
            lines.append(escape_markdown(line))
            continue
        rest = stripped[len(matched):]
        lines.append(f"{leading}*{escape_markdown(matched)}*{escape_markdown(rest)}")
    return "\n".join(lines)


def format_paper(paper, index, grok_summary=None, score=None):
    title = escape_markdown(latex_to_unicode(paper.title.strip().replace("\n", " ")))
    names = [a.name for a in paper.authors]
    if not names:
        authors = ""
    elif len(names) == 1:
        authors = names[0]
    else:
        authors = f"{names[0]} et al."
    authors = escape_markdown(latex_to_unicode(authors))

    # Tag bar: paper-derived keywords from Grok's Topics line. When Grok is
    # unavailable, we omit the tag bar entirely rather than fall back to raw
    # arXiv category codes (which were the thing that made this hard to read).
    topics, grok_body = extract_topics(grok_summary) if grok_summary else ([], "")
    if topics:
        tag_bar = " ".join(f"\\[{escape_markdown(t)}]" for t in topics) + "\n"
    else:
        tag_bar = ""

    # Relevance score from the recommender. In cold start the filter isn't
    # running, so we don't have a personalized score — render "-" instead.
    if score is None or (isinstance(score, float) and math.isnan(score)):
        score_str = "-"
    else:
        score_str = f"{score:+.2f}"
    score_line = f"*Score:* {score_str}\n"

    prefix = (
        f"*[{index}] {title}*\n"
        f"_{authors}_\n"
        f"{score_line}"
        f"{tag_bar}\n"
    )
    suffix = f"\n\n[arXiv:{paper.get_short_id()}]({paper.entry_id})"
    if grok_summary:
        body = f"*Grok summary*\n{format_grok_summary(grok_body)}"
    else:
        abstract = latex_to_unicode(paper.summary.strip().replace("\n", " "))
        body = escape_markdown(abstract)
    available = max(TELEGRAM_SAFE_MESSAGE_CHARS - len(prefix) - len(suffix), 0)
    if len(body) > available:
        body = body[:available].rsplit(" ", 1)[0] + "…"
    return f"{prefix}{body}{suffix}"


def vote_keyboard(key):
    return {
        "inline_keyboard": [
            [
                {"text": "👍 Like", "callback_data": f"v:like:{key}"},
                {"text": "👎 Dislike", "callback_data": f"v:dislike:{key}"},
            ],
            [
                {"text": "Read", "callback_data": f"h:read_to_group:{key}"},
                {"text": "Delete", "callback_data": f"h:delete:{key}"},
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


def build_training_entries(votes, reading_log):
    """Liked/disliked training rows for :func:`recommender.fit_profile`."""
    now = datetime.now(timezone.utc)
    liked_keys = {v["key"] for v in votes["liked"]}
    disliked_keys = {v["key"] for v in votes["disliked"]}

    liked_entries = []
    for vote in votes["liked"]:
        liked_entries.append(
            {
                "text": vote["text"],
                "weight": vote_recency_weight(vote, now),
                "category": _primary_category_from_log(reading_log, vote["key"]),
            }
        )

    for entry in reading_log.get("papers", {}).values():
        if entry.get("status") != "saved" or not entry.get("text"):
            continue
        key = entry.get("key")
        if not key or key in disliked_keys:
            continue
        if key in liked_keys:
            continue
        ts_vote = {"ts": entry.get("status_ts") or entry.get("sent_ts")}
        liked_entries.append(
            {
                "text": entry["text"],
                "weight": READ_SIGNAL_WEIGHT * vote_recency_weight(ts_vote, now),
                "category": _primary_category_from_log(reading_log, key),
            }
        )

    disliked_entries = [
        {
            "text": vote["text"],
            "weight": vote_recency_weight(vote, now),
            "category": _primary_category_from_log(reading_log, vote["key"]),
        }
        for vote in votes["disliked"]
    ]
    return liked_entries, disliked_entries


def build_profile(votes, reading_log):
    liked_entries, disliked_entries = build_training_entries(votes, reading_log)
    return recommender.fit_profile(
        liked_entries,
        disliked_entries,
        tfidf_blend=TFIDF_BLEND,
    )


def score_paper(paper, profile, category_prefs):
    text = paper_text(paper)
    category = _primary_category_from_paper(paper)
    bonus = normalized_category_bonus(category_prefs, category)
    return recommender.score_profile(text, category, profile, category_pref_bonus=bonus)


def score_text_with_profile(text, categories, profile, category_prefs):
    category = categories[0] if categories else "_global"
    bonus = normalized_category_bonus(category_prefs, category)
    return recommender.score_profile(text, category, profile, category_pref_bonus=bonus)


def recent_theme_counts(reading_log, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    counts = {}
    for entry in reading_log.get("papers", {}).values():
        ts_str = entry.get("sent_ts") or entry.get("created_ts")
        if not ts_str:
            continue
        try:
            parsed = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed < cutoff:
            continue
        blob = f"{entry.get('title', '')}\n{entry.get('text', '')}"
        for topic in infer_topics(blob):
            counts[topic] = counts.get(topic, 0) + 1
    return counts


def apply_theme_weekly_cap(papers, reading_log):
    """Drop papers that would exceed per-theme weekly send cap."""
    counts = recent_theme_counts(reading_log, THEME_LOOKBACK_DAYS)
    kept = []
    batch_add = {}
    for paper in papers:
        topics = infer_topics(paper_text(paper))
        if topics and any(
            counts.get(topic, 0) + batch_add.get(topic, 0) >= THEME_WEEKLY_CAP for topic in topics
        ):
            continue
        kept.append(paper)
        for topic in topics:
            batch_add[topic] = batch_add.get(topic, 0) + 1
    if len(kept) < len(papers):
        print(
            f"Theme weekly cap ({THEME_WEEKLY_CAP}/{THEME_LOOKBACK_DAYS}d): "
            f"{len(papers)} → {len(kept)} papers."
        )
    return kept


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


def is_priority_paper(paper):
    return any(cat in PRIORITY_CATEGORIES for cat in paper.categories)


def select_priority_mix(papers, total_limit, priority_slots):
    """Target the configured priority/non-priority mix while preserving ranking within each pool."""
    if total_limit <= 0:
        return []

    priority_slots = min(max(priority_slots, 0), total_limit)
    other_slots = total_limit - priority_slots
    priority = [p for p in papers if is_priority_paper(p)]
    other = [p for p in papers if not is_priority_paper(p)]

    selected = priority[:priority_slots] + other[:other_slots]
    selected_keys = {paper_key(p) for p in selected}

    if len(selected) < total_limit:
        for paper in papers:
            if paper_key(paper) in selected_keys:
                continue
            selected.append(paper)
            selected_keys.add(paper_key(paper))
            if len(selected) >= total_limit:
                break

    return selected


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

    profile = build_profile(votes, reading_log) if filter_active(votes) else None
    category_prefs = state_store.load_category_preferences()
    scored = []
    for entry in candidates:
        if profile is not None:
            score_value = score_text_with_profile(
                entry["text"],
                entry.get("categories") or [],
                profile,
                category_prefs,
            )
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
        help="No-op (legacy flag). Telegram is handled by the Cloudflare Worker webhook.",
    )
    parser.add_argument(
        "--weekly-digest",
        action="store_true",
        help="Send the reading digest and skip arXiv fetch.",
    )
    parser.add_argument(
        "--apply-update",
        action="store_true",
        help=(
            "Process a single Telegram update from LITFEED_UPDATE_JSON "
            "(Worker → repository_dispatch): /digest, /reset, or /clear confirm."
        ),
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

    if args.apply_update:
        raw = os.environ.get("LITFEED_UPDATE_JSON", "").strip()
        if not raw:
            print("LITFEED_UPDATE_JSON is empty; nothing to apply.", file=sys.stderr)
            return
        try:
            update = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"LITFEED_UPDATE_JSON is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        cfg_changed, votes_changed, reading_changed = apply_webhook_update(
            token, chat_id, update, cfg, votes, reading_log,
        )
        save_config(cfg)  # last_update_id advances
        if votes_changed:
            save_votes(votes)
        print(
            f"Applied webhook update {update.get('update_id')}: "
            f"cfg={cfg_changed} votes={votes_changed} reading={reading_changed}"
        )
        return

    if args.commands_only:
        print(
            "--commands-only is a no-op: Telegram updates are handled by the "
            "Cloudflare Worker webhook (see worker/index.js).",
            file=sys.stderr,
        )
        return

    if args.weekly_digest:
        send_message(token, chat_id, format_weekly_digest(votes, reading_log))
        return

    # fetch + filter + alert
    try:
        papers = fetch_recent_papers(cfg["categories"], LOOKBACK_HOURS)
    except Exception as e:
        print(f"[arxiv] fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    sent_ids = set(cfg["sent_ids"])
    fresh = [p for p in papers if paper_key(p) not in sent_ids]
    score_by_key = {}

    category_prefs = state_store.load_category_preferences()

    if filter_active(votes):
        profile = build_profile(votes, reading_log)
        floor = current_relevance_floor(votes)
        scored = [(p, score_paper(p, profile, category_prefs)) for p in fresh]
        # Primary objective: personal relevance. Secondary: freshness.
        scored.sort(key=lambda ps: (ps[1], ps[0].published), reverse=True)
        scored = apply_diversity_guardrail(scored, DIVERSITY_MAX_JACCARD)
        kept = select_with_serendipity(scored, floor, max(len(scored), MAX_PAPERS_PER_RUN), SERENDIPITY_SLOTS)
        score_by_key = {paper_key(p): s for p, s in scored}
        print(
            f"Fetched {len(papers)} papers; {len(fresh)} new after dedup; "
            f"{len(kept)} candidates selected with {SERENDIPITY_SLOTS} serendipity slot(s) "
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

    before_theme_cap = len(to_send)
    to_send = apply_theme_weekly_cap(to_send, reading_log)

    before_priority_mix = len(to_send)
    to_send = select_priority_mix(to_send, MAX_PAPERS_PER_RUN, PRIORITY_PAPERS_PER_RUN)
    priority_count = sum(1 for paper in to_send if is_priority_paper(paper))
    print(
        f"Priority mix ({PRIORITY_PAPERS_PER_RUN}/{MAX_PAPERS_PER_RUN} target): "
        f"{before_priority_mix} → {len(to_send)} selected; {priority_count} priority."
    )

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
        score = score_by_key.get(key)  # None in cold start; format_paper renders "-"
        msg_id = send_message(
            token, chat_id, format_paper(paper, i, grok_summary=grok_summary, score=score),
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
        # save_config writes the trimmed sent_ids ring buffer (and last_update_id)
        # to D1; save_votes replaces the last_batch table; per-paper reading_log
        # entries were already upserted by record_sent_paper above.
        save_config(cfg)
        votes["last_batch"] = last_batch
        save_votes(votes)
        print(f"Sent {len(newly_sent)} messages.")


if __name__ == "__main__":
    main()
