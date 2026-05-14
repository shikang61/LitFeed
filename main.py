"""Daily arXiv → Telegram alerter with a TF-IDF preference filter.

`python main.py` (run daily by GitHub Actions) fetches arXiv papers from the
configured categories announced in the last LOOKBACK_HOURS, applies the
preference filter (cold-start sends all), and alerts on each survivor with
👍/👎 buttons.

State (categories, sent_ids, votes, sent_cache) lives in Firestore via store.py.
Telegram /commands and vote callbacks are handled in real time by the Cloud Run
webhook (webhook.py), which reuses handle_command / handle_callback below.

Commands (owner only):
  /list                  → show current categories
  /add_cat <arxiv.cat>   → add arXiv category
  /rm_cat <arxiv.cat>    → remove arXiv category
  /reset                 → restore defaults
  /stats                 → show vote counts + filter status
  /help                  → command list
"""

import calendar
import json
import os
import sys
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import requests

import recommender
import store

_LATEX2TEXT = None


def latex_to_unicode(text):
    global _LATEX2TEXT
    if _LATEX2TEXT is None:
        from pylatexenc.latex2text import LatexNodes2Text  # lazy; webhook doesn't need it
        _LATEX2TEXT = LatexNodes2Text(keep_comments=False, math_mode="text")
    try:
        return _LATEX2TEXT.latex_to_text(text)
    except Exception:
        return text


DEFAULT_CATEGORIES = [
    "cs.AI",
    "cs.DS",
    "cs.LG",
    "cs.MA",
    "cs.NA",
    "cs.NE",
    "cs.SC",
    "econ.EM",
    "econ.TH",
    "math.MP",
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
    "stat.ML",
    "stat.AP",
]

LOOKBACK_HOURS = 36
ARXIV_RSS_URL = "https://rss.arxiv.org/rss/{category}"
ARXIV_MAX_ATTEMPTS = 4
ARXIV_BACKOFF_BASE = 30  # seconds; backoff is 30, 60, 120 between attempts
# arXiv throttles the default python-requests User-Agent; send a descriptive one.
ARXIV_USER_AGENT = "LitFeed/1.0 (https://github.com/shikang61/LitFeed)"
SNIPPET_CHARS = 300
MIN_VOTES_PER_SIDE = 10
PER_CATEGORY_LIMIT = 2
TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"


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


# ---------- commands ----------

