# LitFeed

Daily arXiv paper alerts delivered to Telegram, with a learned preference filter.

A GitHub Action fetches configured arXiv categories once per day and sends a
Markdown-formatted message per paper, each with 👍/👎 buttons. A Cloud Run
webhook handles those votes (and `/commands`) in real time. Votes train a
TF-IDF filter that progressively narrows what you see. State lives in Firestore.

## Architecture

```
GitHub Action (daily)  ──fetch+filter+alert──>  Telegram
        │                                          │
        └────────────> Firestore <─────────────────┘
                          ▲          button press / command
                          │                        │
                   Cloud Run webhook <──────────────┘
```

- **`main.py`** — daily job: read state from Firestore, fetch arXiv RSS, filter,
  send alerts, write `sent_ids` / `sent_cache` back.
- **`webhook.py`** — Cloud Run Flask app: Telegram POSTs each update here;
  reuses `handle_command` / `handle_callback` and writes to Firestore.
- **`store.py`** — Firestore data layer shared by both.

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow prompts.
2. Save the HTTP API token (`123456:ABC-DEF...`) — this is `TELEGRAM_TOKEN`.

### 2. Get your chat ID

1. Send your bot any message (e.g. `/start`).
2. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
3. The `"chat":{"id": ...}` number is your `CHAT_ID`.

### 3. Google Cloud project + Firestore

1. Create (or pick) a GCP project. Enable the **Firestore API** and create a
   Firestore database in **Native mode**.
2. Create a service account with the **Cloud Datastore User** role. Download a
   JSON key — its contents go in the `GCP_SA_KEY` GitHub secret.

### 4. Migrate existing state into Firestore

One-time, from a local checkout:

```bash
pip install -r requirements.txt
GOOGLE_APPLICATION_CREDENTIALS=key.json python migrate_to_firestore.py
```

This copies `config.json` + `votes.json` into Firestore. Once verified, those
two files can be deleted from the repo.

### 5. Deploy the webhook to Cloud Run

```bash
gcloud run deploy litfeed-webhook \
  --source . \
  --region <region> \
  --allow-unauthenticated \
  --set-env-vars TELEGRAM_TOKEN=<token>,CHAT_ID=<chat_id>,WEBHOOK_SECRET=<random-string>
```

Cloud Run builds the `Dockerfile` and uses the service account's identity for
Firestore. Note the service URL it prints.

### 6. Register the Telegram webhook

```bash
curl -F "url=https://<service-url>/webhook" \
     -F "secret_token=<WEBHOOK_SECRET>" \
     "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook"
```

Telegram now pushes every update to Cloud Run. (Webhook and `getUpdates`
polling are mutually exclusive — setting this disables polling.)

### 7. Add GitHub secrets

**Settings → Secrets and variables → Actions**:

| Name             | Value                                  |
|------------------|----------------------------------------|
| `TELEGRAM_TOKEN` | Token from BotFather                   |
| `CHAT_ID`        | Chat ID from `getUpdates`              |
| `GCP_SA_KEY`     | Full JSON of the service-account key   |

The daily workflow runs on the cron in `daily_papers.yml`; trigger it manually
via **Actions → Daily arXiv Paper Alerts → Run workflow**.

## Customising via Telegram

Commands are handled by the Cloud Run webhook **in real time** (no waiting for
the daily run):

| Command                | Effect                       |
|------------------------|------------------------------|
| `/list`                | Show current categories      |
| `/add_cat <arxiv.cat>` | Add arXiv category           |
| `/rm_cat <arxiv.cat>`  | Remove arXiv category        |
| `/reset`               | Restore default categories   |
| `/stats`               | Vote counts + filter status  |
| `/help`                | Show command list            |

Only the chat owner (`CHAT_ID`) is authorised; other senders are ignored.

## Preference filter

Each paper message has 👍/👎 buttons. Votes are stored in the Firestore `votes`
collection and train a TF-IDF model (sklearn) that scores future papers by
`cos(paper, liked_centroid) − cos(paper, disliked_centroid)`. Papers scoring
`> 0` are sent.

- **Cold start**: while either side has fewer than `MIN_VOTES_PER_SIDE`
  (default 10) votes, the filter is off and every paper is sent — seed the model.
- **Vote anytime**: the webhook records votes the instant you tap. Re-voting on
  a paper overwrites the previous vote (doc id = paper key).
- **Cache**: the `sent_cache` collection holds the text of the last
  `MAX_SENT_CACHE` (500) sent papers so the callback can reconstruct the
  document for training. Voting on older papers is rejected with a toast.

## Customising via code

Knobs in `main.py`:
- `DEFAULT_CATEGORIES` — seed list applied on `/reset` and first run.
- `LOOKBACK_HOURS` — defensive lower bound on paper age (default 36).
- `SNIPPET_CHARS` — abstract preview length.
- `MIN_VOTES_PER_SIDE` — votes per side before the filter activates.
- `PER_CATEGORY_LIMIT` — max papers sent per primary category per run.

`MAX_SENT_IDS` / `MAX_SENT_CACHE` live in `store.py`.

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=... CHAT_ID=...
export GOOGLE_APPLICATION_CREDENTIALS=key.json
python main.py            # daily job
# webhook:
export WEBHOOK_SECRET=...
gunicorn --bind :8080 webhook:app
```

Silent exit when no papers match (no Telegram message sent).
