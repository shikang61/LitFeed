"""state_store helpers with mocked D1 (no network)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import state_store


def test_record_vote_rejects_invalid_bucket():
    with pytest.raises(ValueError, match="bucket"):
        state_store.record_vote("k", "text", "maybe", "2026-01-01T00:00:00+00:00")


def test_load_reading_log_parses_categories_json():
    rows = [
        {
            "paper_key": "arxiv:2401.00001",
            "title": "T",
            "url": "https://arxiv.org/abs/2401.00001",
            "text": "body",
            "categories": '["physics.plasm-ph","cs.LG"]',
            "score": 0.5,
            "status": "sent",
            "status_ts": None,
            "sent_ts": "2026-05-01T00:00:00+00:00",
            "created_ts": "2026-05-01T00:00:00+00:00",
            "grok_summary": None,
        }
    ]
    with patch("state_store._d1.query", return_value=rows):
        log = state_store.load_reading_log()
    entry = log["papers"]["arxiv:2401.00001"]
    assert entry["categories"] == ["physics.plasm-ph", "cs.LG"]
    assert entry["score"] == 0.5
