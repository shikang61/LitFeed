# Recommender system

How LitFeed decides which of today's arXiv papers actually land in your
Telegram chat. This file is the source of truth for the algorithm; revisit
it when you want to tune behavior or rebuild the engine.

The implementation lives in two files:

- [`recommender.py`](../recommender.py) — the bare TF-IDF model
  (`fit` / `score` / `explain`). sklearn-only, no LitFeed-specific
  knowledge.
- [`main.py`](../main.py) — everything around it: vote loading, model
  rebuild, the selection pipeline (diversity, serendipity, priority mix),
  cold-start logic, the message-header score, and the weekly digest
  picker.

## 1. The scoring function

Given a paper `p` and a model fit on your liked / disliked vote corpora,
the relevance score is:

```
score(p) = cos(p, liked_centroid) − cos(p, disliked_centroid)
```

- `> 0` means closer to your liked papers than your disliked ones.
- `< 0` means the opposite.
- `0` is right on the boundary.

The score you see in each Telegram alert (`*Score:* +0.23` etc.) is this
exact number, signed to two decimal places. During cold start the value
isn't computed and the header shows `-`.

The "paper" the recommender sees is `f"{title}\n\n{abstract}"` — both
fields, joined. Authors and arXiv categories are not part of the input.

## 2. How the model is fit

`recommender.fit(liked_docs, disliked_docs, liked_weights, disliked_weights)`
builds a single `TfidfVectorizer` on the concatenated corpus
(`liked + disliked`), then takes a **weighted mean** of the resulting rows
to form two centroids.

Vectorizer config (`recommender.py`):

| Setting          | Value           | Why                                                    |
|------------------|-----------------|--------------------------------------------------------|
| `lowercase`      | `True`          | Case shouldn't matter for matching.                    |
| `stop_words`     | `"english"`     | Strip "the", "and", etc.                               |
| `ngram_range`    | `(1, 2)`        | Captures bigrams like "magnetic reconnection".         |
| `min_df`         | `1`             | Don't drop rare terms — corpus is small.               |
| `max_df`         | `0.95`          | Drop terms in >95% of docs (very generic).             |
| `sublinear_tf`   | `True`          | Use `1 + log(tf)` to dampen runaway term frequencies.  |

Centroid construction (per side):

```
centroid = Σ_i (weight_i · tfidf_row_i) / Σ_i weight_i
```

so a single very-recent like has the same effect as many very-old likes
combined.

### Recency-weighted votes

Both centroids are weighted by **age-decay** with a 45-day half-life
(`RECENCY_HALF_LIFE_DAYS = 45` in `main.py`):

```
weight(vote) = 0.5 ** (age_days / 45)
```

So a vote cast today counts as 1.0, a vote from 45 days ago as 0.5, a
vote from 90 days ago as 0.25, etc. Votes without a timestamp default to
weight 1.0. The intent is to let your interests drift over time without
having to manually retire old votes.

This is computed at fit-time (every daily run rebuilds the model), so a
deployed model is always "as of now" — there's nothing cached on disk.

### Vote storage and re-votes

Each paper has at most **one** row in the D1 `votes` table
(`paper_key PRIMARY KEY`). A re-vote (👍 → 👎 or vice versa) upserts the
row; we don't keep a history of flipped votes. The `text` column stores
the abstract that gets fed to the vectorizer.

