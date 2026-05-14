"""Firestore-backed state store for LitFeed.

Replaces the old config.json / votes.json files. Both the daily GitHub Action
(main.py) and the Cloud Run webhook (webhook.py) read and write here, so there
is a single source of truth with no git races.

Layout:
  state/config          doc   {categories: [...], sent_ids: [...]}
  votes/{paper_key}      docs  {bucket: "liked"|"disliked", text, ts}
  sent_cache/{paper_key} docs  {text, message_id, ts}

Auth: firestore.Client() picks up GOOGLE_APPLICATION_CREDENTIALS (set by the
google-github-actions/auth step) or the Cloud Run service account automatically.
"""

from datetime import datetime, timezone

from google.cloud import firestore

MAX_SENT_IDS = 500
MAX_SENT_CACHE = 500

_db = None


def _client():
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- config ----------

def _config_ref():
    return _client().collection("state").document("config")


def get_config(default_categories):
    """Return {categories, sent_ids}; seed categories from defaults if unset."""
    snap = _config_ref().get()
    data = snap.to_dict() if snap.exists else {}
    return {
        "categories": data.get("categories") or list(default_categories),
        "sent_ids": data.get("sent_ids", []),
    }


def set_categories(categories):
    _config_ref().set({"categories": list(categories)}, merge=True)


def add_sent_ids(keys):
    """Append keys to sent_ids, keeping only the most recent MAX_SENT_IDS."""
    ref = _config_ref()

    @firestore.transactional
    def _txn(txn):
        snap = ref.get(transaction=txn)
        existing = snap.to_dict().get("sent_ids", []) if snap.exists else []
        merged = (existing + list(keys))[-MAX_SENT_IDS:]
        txn.set(ref, {"sent_ids": merged}, merge=True)

    _txn(_client().transaction())


# ---------- votes ----------

def set_vote(key, bucket, text, ts=None):
    """Upsert a vote. Doc id = paper key, so re-voting overwrites in place."""
    _client().collection("votes").document(key).set({
        "bucket": bucket,
        "text": text,
        "ts": ts or _now(),
    })


def get_votes():
    """Return {"liked": [...], "disliked": [...]} of {key, text, ts} entries."""
    liked, disliked = [], []
    for doc in _client().collection("votes").stream():
        v = doc.to_dict()
        entry = {"key": doc.id, "text": v.get("text", ""), "ts": v.get("ts", "")}
        if v.get("bucket") == "liked":
            liked.append(entry)
        elif v.get("bucket") == "disliked":
            disliked.append(entry)
    return {"liked": liked, "disliked": disliked}


def vote_counts():
    """Return (n_liked, n_disliked)."""
    v = get_votes()
    return len(v["liked"]), len(v["disliked"])


# ---------- sent_cache ----------

def get_sent_cache_entry(key):
    """Return {text, message_id, ts} for a sent paper, or None."""
    snap = _client().collection("sent_cache").document(key).get()
    return snap.to_dict() if snap.exists else None


def put_sent_cache(key, text, message_id, ts=None):
    _client().collection("sent_cache").document(key).set({
        "text": text,
        "message_id": message_id,
        "ts": ts or _now(),
    })


def prune_sent_cache():
    """Delete the oldest entries beyond MAX_SENT_CACHE."""
    col = _client().collection("sent_cache")
    docs = list(col.stream())
    excess = len(docs) - MAX_SENT_CACHE
    if excess <= 0:
        return
    docs.sort(key=lambda d: d.to_dict().get("ts", ""))
    for doc in docs[:excess]:
        doc.reference.delete()
