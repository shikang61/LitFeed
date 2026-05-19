"""Paper message formatting helpers."""

from __future__ import annotations

import main as m
from conftest import SimplePaper


def test_paper_key_strips_version_suffix():
    paper = SimplePaper("2401.12345v2")
    assert m.paper_key(paper) == "2401.12345"


def test_extract_topics_parses_grok_line():
    raw = "TL;DR: Short summary.\nTopics: plasma, tokamak, fusion.\nMore detail."
    topics, body = m.extract_topics(raw)
    assert topics == ["plasma", "tokamak", "fusion"]
    assert "Topics:" not in body
    assert "TL;DR" in body


def test_format_paper_cold_start_score_dash():
    paper = SimplePaper("2401.00001", title="Test paper", summary="An abstract.")
    msg = m.format_paper(paper, index=1, grok_summary=None, score=None)
    assert "*Score:* -" in msg
    assert "Test paper" in msg
    assert "arxiv.org/abs/2401.00001" in msg


def test_format_paper_with_grok_topics_and_score():
    paper = SimplePaper("2401.00002", title="Plasma study", summary="Ignored when grok present.")
    grok = "TL;DR: Edge modes.\nTopics: plasma, fusion."
    msg = m.format_paper(paper, index=2, grok_summary=grok, score=0.42)
    assert "*Score:* +0.42" in msg
    assert "[plasma]" in msg
    assert "*Grok summary*" in msg