The Worker fills `votes.text` via `COALESCE(reading_log.text,
last_batch.text, '')` so the abstract is always available without
shipping it back in Telegram's 64-byte `callback_data`. If both lookups
miss (shouldn't happen post-Phase-F) the row gets `text=''` and that
vote contributes nothing useful to the centroid until backfilled.

## 3. The selection pipeline

Each daily run executes this sequence (`main.py` ≈ lines 1230–1265):

```
fresh papers (RSS feed minus dedup minus older than 36h)
    │
    ▼  apply preference filter?  (filter_active(votes))
    │
    ├─ COLD START (either side has < MIN_VOTES_PER_SIDE = 10 votes)
    │     sort by freshness, score=NaN for each → header shows "-"
    │
    └─ WARM
          build_model(votes)            # TF-IDF, recency-weighted
          score each paper              # cos − cos
          sort by (score desc, published desc)
          apply_diversity_guardrail     # Jaccard ≤ 0.85
          select_with_serendipity       # 1 wildcard near-miss slot
    │
    ▼
cap_per_category (PER_CATEGORY_LIMIT = 0 → disabled today)
    │
    ▼
select_priority_mix (target 7 priority + 3 other = 10 per run)
    │
    ▼
send to Telegram
```

Each stage in detail:

### 3.1 Cold-start gate — `filter_active`

```python
def filter_active(votes):
    return len(votes["liked"]) >= 10 and len(votes["disliked"]) >= 10
```

Until you have ≥10 likes **and** ≥10 dislikes, the model isn't built. The
bot sends freshest-first with diversity, so you can rack up votes on a
balanced sample. The header shows `*Score:* -` during this phase.

### 3.2 Score + sort

`scored = [(p, recommender.score(paper_text(p), model)) for p in fresh]`,
sorted by `(score, published)` descending. Personal relevance wins;
freshness is the tiebreaker.

### 3.3 Diversity guardrail

`apply_diversity_guardrail(scored, threshold=0.85)` walks the sorted list
and drops any paper whose Jaccard token-set overlap with an already-kept
paper exceeds 0.85. That's a very loose threshold — it only catches
near-duplicates (reposts, v1/v2 collisions that survived dedup, ML
papers with identical phrasing).

Implementation is naive — `set(re.findall(r"[a-z0-9]+", text.lower()))`
plus pairwise comparisons. Fine at our scale (≤ a few hundred papers per
run); revisit if the candidate pool grows much larger.

### 3.4 Serendipity slot

`select_with_serendipity(scored, floor, total, slots=1)` reserves one
slot in every batch for a **near-miss**: a paper that's below the
relevance floor but is the highest-scoring such paper. The intent is to
avoid an over-narrow feed once the model converges on your existing
likes — you still see one "adjacent idea" per run.

The floor comes from `current_relevance_floor`:

| Vote count (min side)                     | Floor   | Behavior                                          |
|-------------------------------------------|---------|---------------------------------------------------|
| `< 10`                                    | `None`  | Cold start; no filtering, no serendipity.         |
| `10 ≤ n < 15` (early-active window)       | `-0.03` | Slightly permissive — model is still settling.    |
| `≥ 15`                                    | `0.0`   | Strict — only positive-relevance papers + 1 slot. |

The 5-vote "early-active" buffer (`EARLY_ACTIVE_EXTRA_VOTES = 5`) exists
because the first few warm runs can produce a noisy centroid; a slightly
negative floor stops the bot from going silent right after the
cold-start gate opens.

### 3.5 Per-category cap

`cap_per_category(papers, PER_CATEGORY_LIMIT)`. **Currently disabled**
(`PER_CATEGORY_LIMIT = 0`) so the best papers win globally regardless of
arXiv primary_category. If you find one category drowning out the others,
set this to e.g. `2` and quotas kick in.

### 3.6 Priority mix

`select_priority_mix(papers, total=10, priority_slots=7)` enforces a
target ratio of 7/10 from `PRIORITY_CATEGORIES` (currently `cs.MA`,
`math.NA`, `math-ph`, `physics.comp-ph`, `physics.flu-dyn`,
`physics.plasm-ph`, `q-fin.CP`, `q-fin.PM`, `q-fin.TR`, `q-fin.RM`) and
3/10 from anything else.

Both pools are picked in their input order (which is already
relevance-then-freshness from §3.2). If a pool runs out (e.g. only 4
priority papers in today's batch), the remaining slots fall through to
the other pool to fill the cap. So the 7/3 target is best-effort, not a
hard quota.

This is the place to encode "what kind of papers do I actually care
about as a baseline" independently of votes. Tightening or loosening the
priority set is the simplest knob if the feed feels off-domain.

## 4. The weekly digest's deep-read pick

`choose_deep_read_candidate(votes, reading_log)` (called from
`format_weekly_digest`) scores **every** paper in `reading_log` with
status `'saved'` or `'sent'` and returns the highest-scoring one. It
uses the same `recommender.score`, with a small bias:

```python
status_bonus = 0.25 if entry["status"] == "saved" else 0.0
sort_key = (score + status_bonus, status_ts or sent_ts)
```

So a paper you explicitly tapped Read gets a +0.25 lift over one that
was merely sent — even if their raw scores are similar, the "I actually
want to read this" signal wins. Ties break by recency.

During cold start (no model), the bonus is the only ranking signal and
the recency tiebreak does the rest.

## 5. Dead-ish code: `recommender.explain`

`recommender.explain(text, model, top_n=5)` returns the top contributing
n-grams for a given paper (`tfidf_vector * (liked_centroid −
disliked_centroid)`, ranked by contribution). It used to back the
`/why N` command, which was removed. The function still lives in
`recommender.py`; it's harmless and the only thing you'd need to bring
back to revive a "why?" explainer for the message header or `/stats`.

## 6. Tunable knobs

All defined in `main.py`. The relevant ones, with current values and the
effect of changing them:

| Constant                              | Value     | Effect of increasing                                            |
|---------------------------------------|-----------|-----------------------------------------------------------------|
| `MIN_VOTES_PER_SIDE`                  | `10`      | Stricter cold-start gate; the model takes longer to activate.   |
| `EARLY_ACTIVE_EXTRA_VOTES`            | `5`       | Longer early-active window with a permissive floor.             |
| `EARLY_ACTIVE_RELEVANCE_FLOOR`        | `-0.03`   | More forgiving early-window floor (more papers survive).        |
| `RECENCY_HALF_LIFE_DAYS`              | `45`      | Slower drift; older votes keep more weight.                     |
| `DIVERSITY_MAX_JACCARD`               | `0.85`    | More tolerant of near-duplicates (more papers pass).            |
| `SERENDIPITY_SLOTS`                   | `1`       | More wildcard slots per run.                                    |
| `MAX_PAPERS_PER_RUN`                  | `10`      | Bigger batches.                                                 |
| `PRIORITY_PAPERS_PER_RUN`             | `7`       | Push the mix harder toward `PRIORITY_CATEGORIES`.               |
| `PER_CATEGORY_LIMIT`                  | `0`       | `0` = off. Set `>0` to cap per-category and force breadth.      |
| `PRIORITY_CATEGORIES`                 | (set of 10)| Edit the set itself to change what counts as a priority paper. |

And in `recommender.py`'s `TfidfVectorizer` call:

| Setting        | Value     | Effect of changing                                                   |
|----------------|-----------|----------------------------------------------------------------------|
| `ngram_range`  | `(1, 2)`  | Bumping to `(1, 3)` captures trigrams ("partial differential eq").   |
| `min_df`       | `1`       | Raising drops rare terms (rare = noisy ≠ rare = informative).        |
| `max_df`       | `0.95`    | Lowering drops more generic terms.                                   |
| `sublinear_tf` | `True`    | Off → raw tf; runaway frequencies in long abstracts will dominate.   |

## 7. Current production extensions (May 2026)

Implemented in `recommender.fit_profile` / `score_profile` and `main.py`:

| Feature | Behavior |
|---------|----------|
| Read signal | `status=saved` papers join the liked corpus at `READ_SIGNAL_WEIGHT` (1.5×). |
| Per-category models | TF-IDF + embedding centroids per primary category when ≥2 docs on a side. |
| Category preferences | D1 `kv.category_preferences` nudges scores via `CATEGORY_PREF_ALPHA`. |
| Blended scoring | `TFIDF_BLEND` (default 0.5) mixes TF-IDF and `all-MiniLM-L6-v2`; set `LITFEED_DISABLE_EMBEDDINGS=1` to skip embeddings. |
| Negative serendipity | `THEME_WEEKLY_CAP` (= `MAX_PAPERS_PER_RUN × RUNS_PER_DAY`, default 30) per theme over 7 days. |
| Vote cap | Oldest votes dropped beyond `MAX_VOTES_PER_SIDE` (250) per bucket. |

## 8. If you want to improve it

Concrete directions, ordered by effort:

1. **Better text representation.** TF-IDF treats "fusion reactor" and
   "tokamak" as unrelated. Sentence/abstract embeddings (e.g.
   `sentence-transformers/all-MiniLM-L6-v2`, ~80MB) would capture
   semantic similarity. Drop-in replacement: change `fit` to embed each
   abstract, mean-pool to a centroid, and `score` to cosine-similarity
   the embeddings. Costs: an extra ~200MB Docker/Actions image, ~1s per
   paper at run-time on CPU.

2. **Per-category models.** Right now one centroid covers everything you
   like across CS, physics, finance. A vote for an ML paper drags the
   plasma-physics relevance up. Split the corpus by primary_category
   and score each paper against the model for its own category.

3. **Use the Read signal.** Right now Read updates `reading_log.status`
   but doesn't feed the recommender. A "Read" tap is a stronger signal
   than a 👍 — promote it to a synthetic like with weight ≈ 1.5 in the
   centroid sum.

4. **Position-debias the score.** Papers near the top of the batch get
   disproportionately voted on. The recommender currently treats all
   votes equally; you could down-weight votes from papers that were
   #1–#3 in their batch.

5. **Negative serendipity.** The diversity guardrail prevents
   near-duplicates within a batch but does nothing about repeated
   *themes* across batches. Consider a "you've already seen N papers
   about reconnection this week" guardrail before sending.

6. **Active learning / abstention.** When a paper's `|score|` is near
   zero (the model is unsure), prioritize it in the serendipity slot
   even when it doesn't beat the floor. You'd train faster on the
   ambiguous cases.

7. **Calibration.** The raw score is fine for ranking but not
   interpretable. A platt-scaling pass on (score → P(like)) would let
   you set a probability threshold (e.g. send anything with P(like) ≥
   0.6) instead of `floor=0`.

8. **Multiple recommenders, blend.** Keep TF-IDF as a fast baseline,
   bolt on embeddings as a secondary, and blend their scores. If
   they disagree strongly, that's the most interesting paper of the day.

For any of these, the cleanest entry point is `recommender.py::fit` /
`score` — keep the interface, swap the implementation, and the selection
pipeline doesn't need to know anything changed.

## 9. Debugging

```bash
# Print the current model state (requires CF_* env vars)
python -c '
import state_store, main, recommender
votes = state_store.load_votes()
print(f"liked={len(votes[\"liked\"])} disliked={len(votes[\"disliked\"])}")
print(f"active={main.filter_active(votes)} floor={main.current_relevance_floor(votes)}")
if main.filter_active(votes):
    model = main.build_model(votes)
    # score a synthetic paper
    s = recommender.score("magnetic reconnection in tokamak plasmas", model)
    print(f"score(reconnection paper) = {s:+.3f}")
'

# Inspect raw votes in D1
cd worker
npx wrangler d1 execute litfeed_state --remote \
  --command "SELECT bucket, COUNT(*), MIN(ts), MAX(ts) FROM votes GROUP BY bucket"
```

A common failure mode worth knowing: if all 10 likes happen on, say,
plasma-physics papers within one week, the centroid is over-confident in
that direction. The recency half-life *eventually* fixes this, but if
you want it gone faster, manually delete the offending `votes` rows in
D1 (or 👎 a few papers that are exemplars of the bias) and the next
daily run rebuilds the model with a saner shape.
