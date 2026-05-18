"""TF-IDF recommender via scikit-learn.

Train on liked/disliked (title + abstract). Score new paper by
`cos(paper, liked_centroid) - cos(paper, disliked_centroid)`.

sklearn is imported lazily so that `--commands-only` runs (which never call
fit/score) don't need it installed.
"""


def fit(liked_docs, disliked_docs, liked_weights=None, disliked_weights=None):
    """Build TF-IDF model from text docs. Returns dict consumed by `score`."""
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

    liked_centroid = liked_matrix.multiply(liked_weights[:, None]).sum(axis=0) / max(liked_weights.sum(), 1e-9)
    disliked_centroid = (
        disliked_matrix.multiply(disliked_weights[:, None]).sum(axis=0) / max(disliked_weights.sum(), 1e-9)
    )
    return {
        "vectorizer": vectorizer,
        "liked_centroid": np.asarray(liked_centroid),
        "disliked_centroid": np.asarray(disliked_centroid),
    }


def score(text, model):
    """Higher = closer to liked, further from disliked."""
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