def handle_command(text, store):
    """Apply an owner /command, writing any change to the store.
    Returns the reply string to send back, or None for unknown commands."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/list", "/list@"):
        return _format_list(store.get_config(DEFAULT_CATEGORIES)["categories"])

    if cmd == "/stats":
        return _format_stats(*store.vote_counts())

    if cmd == "/help":
        return (
            "*Commands*\n"
            "/list — show categories\n"
            "/add\\_cat <arxiv.cat> — add category\n"
            "/rm\\_cat <arxiv.cat> — remove category\n"
            "/reset — restore default categories\n"
            "/stats — vote counts + filter status\n"
            "/help — this message"
        )

    if cmd == "/add_cat":
        if not arg:
            return "Usage: `/add_cat <arxiv.category>`"
        cats = store.get_config(DEFAULT_CATEGORIES)["categories"]
        if arg in cats:
            return f"Category already present: `{arg}`"
        store.set_categories(cats + [arg])
        return f"Added category: `{arg}`"

    if cmd == "/rm_cat":
        if not arg:
            return "Usage: `/rm_cat <arxiv.category>`"
        cats = store.get_config(DEFAULT_CATEGORIES)["categories"]
        if arg not in cats:
            return f"Category not found: `{arg}`"
        store.set_categories([c for c in cats if c != arg])
        return f"Removed category: `{arg}`"

    if cmd == "/reset":
        store.set_categories(list(DEFAULT_CATEGORIES))
        return "Config reset to defaults."

    return None


def _format_list(categories):
    cats = "\n".join(f"• `{c}`" for c in categories) or "_(none)_"
    return f"*Categories*\n{cats}"


def _format_stats(n_liked, n_disliked):
    active = n_liked >= MIN_VOTES_PER_SIDE and n_disliked >= MIN_VOTES_PER_SIDE
    status = "active" if active else f"cold start (need ≥{MIN_VOTES_PER_SIDE} each)"
    return f"*Votes*\n👍 {n_liked}\n👎 {n_disliked}\n\n*Filter:* {status}"


# ---------- callback (votes) ----------

def handle_callback(token, callback, store):
    """Record an owner 👍/👎 vote callback in the store and answer the query."""
    cb_id = callback["id"]
    data = callback.get("data", "")

    if not data.startswith("v:"):
        telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id)
        return
    try:
        _, action, key = data.split(":", 2)
    except ValueError:
        telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id)
        return
    if action not in ("like", "dislike"):
        telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id)
        return

    cache_entry = store.get_sent_cache_entry(key)
    if not cache_entry:
        telegram_call(
            token, "answerCallbackQuery",
            callback_query_id=cb_id,
            text="Paper not in cache (too old).",
        )
        return

    bucket = "liked" if action == "like" else "disliked"
    store.set_vote(key, bucket, cache_entry["text"])  # doc id = key → overwrites

    emoji = "👍" if action == "like" else "👎"
    telegram_call(
        token, "answerCallbackQuery",
        callback_query_id=cb_id,
        text=f"Recorded {emoji}",
    )


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
    import feedparser  # lazy import
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


def format_paper(paper):
    title = escape_markdown(latex_to_unicode(paper.title.strip().replace("\n", " ")))
    names = [a.name for a in paper.authors]
    if len(names) <= 3:
        authors = ", ".join(names)
    else:
        authors = f"{names[0]}, {names[1]}, …, {names[-1]}"
    authors = escape_markdown(latex_to_unicode(authors))
    abstract = latex_to_unicode(paper.summary.strip().replace("\n", " "))
    if len(abstract) > SNIPPET_CHARS:
        abstract = abstract[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
    abstract = escape_markdown(abstract)
    categories = " ".join(f"\\[{c}]" for c in paper.categories)
    return (
        f"*{title}*\n"
        f"_{authors}_\n"
        f"{categories}\n\n"
        f"{abstract}\n\n"
        f"[arXiv:{paper.get_short_id()}]({paper.entry_id})"
    )


def vote_keyboard(key):
    return {
        "inline_keyboard": [[
            {"text": "👍 Like", "callback_data": f"v:like:{key}"},
            {"text": "👎 Dislike", "callback_data": f"v:dislike:{key}"},
        ]]
    }


# ---------- recommender wiring ----------

def filter_active(votes):
    return (
        len(votes["liked"]) >= MIN_VOTES_PER_SIDE
        and len(votes["disliked"]) >= MIN_VOTES_PER_SIDE
    )


def build_model(votes):
    return recommender.fit(
        [v["text"] for v in votes["liked"]],
        [v["text"] for v in votes["disliked"]],
    )


def cap_per_category(papers, limit):
    """Keep first `limit` papers per primary_category, preserve input order."""
    counts = {}
    kept = []
    for p in papers:
        cat = p.primary_category
        if counts.get(cat, 0) >= limit:
            continue
        counts[cat] = counts.get(cat, 0) + 1
        kept.append(p)
    return kept


# ---------- main ----------

def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID env vars.", file=sys.stderr)
        sys.exit(1)

    cfg = store.get_config(DEFAULT_CATEGORIES)
    votes = store.get_votes()

    # fetch + filter + alert
    try:
        papers = fetch_recent_papers(cfg["categories"], LOOKBACK_HOURS)
    except Exception as e:
        print(f"[arxiv] fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    sent_ids = set(cfg["sent_ids"])
    fresh = [p for p in papers if paper_key(p) not in sent_ids]

    if filter_active(votes):
        model = build_model(votes)
        scored = [(p, recommender.score(paper_text(p), model)) for p in fresh]
        scored.sort(key=lambda ps: ps[1], reverse=True)
        kept = [p for p, s in scored if s > 0]
        print(
            f"Fetched {len(papers)} papers; {len(fresh)} new after dedup; "
            f"{len(kept)} passed filter (threshold 0)."
        )
        to_send = kept
    else:
        print(
            f"Fetched {len(papers)} papers; {len(fresh)} new after dedup; "
            f"filter cold (likes={len(votes['liked'])}, dislikes={len(votes['disliked'])}) — sending all."
        )
        to_send = fresh

    before_cap = len(to_send)
    to_send = cap_per_category(to_send, PER_CATEGORY_LIMIT)
    print(f"Capped per-category ({PER_CATEGORY_LIMIT}): {before_cap} → {len(to_send)}.")

    if not papers:
        run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        send_message(
            token,
            chat_id,
            f"_No papers fetched from arXiv_\nRun: `{run_time}`",
        )

    newly_sent = []
    for paper in to_send:
        key = paper_key(paper)
        msg_id = send_message(
            token, chat_id, format_paper(paper),
            reply_markup=vote_keyboard(key),
        )
        if msg_id is not None:
            newly_sent.append(key)
            store.put_sent_cache(key, paper_text(paper), msg_id)
        time.sleep(1)  # be polite to Telegram

    if newly_sent:
        store.add_sent_ids(newly_sent)
        store.prune_sent_cache()
        print(f"Sent {len(newly_sent)} messages.")


if __name__ == "__main__":
    main()
