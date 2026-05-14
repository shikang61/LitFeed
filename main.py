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
  /vote                  → multi-select keyboard to batch-vote recent papers
  /stats                 → show vote counts + filter status
  /help                  → command list
"""

import argparse
import calendar
import json
import os
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
MAX_SENT_IDS = 500
MAX_SENT_CACHE = 500
MIN_VOTES_PER_SIDE = 10
PER_CATEGORY_LIMIT = 2
MAX_VOTE_KEYBOARD = 20    # papers shown per /vote message (Telegram keyboard limit)
VOTE_TITLE_CHARS = 35     # truncation for paper titles on /vote buttons
MAX_VOTE_SESSIONS = 10    # keep recent /vote sessions; drop the rest
TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"


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
        return {"liked": [], "disliked": [], "sent_cache": {}}
    with VOTES_PATH.open() as f:
        votes = json.load(f)
    votes.setdefault("liked", [])
    votes.setdefault("disliked", [])
    votes.setdefault("sent_cache", {})
    votes.setdefault("vote_sessions", {})  # /vote message_id → {papers, selections}
    return votes


def save_votes(votes):
    with VOTES_PATH.open("w") as f:
        json.dump(votes, f, indent=2, ensure_ascii=False)
        f.write("\n")


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

def handle_command(text, cfg, votes):
    """Mutate cfg in place. Return (cfg_changed: bool, reply: str | None)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/list", "/list@"):
        return False, _format_list(cfg)

    if cmd == "/stats":
        return False, _format_stats(votes)

    if cmd == "/help":
        return False, (
            "*Commands*\n"
            "/list — show categories\n"
            "/add\\_cat <arxiv.cat> — add category\n"
            "/rm\\_cat <arxiv.cat> — remove category\n"
            "/reset — restore default categories\n"
            "/vote — batch-vote recent papers via a keyboard\n"
            "/stats — vote counts + filter status\n"
            "/help — this message"
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


def _format_stats(votes):
    nl, nd = len(votes["liked"]), len(votes["disliked"])
    active = nl >= MIN_VOTES_PER_SIDE and nd >= MIN_VOTES_PER_SIDE
    status = "active" if active else f"cold start (need ≥{MIN_VOTES_PER_SIDE} each)"
    return f"*Votes*\n👍 {nl}\n👎 {nd}\n\n*Filter:* {status}"


# ---------- callback (votes) ----------

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


def _paper_title(votes, key):
    """First line (title) of a cached paper's text, or the key if uncached."""
    entry = votes["sent_cache"].get(key)
    if not entry:
        return key
    return entry["text"].split("\n\n", 1)[0].replace("\n", " ")


def build_vote_keyboard(votes, session):
    """Inline keyboard for a /vote session: one toggle per paper + Submit."""
    emoji = {"like": "👍", "dislike": "👎"}
    rows = []
    for key in session["papers"]:
        mark = emoji.get(session["selections"].get(key), "⚪")
        title = _paper_title(votes, key)
        if len(title) > VOTE_TITLE_CHARS:
            title = title[:VOTE_TITLE_CHARS].rstrip() + "…"
        rows.append([{"text": f"{mark} {title}", "callback_data": f"t:{key}"}])
    rows.append([{"text": "✅ Submit", "callback_data": "vs:submit"}])
    return {"inline_keyboard": rows}


def send_vote_keyboard(token, chat_id, votes):
    """Send a /vote multi-select message for recent unvoted papers.
    Mutates votes (adds a session). Returns True if a session was created."""
    voted = {v["key"] for v in votes["liked"]} | {v["key"] for v in votes["disliked"]}
    candidates = [k for k in reversed(votes["sent_cache"]) if k not in voted]
    candidates = candidates[:MAX_VOTE_KEYBOARD]
    if not candidates:
        send_message(token, chat_id, "_No unvoted papers in the cache._")
        return False

    session = {"papers": candidates, "selections": {}}
    msg_id = send_message(
        token, chat_id,
        "*Tap to mark 👍/👎, then Submit:*",
        reply_markup=build_vote_keyboard(votes, session),
    )
    if msg_id is None:
        return False

    votes["vote_sessions"][str(msg_id)] = session
    # keep only the most recent sessions; drop older ones
    excess = len(votes["vote_sessions"]) - MAX_VOTE_SESSIONS
    for old in list(votes["vote_sessions"])[:max(0, excess)]:
        del votes["vote_sessions"][old]
    return True


def _answer(token, cb_id, text=None):
    if text:
        telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id, text=text)
    else:
        telegram_call(token, "answerCallbackQuery", callback_query_id=cb_id)


def handle_callback(token, chat_id, callback, votes):
    """Mutate votes in place. Returns True if votes mutated.

    Dispatches three callback_data shapes:
      v:like:<key> / v:dislike:<key>  single per-paper vote button
      t:<key>                         /vote keyboard toggle
      vs:submit                       /vote keyboard submit
    """
    cb_id = callback["id"]
    data = callback.get("data", "")

    if data.startswith("v:"):
        return _handle_single_vote(token, cb_id, data, votes)
    if data.startswith("t:"):
        return _handle_toggle(token, chat_id, cb_id, data, callback, votes)
    if data == "vs:submit":
        return _handle_submit(token, chat_id, cb_id, callback, votes)

    _answer(token, cb_id)
    return False


