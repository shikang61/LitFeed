"""Paper relevance models: TF-IDF + optional sentence embeddings.

Train on liked/disliked (title + abstract). Score by blended cosine distance
to liked vs disliked centroids, with optional per-category models.

sklearn / sentence-transformers are imported lazily so a minimal import of
this module does not require them installed.
"""

from __future__ import annotations

import functools
import os

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_TFIDF_BLEND = 0.5  # weight on TF-IDF branch; embedding gets the rest
MIN_DOCS_PER_CATEGORY_MODEL = 2  # min liked *or* disliked rows to fit a category model


def _env_disable_embeddings() -> bool:
    return os.environ.get("LITFEED_DISABLE_EMBEDDINGS", "").lower() in ("1", "true", "yes")


@functools.lru_cache(maxsize=1)
def _get_encoder():
    """Load the sentence-transformer once per process. Re-instantiating it on
    every fit/score call reloads weights and re-checks the HF hub each time."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


# ---------- TF-IDF (legacy single-model API) ----------


def fit(liked_docs, disliked_docs, liked_weights=None, disliked_weights=None):
    """Build TF-IDF model from text docs. Returns dict consumed by ``score``."""
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer

    corpus = list(liked_docs) + list(disliked_docs)
    vectorizer = TfidfVectorizer(
        lowercase=True,
        stop_words="english",
        ngram_range=(1, 2),
        min_df=1,
        max_df=0.95,
        sublinear_tf=True,
    )
    matrix = vectorizer.fit_transform(corpus)
    n_liked = len(liked_docs)
    liked_matrix = matrix[:n_liked]
    disliked_matrix = matrix[n_liked:]

    if liked_weights is None:
        liked_weights = np.ones(n_liked, dtype=float)
    if disliked_weights is None:
        disliked_weights = np.ones(len(disliked_docs), dtype=float)

    liked_weights = np.asarray(liked_weights, dtype=float)
    disliked_weights = np.asarray(disliked_weights, dtype=float)

    liked_centroid = liked_matrix.multiply(liked_weights[:, None]).sum(axis=0) / max(
        liked_weights.sum(), 1e-9
    )
    disliked_centroid = disliked_matrix.multiply(disliked_weights[:, None]).sum(axis=0) / max(
        disliked_weights.sum(), 1e-9
    )
    return {
        "vectorizer": vectorizer,
        "liked_centroid": np.asarray(liked_centroid),
        "disliked_centroid": np.asarray(disliked_centroid),
    }


def score(text, model):
    """Higher = closer to liked, further from disliked (TF-IDF only)."""
    from sklearn.metrics.pairwise import cosine_similarity

    vec = model["vectorizer"].transform([text])
    sim_liked = cosine_similarity(vec, model["liked_centroid"])[0, 0]
    sim_disliked = cosine_similarity(vec, model["disliked_centroid"])[0, 0]
    return float(sim_liked - sim_disliked)


def explain(text, model, top_n=5):
    """Return top positive contributing terms for this text."""
    import numpy as np

    vec = model["vectorizer"].transform([text]).toarray()[0]
    centroid_delta = (model["liked_centroid"] - model["disliked_centroid"]).ravel()
    contrib = vec * centroid_delta
    if not np.any(contrib):
        return []

    top_idx = np.argsort(contrib)[::-1]
    feature_names = model["vectorizer"].get_feature_names_out()
    out = []
    for idx in top_idx:
        value = float(contrib[idx])
        if value <= 0:
            break
        out.append((feature_names[idx], value))
        if len(out) >= top_n:
            break
    return out


# ---------- Blended profile (TF-IDF + embeddings, per-category) ----------


def _weighted_centroid(vectors, weights):
    import numpy as np

    w = np.asarray(weights, dtype=float)
    if vectors.shape[0] == 0:
        return None
    if w.sum() <= 0:
        w = np.ones(len(w), dtype=float)
    return (vectors * w[:, None]).sum(axis=0) / w.sum()


def _fit_tfidf_branch(liked_docs, disliked_docs, liked_weights, disliked_weights):
    if not liked_docs and not disliked_docs:
        return None
    if not liked_docs:
        liked_docs = [""]
        liked_weights = [0.0]
    if not disliked_docs:
        disliked_docs = [""]
        disliked_weights = [0.0]
    return fit(liked_docs, disliked_docs, liked_weights, disliked_weights)


def _fit_embedding_branch(liked_docs, disliked_docs, liked_weights, disliked_weights):
    if _env_disable_embeddings() or (not liked_docs and not disliked_docs):
        return None
    try:
        encoder = _get_encoder()
    except ImportError:
        return None

    if not liked_docs:
        liked_docs, liked_weights = [""], [0.0]
    if not disliked_docs:
        disliked_docs, disliked_weights = [""], [0.0]
    liked_vecs = encoder.encode(list(liked_docs), show_progress_bar=False)
    disliked_vecs = encoder.encode(list(disliked_docs), show_progress_bar=False)
    liked_c = _weighted_centroid(liked_vecs, liked_weights or [1.0] * len(liked_docs))
    disliked_c = _weighted_centroid(disliked_vecs, disliked_weights or [1.0] * len(disliked_docs))
    if liked_c is None or disliked_c is None:
        return None
    return {"liked_centroid": liked_c, "disliked_centroid": disliked_c}


def _score_tfidf_branch(text, branch):
    if branch is None:
        return None
    return score(text, branch)


def _score_embedding_branch(text, branch):
    if branch is None:
        return None
    from sklearn.metrics.pairwise import cosine_similarity

    encoder = _get_encoder()
    vec = encoder.encode([text], show_progress_bar=False)
    sim_liked = cosine_similarity(vec, branch["liked_centroid"].reshape(1, -1))[0, 0]
    sim_disliked = cosine_similarity(vec, branch["disliked_centroid"].reshape(1, -1))[0, 0]
    return float(sim_liked - sim_disliked)


def _blend_scores(tfidf_score, embedding_score, tfidf_blend: float) -> float:
    parts = []
    weights = []
    if tfidf_score is not None:
        parts.append(tfidf_score)
        weights.append(tfidf_blend)
    if embedding_score is not None:
        parts.append(embedding_score)
        weights.append(1.0 - tfidf_blend if tfidf_score is not None else 1.0)
    if not parts:
        return 0.0
    wsum = sum(weights)
    return sum(p * w for p, w in zip(parts, weights)) / wsum


def _fit_branch(liked_docs, disliked_docs, liked_weights, disliked_weights):
    tfidf = _fit_tfidf_branch(liked_docs, disliked_docs, liked_weights, disliked_weights)
    emb = _fit_embedding_branch(liked_docs, disliked_docs, liked_weights, disliked_weights)
    if tfidf is None and emb is None:
        return None
    return {"tfidf": tfidf, "embedding": emb}


def score_branch(text, branch, tfidf_blend: float) -> float | None:
    if branch is None:
        return None
    return _blend_scores(
        _score_tfidf_branch(text, branch.get("tfidf")),
        _score_embedding_branch(text, branch.get("embedding")),
        tfidf_blend,
    )


def fit_profile(
    liked_entries,
    disliked_entries,
    *,
    tfidf_blend: float = DEFAULT_TFIDF_BLEND,
    min_docs_per_category: int = MIN_DOCS_PER_CATEGORY_MODEL,
):
    """Build global + per-category blended models.

    Each entry is ``{text, weight, category}`` where ``category`` is the
    primary arXiv category code (or ``_global``).
    """
    def split(entries):
        by_cat: dict[str, list] = {}
        for e in entries:
            cat = e.get("category") or "_global"
            by_cat.setdefault(cat, []).append(e)
        return by_cat

    liked_by = split(liked_entries)
    disliked_by = split(disliked_entries)
    all_cats = set(liked_by) | set(disliked_by)

    def corpus_for(cat, side_by):
        rows = side_by.get(cat, [])
        return (
            [r["text"] for r in rows],
            [r.get("weight", 1.0) for r in rows],
        )

    global_liked, global_lw = corpus_for("_global", liked_by)
    for cat in all_cats:
        if cat == "_global":
            continue
        docs, weights = corpus_for(cat, liked_by)
        global_liked.extend(docs)
        global_lw.extend(weights)
    global_disliked, global_dw = corpus_for("_global", disliked_by)
    for cat in all_cats:
        if cat == "_global":
            continue
        docs, weights = corpus_for(cat, disliked_by)
        global_disliked.extend(docs)
        global_dw.extend(weights)

    profile = {
        "tfidf_blend": tfidf_blend,
        "global": _fit_branch(global_liked, global_disliked, global_lw, global_dw),
        "by_category": {},
    }

    for cat in all_cats:
        if cat == "_global":
            continue
        ld, lw = corpus_for(cat, liked_by)
        dd, dw = corpus_for(cat, disliked_by)
        if len(ld) < min_docs_per_category and len(dd) < min_docs_per_category:
            continue
        branch = _fit_branch(ld, dd, lw, dw)
        if branch is not None:
            profile["by_category"][cat] = branch

    return profile


def score_profile(text, primary_category, profile, *, category_pref_bonus: float = 0.0) -> float:
    """Score text using category model when available, else global."""
    if profile is None or profile.get("global") is None:
        return 0.0
    blend = profile.get("tfidf_blend", DEFAULT_TFIDF_BLEND)
    cat_branch = profile.get("by_category", {}).get(primary_category)
    if cat_branch is not None:
        raw = score_branch(text, cat_branch, blend)
    else:
        raw = score_branch(text, profile["global"], blend)
    if raw is None:
        raw = 0.0
    return float(raw) + category_pref_bonus
