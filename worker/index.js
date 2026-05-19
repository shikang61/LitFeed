/**
 * LitFeed Cloudflare Worker — Telegram webhook receiver.
 *
 * Telegram delivers each update here. We split work into two paths:
 *
 *   1. Instant (handled in this Worker, sub-second response):
 *      - h:read_to_group  → forward the paper message to LITFEED_TO_READ_CHAT_ID
 *      - h:delete         → swap keyboard to confirm/cancel
 *      - h:confirm_delete → delete the message
 *      - h:cancel_delete  → restore the Read/Delete keyboard
 *
 *   2. Stateful (forwarded to GitHub via repository_dispatch, processed by
 *      .github/workflows/process_update.yml using `python main.py --apply-update`):
 *      - All /commands (e.g. /like, /dislike, /later, /add_cat, …)
 *      - h:read_to_group is ALSO dispatched (with webhook_handled=true) so the
 *        reading_log can be marked "saved".
 *      - Any legacy v:like/v:dislike/h:later/h:read/h:skip callbacks (not
 *        currently emitted by the bot but kept for backward compatibility).
 *
 * Required Worker secrets (set via `wrangler secret put NAME`):
 *   TELEGRAM_TOKEN        — Telegram bot token from BotFather
 *   CHAT_ID               — owner's numeric chat id (only this user is honoured)
 *   LITFEED_TO_READ_CHAT_ID — destination group for the Read button
 *   GITHUB_REPO           — "owner/repo", e.g. "shikang61/LitFeed"
 *   GITHUB_PAT            — fine-grained PAT with Contents:write on the repo,
 *                           or a classic PAT with the `repo` scope
 *   WEBHOOK_SECRET        — random string passed to setWebhook?secret_token=…;
 *                           Telegram echoes it back in X-Telegram-Bot-Api-Secret-Token
 */

const TG_API = "https://api.telegram.org";

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("LitFeed webhook is alive.", { status: 200 });
    }

    const presented = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.WEBHOOK_SECRET || presented !== env.WEBHOOK_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch (e) {
      return new Response("Bad JSON", { status: 400 });
    }

    const ownerId = Number(env.CHAT_ID);
    const senderId =
      update.callback_query?.from?.id ??
      update.message?.from?.id ??
      update.edited_message?.from?.id;
    if (!Number.isFinite(ownerId) || senderId !== ownerId) {
      // Ignore non-owner traffic silently — same posture as main.py.
      return new Response("OK");
    }

    const cb = update.callback_query;
    if (cb) {
      const handled = await handleCallback(cb, update, env, ctx);
      if (handled) return new Response("OK");
    }

    // Fall-through: dispatch to GitHub for stateful processing.
    ctx.waitUntil(dispatchToGitHub(env, update, false));
    return new Response("OK");
  },
};

async function handleCallback(cb, update, env, ctx) {
  const data = cb.data || "";
  const parts = data.split(":");
  if (parts.length < 3) return false;
  const [kind, action, key] = parts;
  if (kind !== "h") return false;

  const message = cb.message || {};
  const sourceChatId = message.chat?.id ?? Number(env.CHAT_ID);
  const messageId = message.message_id;

  if (action === "delete") {
    if (!messageId) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Message unavailable." });
      return true;
    }
    await tg(env, "editMessageReplyMarkup", {
      chat_id: sourceChatId,
      message_id: messageId,
      reply_markup: deleteConfirmKeyboard(key),
    });
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Confirm deletion?" });
    return true;
  }

  if (action === "cancel_delete") {
    if (!messageId) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Message unavailable." });
      return true;
    }
    await tg(env, "editMessageReplyMarkup", {
      chat_id: sourceChatId,
      message_id: messageId,
      reply_markup: voteKeyboard(key),
    });
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Deletion cancelled." });
    return true;
  }

  if (action === "confirm_delete") {
    if (!messageId) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Message unavailable." });
      return true;
    }
    const result = await tg(env, "deleteMessage", { chat_id: sourceChatId, message_id: messageId });
    const ok = result && result.ok;
    await tg(env, "answerCallbackQuery", {
      callback_query_id: cb.id,
      text: ok ? "Deleted." : "Could not delete message.",
    });
    return true;
  }

  if (action === "read_to_group") {
    if (!env.LITFEED_TO_READ_CHAT_ID) {
      await tg(env, "answerCallbackQuery", {
        callback_query_id: cb.id,
        text: "Set LITFEED_TO_READ_CHAT_ID to forward papers.",
      });
      return true;
    }
    if (!messageId) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Message unavailable." });
      return true;
    }
    const result = await tg(env, "forwardMessage", {
      chat_id: env.LITFEED_TO_READ_CHAT_ID,
      from_chat_id: sourceChatId,
      message_id: messageId,
    });
    if (!result || !result.ok) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Could not forward." });
      return true;
    }
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Forwarded to To Read." });
    // Also dispatch so reading_log can be updated to "saved".
    ctx.waitUntil(dispatchToGitHub(env, update, true));
    return true;
  }

  // Legacy h:later / h:read / h:skip — main.py mutates state. Dispatch.
  return false;
}

async function tg(env, method, body) {
  const url = `${TG_API}/bot${env.TELEGRAM_TOKEN}/${method}`;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !data.ok) {
      console.error(`[telegram] ${method} failed`, resp.status, data);
      return null;
    }
    return data;
  } catch (e) {
    console.error(`[telegram] ${method} error`, e);
    return null;
  }
}

async function dispatchToGitHub(env, update, webhookHandled) {
  if (!env.GITHUB_REPO || !env.GITHUB_PAT) {
    console.error("[github] GITHUB_REPO or GITHUB_PAT missing; cannot dispatch.");
    return;
  }
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_PAT}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "litfeed-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        event_type: "telegram-update",
        client_payload: { update, webhook_handled: webhookHandled },
      }),
    });
    if (!resp.ok) {
      console.error(`[github] dispatch failed ${resp.status}`, await resp.text());
    }
  } catch (e) {
    console.error("[github] dispatch error", e);
  }
}

function voteKeyboard(key) {
  return {
    inline_keyboard: [
      [{ text: "Read", callback_data: `h:read_to_group:${key}` }],
      [{ text: "Delete", callback_data: `h:delete:${key}` }],
    ],
  };
}

function deleteConfirmKeyboard(key) {
  return {
    inline_keyboard: [
      [
        { text: "Confirm delete", callback_data: `h:confirm_delete:${key}` },
        { text: "Cancel", callback_data: `h:cancel_delete:${key}` },
      ],
    ],
  };
}