def _handle_single_vote(token, cb_id, data, votes):
    try:
        _, action, key = data.split(":", 2)
    except ValueError:
        _answer(token, cb_id)
        return False
    if action not in ("like", "dislike"):
        _answer(token, cb_id)
        return False

    cache_entry = votes["sent_cache"].get(key)
    if not cache_entry:
        _answer(token, cb_id, "Paper not in cache (too old).")
        return False

    _record_vote(votes, key, cache_entry["text"], action)
    _answer(token, cb_id, f"Recorded {'👍' if action == 'like' else '👎'}")
    return True


def _handle_toggle(token, chat_id, cb_id, data, callback, votes):
    key = data.split(":", 1)[1]
    msg_id = callback.get("message", {}).get("message_id")
    session = votes["vote_sessions"].get(str(msg_id))
    if not session or key not in session["papers"]:
        _answer(token, cb_id, "This vote session has expired — send /vote again.")
        return False

    cur = session["selections"].get(key)
    nxt = {None: "like", "like": "dislike", "dislike": None}[cur]
    if nxt is None:
        session["selections"].pop(key, None)
    else:
        session["selections"][key] = nxt

    telegram_call(
        token, "editMessageReplyMarkup",
        chat_id=chat_id, message_id=msg_id,
        reply_markup=json.dumps(build_vote_keyboard(votes, session)),
    )
    _answer(token, cb_id)
    return True


def _handle_submit(token, chat_id, cb_id, callback, votes):
    msg_id = callback.get("message", {}).get("message_id")
    session = votes["vote_sessions"].pop(str(msg_id), None)
    if session is None:
        _answer(token, cb_id, "This vote session has expired — send /vote again.")
        return False

    n_like = n_dislike = 0
    for key, action in session["selections"].items():
        cache_entry = votes["sent_cache"].get(key)
        if not cache_entry:
            continue
        _record_vote(votes, key, cache_entry["text"], action)
        if action == "like":
            n_like += 1
        else:
            n_dislike += 1

    summary = f"Recorded 👍×{n_like} 👎×{n_dislike}"
    telegram_call(
        token, "editMessageText",
        chat_id=chat_id, message_id=msg_id, text=summary,
    )
    _answer(token, cb_id, summary)
    return True


def process_telegram_updates(token, chat_id, cfg, votes):
    """Poll Telegram; apply owner commands + vote callbacks.
    Returns (cfg_changed, votes_changed)."""
    try:
        owner_id = int(chat_id)
    except (TypeError, ValueError):
        print("CHAT_ID is not an integer; skipping update processing.", file=sys.stderr)
        return False, False

    updates = fetch_updates(token, cfg["last_update_id"])
    cfg_changed = False
    votes_changed = False

    for upd in updates:
        cfg["last_update_id"] = max(cfg["last_update_id"], upd.get("update_id", 0))

        cb = upd.get("callback_query")
        if cb:
            sender = cb.get("from", {}).get("id")
            if sender != owner_id:
                continue
            if handle_callback(token, chat_id, cb, votes):
                votes_changed = True
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
        if text.strip().split()[0].lower() in ("/vote", "/vote@"):
            if send_vote_keyboard(token, chat_id, votes):
                votes_changed = True
            continue
        mutated, reply = handle_command(text, cfg, votes)
        if reply:
            send_message(token, chat_id, reply)
        if mutated:
            cfg_changed = True

    return cfg_changed, votes_changed


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


def prune_sent_cache(votes):
    cache = votes["sent_cache"]
    excess = len(cache) - MAX_SENT_CACHE
    if excess <= 0:
        return
    for key in list(cache.keys())[:excess]:
        del cache[key]


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commands-only",
        action="store_true",
        help="Only process queued Telegram updates (commands + votes); skip arXiv fetch.",
    )
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID env vars.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    votes = load_votes()

    # 1. process pending /commands + callback votes
    cfg_changed, votes_changed = process_telegram_updates(token, chat_id, cfg, votes)
    save_config(cfg)  # always — last_update_id advances
    if votes_changed:
        save_votes(votes)
    if cfg_changed:
        print("Config mutated by Telegram command(s) this run.")
    if votes_changed:
        print("Votes mutated by callback(s) this run.")

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
            votes["sent_cache"][key] = {
                "text": paper_text(paper),
                "message_id": msg_id,
            }
        time.sleep(1)  # be polite to Telegram

    if newly_sent:
        cfg["sent_ids"] = (cfg["sent_ids"] + newly_sent)[-MAX_SENT_IDS:]
        save_config(cfg)
        prune_sent_cache(votes)
        save_votes(votes)
        print(f"Sent {len(newly_sent)} messages.")


if __name__ == "__main__":
    main()
