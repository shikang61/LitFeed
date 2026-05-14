"""Cloud Run webhook receiver for Telegram updates.

Telegram delivers each update (an owner /command or a 👍/👎 vote callback) here
the instant it happens — this replaces the old getUpdates polling. The handler
reuses handle_command / handle_callback from main.py and persists to Firestore
via store.py, so a vote lands in seconds.

Env vars (set on the Cloud Run service):
  TELEGRAM_TOKEN   Telegram bot token
  CHAT_ID          owner's numeric Telegram id; other senders are ignored
  WEBHOOK_SECRET   shared secret; must match setWebhook's secret_token

Register the webhook once after deploy:
  curl -F "url=https://<service-url>/webhook" \
       -F "secret_token=<WEBHOOK_SECRET>" \
       "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook"
"""

import os

from flask import Flask, request

import main
import store

app = Flask(__name__)

TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = int(os.environ["CHAT_ID"])
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


@app.get("/")
def health():
    return "ok"


@app.post("/webhook")
def webhook():
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            return "forbidden", 403

    update = request.get_json(silent=True) or {}

    cb = update.get("callback_query")
    if cb:
        if cb.get("from", {}).get("id") == OWNER_ID:
            main.handle_callback(TOKEN, cb, store)
        return "ok"

    msg = update.get("message") or update.get("edited_message")
    if msg and msg.get("from", {}).get("id") == OWNER_ID:
        text = msg.get("text", "")
        if text.startswith("/"):
            reply = main.handle_command(text, store)
            if reply:
                main.send_message(TOKEN, str(OWNER_ID), reply)

    return "ok"
