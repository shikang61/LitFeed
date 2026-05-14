"""One-time migration: config.json + votes.json → Firestore.

Run locally once, pointing at a service-account key for the target project:

    GOOGLE_APPLICATION_CREDENTIALS=key.json python migrate_to_firestore.py

Idempotent — re-running just overwrites the same docs. Once Firestore is
populated and verified, config.json / votes.json can be deleted from the repo.
"""

import json
from pathlib import Path

import store

HERE = Path(__file__).parent


def main():
    cfg = json.loads((HERE / "config.json").read_text())
    votes = json.loads((HERE / "votes.json").read_text())

    store.set_categories(cfg.get("categories", []))
    store.add_sent_ids(cfg.get("sent_ids", []))

    for v in votes.get("liked", []):
        store.set_vote(v["key"], "liked", v["text"], ts=v.get("ts"))
    for v in votes.get("disliked", []):
        store.set_vote(v["key"], "disliked", v["text"], ts=v.get("ts"))
    for key, entry in votes.get("sent_cache", {}).items():
        store.put_sent_cache(key, entry["text"], entry["message_id"])

    print(
        f"Migrated: {len(cfg.get('categories', []))} categories, "
        f"{len(cfg.get('sent_ids', []))} sent_ids, "
        f"{len(votes.get('liked', []))} liked, "
        f"{len(votes.get('disliked', []))} disliked, "
        f"{len(votes.get('sent_cache', {}))} sent_cache entries."
    )


if __name__ == "__main__":
    main()
