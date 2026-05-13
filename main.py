"""Daily arXiv → Telegram alerter with Telegram-driven config.

Each run:
  1. Polls Telegram for new /commands and mutates config.json accordingly.
  2. Fetches arXiv papers from configured categories submitted in the last
     LOOKBACK_HOURS and alerts on each.

Commands (owner only):
  /list                  → show current categories
  /add_cat <arxiv.cat>   → add arXiv category
  /rm_cat <arxiv.cat>    → remove arXiv category
  /reset                 → restore defaults
  /help                  → command list
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CATEGORIES = [
    "physics.plasm-ph",
    "physics.comp-ph",
    "q-fin.CP",
    "q-fin.PR",
]

LOOKBACK_HOURS = 36
SNIPPET_CHARS = 300
MAX_SENT_IDS = 500
TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"


# ---------- config ----------

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


def send_message(token, chat_id, text, markdown=True):
    params = {"chat_id": chat_id, "text": text}
    if markdown:
        params["parse_mode"] = "Markdown"
        params["disable_web_page_preview"] = False
    return telegram_call(token, "sendMessage", **params) is not None


def fetch_updates(token, offset):
    """Return list of new updates with id > offset."""
    result = telegram_call(token, "getUpdates", offset=offset + 1, timeout=0)
    if result is None or not result.get("ok"):
        return []
    return result.get("result", [])


# ---------- commands ----------

def handle_command(text, cfg):
    """Mutate cfg in place. Return (changed: bool, reply: str)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/list", "/list@",):
        return False, _format_list(cfg)

    if cmd == "/help":
        return False, (
            "*Commands*\n"
            "/list — show config\n"
            "/add\\_cat <arxiv.cat> — add category\n"
            "/rm\\_cat <arxiv.cat> — remove category\n"
            "/reset — restore defaults\n"
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

    return False, None  # not a recognised command — ignore silently


def _format_list(cfg):
    cats = "\n".join(f"• `{c}`" for c in cfg["categories"]) or "_(none)_"
    return f"*Categories*\n{cats}"


def process_telegram_commands(token, chat_id, cfg):
    """Poll Telegram, apply owner commands. Returns True if cfg mutated."""
    try:
        owner_id = int(chat_id)
    except (TypeError, ValueError):
        print("CHAT_ID is not an integer; skipping command processing.", file=sys.stderr)
        return False

    updates = fetch_updates(token, cfg["last_update_id"])
    changed = False

    for upd in updates:
        cfg["last_update_id"] = max(cfg["last_update_id"], upd.get("update_id", 0))
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        sender = msg.get("from", {}).get("id")
        if sender != owner_id:
            continue  # ignore everyone except owner
        text = msg.get("text", "")
        if not text.startswith("/"):
            continue
        mutated, reply = handle_command(text, cfg)
        if reply:
            send_message(token, chat_id, reply)
        if mutated:
            changed = True

    return changed


# ---------- arxiv ----------

def fetch_recent_papers(categories, hours):
    if not categories:
        return []
    import arxiv  # lazy import; not needed in --commands-only mode
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    query = " OR ".join(f"cat:{c}" for c in categories)

    client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
    search = arxiv.Search(
        query=query,
        max_results=200,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    recent = []
    for result in client.results(search):
        submitted = result.published
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=timezone.utc)
        if submitted < cutoff:
            break
        recent.append(result)
    return recent


def paper_key(paper):
    """Stable ID for dedup; strip version suffix so v1/v2 don't both alert."""
    sid = paper.get_short_id()
    return sid.rsplit("v", 1)[0] if "v" in sid else sid


def escape_markdown(text):
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def format_paper(paper):
    title = escape_markdown(paper.title.strip().replace("\n", " "))
    authors = escape_markdown(", ".join(a.name for a in paper.authors[:5]))
    if len(paper.authors) > 5:
        authors += " et al."
    abstract = paper.summary.strip().replace("\n", " ")
    if len(abstract) > SNIPPET_CHARS:
        abstract = abstract[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
    abstract = escape_markdown(abstract)
    categories = ", ".join(paper.categories)
    return (
        f"*{title}*\n"
        f"_{authors}_\n"
        f"`{categories}`\n\n"
        f"{abstract}\n\n"
        f"[arXiv:{paper.get_short_id()}]({paper.entry_id})"
    )


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commands-only",
        action="store_true",
        help="Only process queued Telegram commands; skip arXiv fetch + alerts.",
    )
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID env vars.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()

    # 1. process pending /commands
    config_changed = process_telegram_commands(token, chat_id, cfg)
    # save always so last_update_id advances even when no mutation
    save_config(cfg)
    if config_changed:
        print("Config mutated by Telegram command(s) this run.")

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
    print(f"Fetched {len(papers)} papers; {len(fresh)} new after dedup.")

    if not papers:
        run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        send_message(
            token,
            chat_id,
            f"_No papers fetched from arXiv_\nRun: `{run_time}`",
        )

    newly_sent = []
    for paper in fresh:
        if send_message(token, chat_id, format_paper(paper)):
            newly_sent.append(paper_key(paper))
        time.sleep(1)  # be polite to Telegram

    if newly_sent:
        cfg["sent_ids"] = (cfg["sent_ids"] + newly_sent)[-MAX_SENT_IDS:]
        save_config(cfg)
        print(f"Sent {len(newly_sent)} messages.")


if __name__ == "__main__":
    main()
