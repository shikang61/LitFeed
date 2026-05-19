"""Selection pipeline and vote/training helpers in main.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import main as m


def test_filter_active_requires_min_votes_per_side(cold_start_votes, active_votes):
    assert m.filter_active(cold_start_votes) is False
    assert m.filter_active(active_votes) is True


def test_current_relevance_floor_early_window(active_votes):
    # Exactly MIN_VOTES_PER_SIDE on each side → early-active permissive floor.
    votes = {
        "liked": active_votes["liked"][:10],
        "disliked": active_votes["disliked"][:10],
        "last_batch": {},
    }
    assert m.current_relevance_floor(votes) == m.EARLY_ACTIVE_RELEVANCE_FLOOR

    # Early window ends once *both* sides reach MIN + EARLY_ACTIVE_EXTRA (15).
    for i in range(m.EARLY_ACTIVE_EXTRA_VOTES):
        votes["liked"].append(
            {"key": f"extra_like{i}", "text": "x", "ts": "2026-05-01T00:00:00+00:00"}
        )
        votes["disliked"].append(
            {"key": f"extra_dis{i}", "text": "y", "ts": "2026-05-01T00:00:00+00:00"}
        )
    assert m.current_relevance_floor(votes) == 0.0


def test_vote_recency_weight_decays():
    now = datetime(2026, 5, 19, tzinfo=timezone.utc)
    recent = {"ts": "2026-05-18T00:00:00+00:00"}
    old = {"ts": "2026-03-01T00:00:00+00:00"}
    assert m.vote_recency_weight(recent, now) > m.vote_recency_weight(old, now)
    assert m.vote_recency_weight({}, now) == 1.0


def test_build_training_entries_includes_saved_reading_log(reading_log_entry):
    votes = {
        "liked": [{"key": "v1", "text": "liked text", "ts": "2026-05-01T00:00:00+00:00"}],
        "disliked": [],
        "last_batch": {},
    }
    reading_log = {
        "papers": {
            "saved1": reading_log_entry(
                "saved1",
                status="saved",
                text="tokamak plasma fusion simulation",
                categories=["physics.plasm-ph"],
                status_ts="2026-05-10T00:00:00+00:00",
            ),
            "also_liked": reading_log_entry(
                "v1",
                status="saved",
                text="duplicate key should not double-count",
            ),
        }
    }
    liked, disliked = m.build_training_entries(votes, reading_log)
    assert len(disliked) == 0
    keys = {e["text"] for e in liked}
    assert "liked text" in keys
    assert any("tokamak" in t for t in keys)
    saved_rows = [e for e in liked if "tokamak" in e["text"]]
    assert saved_rows
    # Saved signal uses READ_SIGNAL_WEIGHT × recency (≤ 1.5 when recent).
    assert saved_rows[0]["weight"] > 1.0


def test_apply_diversity_guardrail_drops_near_duplicates(make_paper):
    p1 = make_paper("1", title="Magnetic reconnection", summary="tokamak plasma edge")
    p2 = make_paper("2", title="Magnetic reconnection", summary="tokamak plasma edge")
    p3 = make_paper("3", title="Portfolio optimization", summary="trading volatility")
    scored = [(p1, 0.9), (p2, 0.8), (p3, 0.7)]
    kept = m.apply_diversity_guardrail(scored, threshold=0.85)
    assert len(kept) == 2
    assert kept[0][0].get_short_id() == "1"
    assert kept[1][0].get_short_id() == "3"


def test_select_priority_mix_respects_slots(make_paper):
    priority = make_paper("p1", categories=["physics.plasm-ph"])
    other = make_paper("o1", categories=["cs.LG"])
    papers = [other, priority, make_paper("o2", categories=["stat.ML"])]
    selected = m.select_priority_mix(papers, total_limit=2, priority_slots=1)
    assert len(selected) == 2
    assert selected[0].get_short_id() == "p1"
    assert selected[1].get_short_id() == "o1"


def test_select_with_serendipity_adds_near_miss(make_paper):
    above = make_paper("a", title="above floor")
    below = make_paper("b", title="wildcard slot")
    scored = [(above, 0.5), (below, -0.1)]
    picked = m.select_with_serendipity(scored, floor=0.0, total_limit=2, slots=1)
    keys = {m.paper_key(p) for p in picked}
    assert keys == {"a", "b"}


def test_cap_per_category_and_total(make_paper):
    papers = [
        make_paper("1", categories=["cs.LG"]),
        make_paper("2", categories=["cs.LG"]),
        make_paper("3", categories=["stat.ML"]),
    ]
    capped = m.cap_per_category(papers, limit=1)
    assert len(capped) == 2
    assert m.cap_total(papers, 2) == papers[:2]


def test_apply_theme_weekly_cap(make_paper, reading_log_entry, utc_now):
    ts = (utc_now - timedelta(days=1)).isoformat(timespec="seconds")
    reading_log = {
        "papers": {
            f"old{i}": reading_log_entry(
                f"old{i}",
                text="tokamak plasma fusion mhd",
                sent_ts=ts,
            )
            for i in range(m.THEME_WEEKLY_CAP)
        }
    }
    new_paper = make_paper(
        "new",
        title="Another tokamak paper",
        summary="plasma fusion tokamak mhd simulation",
    )
    kept = m.apply_theme_weekly_cap([new_paper], reading_log)
    assert kept == []


def test_normalized_category_bonus_scales():
    prefs = {"physics.plasm-ph": 10.0, "cs.LG": -5.0}
    bonus = m.normalized_category_bonus(prefs, "physics.plasm-ph")
    assert bonus > 0
    assert m.normalized_category_bonus(prefs, "_global") == 0.0
