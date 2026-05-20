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


class _FakeEncoder:
    """Deterministic stand-in for the sentence-transformer (no torch/download)."""

    def encode(self, docs, show_progress_bar=False):
        import numpy as np

        return np.array([[float(len(d)), 1.0, 2.0] for d in docs])


def test_embedding_branch_no_nan_when_one_side_empty(monkeypatch):
    """A per-category model with likes but no dislikes (or vice versa) must not
    produce a NaN centroid — that crashed cosine_similarity in production."""
    import numpy as np

    monkeypatch.delenv("LITFEED_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(recommender, "_get_encoder", _FakeEncoder)

    branch = recommender._fit_embedding_branch(["graph neural networks"], [], [1.0], [])
    assert branch is not None
    assert not np.isnan(branch["liked_centroid"]).any()
    assert not np.isnan(branch["disliked_centroid"]).any()
    assert np.isfinite(recommender._score_embedding_branch("deep learning", branch))
