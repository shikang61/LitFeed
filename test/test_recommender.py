"""Recommender TF-IDF / profile scoring (embeddings disabled in conftest)."""

from __future__ import annotations

import recommender


def test_fit_score_liked_beats_disliked_on_similar_text():
    liked = ["machine learning neural network optimization"]
    disliked = ["portfolio trading volatility risk options"]
    model = recommender.fit(liked, disliked)
    ml_score = recommender.score("deep learning with neural networks", model)
    fin_score = recommender.score("options trading and market volatility", model)
    assert ml_score > fin_score


def test_explain_returns_positive_terms():
    liked = ["magnetic reconnection plasma tokamak"]
    disliked = ["portfolio trading volatility"]
    model = recommender.fit(liked, disliked)
    terms = recommender.explain("tokamak plasma magnetic reconnection", model, top_n=3)
    assert terms
    assert all(weight > 0 for _, weight in terms)


def test_fit_profile_and_score_with_category_bonus():
    liked = [
        {"text": "plasma fusion tokamak", "weight": 1.0, "category": "physics.plasm-ph"},
        {"text": "gyrokinetic particle in cell", "weight": 1.0, "category": "physics.plasm-ph"},
    ]
    disliked = [
        {"text": "portfolio volatility trading", "weight": 1.0, "category": "q-fin.PM"},
        {"text": "options market risk", "weight": 1.0, "category": "q-fin.PM"},
    ]
    profile = recommender.fit_profile(liked, disliked, tfidf_blend=1.0)
    plasma_score = recommender.score_profile(
        "tokamak edge plasma stability",
        "physics.plasm-ph",
        profile,
        category_pref_bonus=0.05,
    )
    finance_score = recommender.score_profile(
        "tokamak edge plasma stability",
        "q-fin.PM",
        profile,
    )
    assert plasma_score > finance_score
