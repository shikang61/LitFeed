"""TF-IDF recommender via scikit-learn.

Train on liked/disliked (title + abstract). Score new paper by
`cos(paper, liked_centroid) - cos(paper, disliked_centroid)`.

sklearn is imported lazily so that `--commands-only` runs (which never call
fit/score) don't need it installed.
"""


def fit(liked_docs, disliked_docs):
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
    liked_centroid = matrix[:n_liked].mean(axis=0)
    disliked_centroid = matrix[n_liked:].mean(axis=0)
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
