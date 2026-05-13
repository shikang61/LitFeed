"""Daily arXiv → Telegram alerter.

Fetches papers submitted in the last 24h from configured arXiv categories,
filters by keyword (title/abstract, case-insensitive), and posts each match
to a Telegram chat as a Markdown-formatted message.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import arxiv
import requests

CATEGORIES = [
    "physics.plasm-ph",
    "physics.comp-ph",
    "q-fin.CP",
    "q-fin.PR",
]

KEYWORDS = [
    # Physics / computing
    "AMReX",
    "Grad-Shafranov",
    "EFIT++",
    "VMEC",
    "nuclear fusion",
    "plasma simulation",
    "high-performance computing",
    # Finance
    "stochastic calculus",
    "volatility modeling",
    "GARCH",
    "Heston",
    "Merton",
    "derivatives pricing",
    "arbitrage",
]

LOOKBACK_HOURS = 24
SNIPPET_CHARS = 300
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def fetch_recent_papers(categories, hours):
    """Return arxiv.Result list submitted within the last `hours` across categories."""
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
            # Results are sorted descending — stop once past cutoff.
            break
        recent.append(result)
    return recent


def matches_keywords(paper, keywords):
    haystack = f"{paper.title}\n{paper.summary}".lower()
    return any(kw.lower() in haystack for kw in keywords)


def escape_markdown(text):
    """Escape characters that break Telegram legacy Markdown parsing."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def format_message(paper):
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


def send_telegram_message(token, chat_id, text):
    url = TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, data=payload, timeout=20)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[telegram] send failed: {e}", file=sys.stderr)
        return False


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        print("Missing TELEGRAM_TOKEN or CHAT_ID env vars.", file=sys.stderr)
        sys.exit(1)

    try:
        papers = fetch_recent_papers(CATEGORIES, LOOKBACK_HOURS)
    except Exception as e:
        print(f"[arxiv] fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    matched = [p for p in papers if matches_keywords(p, KEYWORDS)]
    print(f"Fetched {len(papers)} papers; {len(matched)} matched keywords.")

    if not matched:
        return

    sent = 0
    for paper in matched:
        if send_telegram_message(token, chat_id, format_message(paper)):
            sent += 1
        time.sleep(1)  # avoid Telegram rate limits
    print(f"Sent {sent}/{len(matched)} messages.")


if __name__ == "__main__":
    main()
