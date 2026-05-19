# LitFeed roadmap

Tracked future work and deferred items. In-scope items from the May 2026
recommender/ops pass are marked **done** below.

## Done (May 2026)

- [x] **Read as a signal** — saved papers feed the liked corpus at elevated weight.
- [x] **Per-category models** — TF-IDF + embedding centroids per arXiv category, with global fallback.
- [x] **Category preference learning** — weights in D1 `kv` updated from votes and reads.
- [x] **Embeddings blended with TF-IDF** — `sentence-transformers/all-MiniLM-L6-v2` (disable via `LITFEED_DISABLE_EMBEDDINGS=1`).
- [x] **Negative serendipity** — weekly cap per inferred theme (`MAX_PAPERS_PER_RUN × RUNS_PER_DAY`, 3 cron runs/day).
- [x] **Lightweight commands in Worker** — `/stats` and `/help` answered at the edge.
- [x] **GitHub only for batch brain** — Worker owns all Telegram; Actions runs daily + weekly digest only; `/digest` removed.
- [x] **CI guard for workflow YAML** — `.github/workflows/validate.yml`.
- [x] **Vote cap (250/side)** — automatic prune of oldest votes after each write.
- [x] **Digest from D1 only** — weekly digest and deep-read pick use `state_store` / D1 data only.

## Keep in view (KIV)

### Tests

- [ ] pytest coverage for `format_paper`, selection pipeline, `format_weekly_digest`.
- [ ] Mocked D1 client tests for `state_store` mutators.
- [ ] Worker integration smoke (optional: miniflare + D1 local).

### Export

- [ ] `/export` or script: liked papers → CSV / BibTeX for Zotero.

### Architecture (longer term)

- [x] Handle all Telegram commands in the Worker; GitHub Actions only for daily fetch + weekly digest.
- [ ] Grok summary cache: skip re-summarize when abstract unchanged.
- [ ] `/add` / `/remove` category commands without editing `config.json`.
- [ ] Health cron: D1 + webhook + last daily run status.

## Ideas (not scheduled)

- Score explain line on each alert (top TF-IDF terms).
- Position-debias for votes on papers #1–#3 in a batch.
- Platt calibration so scores map to P(like).
- Dual-recommender “disagreement” highlight when TF-IDF and embeddings diverge.
