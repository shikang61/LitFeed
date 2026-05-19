"""Weekly digest formatting and deep-read selection."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import main as m
from conftest import iso_days_ago


def test_infer_topics_matches_keywords():
    topics = m.infer_topics("A tokamak plasma fusion simulation study")
    assert "plasma" in topics


def test_format_weekly_digest_includes_recent_saved_and_deep_read(
    utc_now, reading_log_entry, active_votes
):
    now = utc_now
    reading_log = {
        "papers": {
            "recent_saved": reading_log_entry(
                "recent_saved",
                title="Tokamak *edge* modes",
                text="tokamak plasma edge stability",
                status="saved",
                status_ts=iso_days_ago(now, 2),
                sent_ts=iso_days_ago(now, 3),
                url="https://arxiv.org/abs/recent_saved",
            ),
            "old_sent": reading_log_entry(
                "old_sent",
                title="Old paper",
                text="portfolio trading",
                status="sent",
                sent_ts=iso_days_ago(now, 30),
            ),
            "recent_sent": reading_log_entry(
                "recent_sent",
                title="Recent sent",
                text="machine learning neural",
                status="sent",
                sent_ts=iso_days_ago(now, 1),
                score=0.1,
            ),
        }
    }

    with patch("main.state_store.load_category_preferences", return_value={}):
        text = m.format_weekly_digest(active_votes, reading_log)

    assert "*Weekly reading digest*" in text
    assert "Saved: 1" in text
    assert "Themes:" in text
    assert "plasma" in text.lower() or "machine" in text.lower()
    assert "*Deep read pick*" in text
    assert "recent_saved" in text
    assert "*Unread queue*" in text
    assert "Tokamak" in text  # markdown-escaped asterisks in title


def test_choose_deep_read_prefers_saved_bonus(utc_now, reading_log_entry, active_votes):
    now = utc_now
    reading_log = {
        "papers": {
            "sent_only": reading_log_entry(
                "sent_only",
                text="machine learning neural transformer",
                status="sent",
                sent_ts=iso_days_ago(now, 1),
                score=0.9,
            ),
            "saved_one": reading_log_entry(
                "saved_one",
                text="machine learning neural network",
                status="saved",
                status_ts=iso_days_ago(now, 2),
                sent_ts=iso_days_ago(now, 3),
                score=0.1,
            ),
        }
    }

    with patch("main.state_store.load_category_preferences", return_value={}):
        pick = m.choose_deep_read_candidate(active_votes, reading_log)

    assert pick is not None
    assert pick["key"] == "saved_one"


def test_choose_deep_read_cold_start_uses_score(utc_now, reading_log_entry, cold_start_votes):
    reading_log = {
        "papers": {
            "low": reading_log_entry("low", text="a", status="sent", score=0.1, sent_ts=iso_days_ago(utc_now, 1)),
            "high": reading_log_entry("high", text="b", status="sent", score=0.9, sent_ts=iso_days_ago(utc_now, 1)),
        }
    }
    with patch("main.state_store.load_category_preferences", return_value={}):
        pick = m.choose_deep_read_candidate(cold_start_votes, reading_log)
    assert pick["key"] == "high"
