"""Shared fixtures for LitFeed unit tests."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

# Fast, deterministic recommender tests (no sentence-transformers download).
os.environ.setdefault("LITFEED_DISABLE_EMBEDDINGS", "1")


@pytest.fixture
def utc_now():
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


def iso_days_ago(now: datetime, days: float) -> str:
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


class SimplePaper:
    """Minimal paper stand-in for selection tests (no feedparser)."""

    def __init__(
        self,
        arxiv_id: str,
        *,
        title: str = "Title",
        summary: str = "Abstract body",
        categories=None,
        primary_category: str | None = None,
        published: datetime | None = None,
    ):
        self.entry_id = f"https://arxiv.org/abs/{arxiv_id}"
        self.title = title
        self.summary = summary
        self.categories = list(categories or ["cs.LG"])
        self.primary_category = primary_category or self.categories[0]
        self.published = published or datetime.now(timezone.utc)
        self.authors = []

    def get_short_id(self):
        return self.entry_id.rsplit("/abs/", 1)[-1]


@pytest.fixture
def make_paper():
    return SimplePaper


@pytest.fixture
def empty_votes():
    return {"liked": [], "disliked": [], "last_batch": {}}


@pytest.fixture
def cold_start_votes():
    """Fewer than MIN_VOTES_PER_SIDE on each side."""
    return {
        "liked": [{"key": "a", "text": "liked one", "ts": "2026-01-01T00:00:00+00:00"}],
        "disliked": [{"key": "b", "text": "disliked one", "ts": "2026-01-01T00:00:00+00:00"}],
        "last_batch": {},
    }


@pytest.fixture
def active_votes():
    """Enough votes to activate the preference filter."""
    liked = [
        {
            "key": f"like{i}",
            "text": f"machine learning neural network paper {i}",
            "ts": "2026-05-01T00:00:00+00:00",
        }
        for i in range(10)
    ]
    disliked = [
        {
            "key": f"dis{i}",
            "text": f"portfolio trading volatility risk paper {i}",
            "ts": "2026-05-01T00:00:00+00:00",
        }
        for i in range(10)
    ]
    return {"liked": liked, "disliked": disliked, "last_batch": {}}


@pytest.fixture
def reading_log_entry():
    def _make(
        key: str,
        *,
        title: str = "Paper",
        text: str = "Title\n\nAbstract",
        status: str = "sent",
        categories=None,
        sent_ts: str | None = None,
        status_ts: str | None = None,
        score: float = 0.0,
        url: str | None = None,
    ):
        entry = {
            "key": key,
            "title": title,
            "text": text,
            "status": status,
            "categories": categories or ["physics.plasm-ph"],
            "score": score,
            "created_ts": sent_ts or "2026-05-10T00:00:00+00:00",
        }
        if sent_ts:
            entry["sent_ts"] = sent_ts
        if status_ts:
            entry["status_ts"] = status_ts
        if url:
            entry["url"] = url
        return entry

    return _make
