/**
 * LitFeed Cloudflare Worker — Telegram webhook receiver.
 *
 * All Telegram traffic is handled here (sub-second toasts, D1 writes, and
 * owner commands). GitHub Actions only runs the batch brain:
 *   • daily_papers.yml  → python main.py
 *   • weekly_digest.yml → python main.py --weekly-digest
 *
 * Instant path (no GitHub):
 *   - v:like / v:dislike → D1 votes
 *   - h:read_to_group → forward + reading_log.status='saved'
 *   - h:delete / confirm / cancel — Telegram-only
 *   - /stats, /help, /clear prompt, /reset
 *   - h:confirm_clear / h:cancel_clear — D1 wipe + config.json via GitHub API
 *
 * Required Worker secrets (wrangler secret put NAME):
 *   TELEGRAM_TOKEN, CHAT_ID, LITFEED_TO_READ_CHAT_ID
 *   GITHUB_REPO — "owner/repo" (for /reset and confirmed /clear)
 *   GITHUB_PAT  — Contents:write on the repo
 *   WEBHOOK_SECRET — setWebhook secret_token
 *
 * D1 binding: DB → litfeed_state (wrangler.toml)
 */

import TOPIC_KEYWORDS from "../shared/topic_keywords.json";
import DEFAULT_CATEGORIES from "../shared/default_categories.json";

const TG_API = "https://api.telegram.org";
const MIN_VOTES_PER_SIDE = 10;
const MAX_VOTES_PER_SIDE = 250;

const CLEAR_CONFIRM_TEXT =
  "*Clear all LitFeed state?*\n\n" +
  "This permanently removes every vote, reading-history row, " +
  "sent-paper dedup entry, category preferences, and the last daily batch. " +
  "Categories will reset to defaults. The recommender goes back to cold start.\n\n" +
  "_This cannot be undone._";

const CLEAR_DONE_TEXT =
  "*LitFeed cleared*\n\n" +
  "Removed all votes, reading history, sent-paper dedup, " +
  "last batch, and category preferences. Categories reset to defaults.\n\n" +
  "_Next daily run starts cold (filter off until you vote again)._";

function inferTopics(text) {
  const lowered = (text || "").toLowerCase();
  const matched = [];
  for (const [topic, keywords] of Object.entries(TOPIC_KEYWORDS)) {
    if (keywords.some((kw) => lowered.includes(kw))) matched.push(topic);
  }
  return matched;
}

function topTopicsForEntries(entries, limit = 5) {
  const counts = {};
  for (const entry of entries) {
    const blob = `${entry.title || ""}\n${entry.text || ""}`;
    for (const topic of inferTopics(blob)) {
      counts[topic] = (counts[topic] || 0) + 1;
    }
  }
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit);
}

function formatTopicSummary(entries) {
  if (!entries.length) return "_not enough reading data yet_";
  const topics = topTopicsForEntries(entries);
  if (!topics.length) return "_not enough reading data yet_";
  return topics.map(([topic, count]) => `\`${topic}\` (${count})`).join(", ");
}

function githubHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_PAT}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "litfeed-worker",
    "Content-Type": "application/json",
  };
}

function utf8ToBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

/** Commit categories to config.json via the GitHub Contents API (for CI checkout). */
async function commitConfigCategories(env, categories) {
  if (!env.GITHUB_REPO || !env.GITHUB_PAT) {
    console.error("[github] GITHUB_REPO or GITHUB_PAT missing; cannot update config.json.");
    return false;
  }
  const content = JSON.stringify({ categories }, null, 2) + "\n";
  const encoded = utf8ToBase64(content);
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/contents/config.json`;

  let sha;
  try {
    const getResp = await fetch(url, { headers: githubHeaders(env) });
    if (getResp.ok) {
      sha = (await getResp.json()).sha;
    } else if (getResp.status !== 404) {
      console.error(`[github] GET config.json failed ${getResp.status}`, await getResp.text());
      return false;
    }
  } catch (e) {
    console.error("[github] GET config.json error", e);
    return false;
  }

  const body = {
    message: "chore: reset categories from Telegram",
    content: encoded,
  };
  if (sha) body.sha = sha;

  try {
    const putResp = await fetch(url, {
      method: "PUT",
      headers: githubHeaders(env),
      body: JSON.stringify(body),
    });
    if (!putResp.ok) {
      console.error(`[github] PUT config.json failed ${putResp.status}`, await putResp.text());
      return false;
    }
    return true;
  } catch (e) {
    console.error("[github] PUT config.json error", e);
    return false;
  }
}

async function clearAllState(env) {
  if (!env.DB) {
    console.error("[d1] DB binding missing; cannot clear state.");
    return false;
  }
  const tables = ["votes", "reading_log", "sent_ids", "last_batch", "kv"];
  try {
    for (const table of tables) {
      await env.DB.prepare(`DELETE FROM ${table}`).run();
    }
    return true;
  } catch (e) {
    console.error("[d1] clearAllState failed", e);
    return false;
  }
}

async function handleReset(env) {
  const ok = await commitConfigCategories(env, DEFAULT_CATEGORIES);
  const text = ok
    ? "Config reset to defaults."
    : "Could not update config.json — check Worker logs (GITHUB_PAT / GITHUB_REPO).";
  await tg(env, "sendMessage", { chat_id: env.CHAT_ID, text });
}

async function handleConfirmClear(env, sourceChatId, messageId) {
  const cleared = await clearAllState(env);
  const cfgOk = await commitConfigCategories(env, DEFAULT_CATEGORIES);
  let text = CLEAR_DONE_TEXT;
  if (!cleared) {
    text += "\n\n_(D1 clear failed — check Worker logs.)_";
  } else if (!cfgOk) {
    text += "\n\n_(config.json commit failed — daily runs may use stale categories until fixed.)_";
  }
  await tg(env, "editMessageText", {
    chat_id: sourceChatId,
    message_id: messageId,
    text,
    parse_mode: "Markdown",
    reply_markup: { inline_keyboard: [] },
  });
}

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
      return new Response("OK");
    }

    const cb = update.callback_query;
    if (cb) {
      const handled = await handleCallback(cb, update, env, ctx);
      if (handled) return new Response("OK");
    }

    const msg = update.message || update.edited_message;
    if (msg?.text?.startsWith("/")) {
      const cmd = msg.text.trim().split(/\s+/)[0].toLowerCase();
      if (cmd === "/stats" || cmd === "/help") {
        ctx.waitUntil(handleOwnerCommand(cmd, env));
        return new Response("OK");
      }
      if (cmd === "/reset") {
        ctx.waitUntil(handleReset(env));
        return new Response("OK");
      }
      if (cmd === "/clear") {
        await tg(env, "sendMessage", {
          chat_id: env.CHAT_ID,
          text: CLEAR_CONFIRM_TEXT,
          parse_mode: "Markdown",
          reply_markup: clearConfirmKeyboard(),
        });
        return new Response("OK");
      }
    }

    return new Response("OK");
  },
};

async function handleCallback(cb, update, env, ctx) {
  const data = cb.data || "";
  const parts = data.split(":");
  if (parts.length < 3) return false;
  const [kind, action, key] = parts;

  if (kind === "v" && (action === "like" || action === "dislike")) {
    const bucket = action === "like" ? "liked" : "disliked";
    // The vote is real and must persist even if the message is then deleted.
    ctx.waitUntil(
      recordVote(env, key, bucket).then(() => bumpCategoryPrefsFromVote(env, key, bucket))
    );
    if (action === "dislike") {
      // Offer the same delete-confirm flow as the Delete button.
      const message = cb.message || {};
      const sourceChatId = message.chat?.id ?? Number(env.CHAT_ID);
      const messageId = message.message_id;
      if (messageId) {
        await tg(env, "editMessageReplyMarkup", {
          chat_id: sourceChatId,
          message_id: messageId,
          reply_markup: deleteConfirmKeyboard(key),
        });
      }
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Recorded 👎 — delete?" });
    } else {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Recorded 👍" });
    }
    return true;
  }

  if (kind !== "h") return false;

  const message = cb.message || {};
  const sourceChatId = message.chat?.id ?? Number(env.CHAT_ID);
  const messageId = message.message_id;

  if (action === "confirm_clear" || action === "cancel_clear") {
    if (!messageId) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Message unavailable." });
      return true;
    }
    if (action === "cancel_clear") {
      await tg(env, "editMessageText", {
        chat_id: sourceChatId,
        message_id: messageId,
        text: "Clear cancelled.",
        parse_mode: "Markdown",
        reply_markup: { inline_keyboard: [] },
      });
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Cancelled." });
      return true;
    }
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Clearing…" });
    ctx.waitUntil(handleConfirmClear(env, sourceChatId, messageId));
    return true;
  }

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
    ctx.waitUntil(
      recordReadSaved(env, key).then(() => bumpCategoryPrefsFromRead(env, key))
    );
    return true;
  }

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

async function recordVote(env, key, bucket) {
  if (!env.DB) {
    console.error("[d1] DB binding missing; cannot record vote.");
    return;
  }
  const ts = new Date().toISOString();
  try {
    await env.DB.prepare(
      `INSERT INTO votes (paper_key, bucket, text, ts)
       VALUES (?1, ?2, COALESCE(
         (SELECT text FROM reading_log WHERE paper_key = ?1),
         (SELECT text FROM last_batch  WHERE paper_key = ?1),
         ''
       ), ?3)
       ON CONFLICT(paper_key) DO UPDATE SET bucket = excluded.bucket, ts = excluded.ts`
    )
      .bind(key, bucket, ts)
      .run();
    await pruneVotes(env, "liked");
    await pruneVotes(env, "disliked");
  } catch (e) {
    console.error("[d1] recordVote failed", e);
  }
}

async function pruneVotes(env, bucket) {
  try {
    await env.DB.prepare(
      `DELETE FROM votes WHERE bucket = ?1 AND paper_key NOT IN (
         SELECT paper_key FROM votes WHERE bucket = ?1
         ORDER BY ts DESC LIMIT ?2
       )`
    )
      .bind(bucket, bucket, MAX_VOTES_PER_SIDE)
      .run();
  } catch (e) {
    console.error("[d1] pruneVotes failed", e);
  }
}

async function recordReadSaved(env, key) {
  if (!env.DB) {
    console.error("[d1] DB binding missing; cannot record read_to_group.");
    return;
  }
  const ts = new Date().toISOString();
  try {
    await env.DB.prepare(
      `INSERT INTO reading_log (paper_key, status, status_ts, created_ts)
       VALUES (?1, 'saved', ?2, ?2)
       ON CONFLICT(paper_key) DO UPDATE SET status = 'saved', status_ts = excluded.status_ts`
    )
      .bind(key, ts)
      .run();
  } catch (e) {
    console.error("[d1] recordReadSaved failed", e);
  }
}

function voteKeyboard(key) {
  return {
    inline_keyboard: [
      [
        { text: "👍 Like", callback_data: `v:like:${key}` },
        { text: "👎 Dislike", callback_data: `v:dislike:${key}` },
      ],
      [
        { text: "Read", callback_data: `h:read_to_group:${key}` },
        { text: "Delete", callback_data: `h:delete:${key}` },
      ],
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

function clearConfirmKeyboard() {
  return {
    inline_keyboard: [
      [
        { text: "Confirm clear", callback_data: "h:confirm_clear:all" },
        { text: "Cancel", callback_data: "h:cancel_clear:all" },
      ],
    ],
  };
}

async function handleOwnerCommand(cmd, env) {
  if (cmd === "/help") {
    await sendHelp(env);
    return;
  }
  if (cmd === "/stats") {
    await sendStats(env);
  }
}

async function sendHelp(env) {
  const text =
    "*Commands*\n" +
    "/reset — restore default categories\n" +
    "/clear — wipe all state (asks for confirmation)\n" +
    "/stats — vote counts + filter status\n" +
    "/help — this message\n\n" +
    "_Vote and triage with the buttons under each paper:_ " +
    "👍 / 👎 / Read / Delete.\n\n" +
    "_Weekly reading digest: Sunday cron (GitHub Actions)._";
  await tg(env, "sendMessage", { chat_id: env.CHAT_ID, text, parse_mode: "Markdown" });
}

async function sendStats(env) {
  if (!env.DB) {
    await tg(env, "sendMessage", { chat_id: env.CHAT_ID, text: "D1 binding missing." });
    return;
  }
  try {
    const liked = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM votes WHERE bucket = 'liked'"
    ).first();
    const disliked = await env.DB.prepare(
      "SELECT COUNT(*) AS n FROM votes WHERE bucket = 'disliked'"
    ).first();
    const nl = Number(liked?.n ?? 0);
    const nd = Number(disliked?.n ?? 0);
    const active = nl >= MIN_VOTES_PER_SIDE && nd >= MIN_VOTES_PER_SIDE;
    const status = active ? "active" : `cold start (need ≥${MIN_VOTES_PER_SIDE} each)`;

    const statusRows = await env.DB.prepare(
      "SELECT status, COUNT(*) AS n FROM reading_log GROUP BY status"
    ).all();
    const counts = {};
    for (const row of statusRows.results || []) {
      counts[row.status || "sent"] = Number(row.n ?? 0);
    }

    const meaningfulRows = await env.DB.prepare(
      "SELECT title, text FROM reading_log WHERE status = 'saved'"
    ).all();
    const topics = formatTopicSummary(meaningfulRows.results || []);

    const prefs = await loadCategoryPreferences(env);
    let prefLine = "";
    if (Object.keys(prefs).length) {
      const top = Object.entries(prefs)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 5)
        .map(([cat, val]) => `\`${cat}\` (${val >= 0 ? "+" : ""}${val.toFixed(1)})`)
        .join(", ");
      prefLine = `\n*Category lean:* ${top}\n`;
    }

    const text =
      `*Votes*\n👍 ${nl}\n👎 ${nd}\n` +
      `_Capped at ${MAX_VOTES_PER_SIDE} per side (oldest dropped)._\n\n` +
      `*Filter:* ${status}\n` +
      `*Scoring:* TF-IDF + embeddings\n\n` +
      `*Reading*\n` +
      `Saved: ${counts.saved ?? 0}\n\n` +
      `*Topics:* ${topics}` +
      prefLine;
    await tg(env, "sendMessage", { chat_id: env.CHAT_ID, text, parse_mode: "Markdown" });
  } catch (e) {
    console.error("[d1] sendStats failed", e);
    await tg(env, "sendMessage", {
      chat_id: env.CHAT_ID,
      text: "Could not load stats from D1.",
    });
  }
}

async function loadCategoryPreferences(env) {
  const row = await env.DB.prepare(
    "SELECT value FROM kv WHERE key = 'category_preferences'"
  ).first();
  if (!row?.value) return {};
  try {
    const data = JSON.parse(row.value);
    return typeof data === "object" && data !== null ? data : {};
  } catch {
    return {};
  }
}

async function saveCategoryPreferences(env, prefs) {
  await env.DB.prepare(
    "INSERT INTO kv (key, value) VALUES ('category_preferences', ?1) " +
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
  )
    .bind(JSON.stringify(prefs))
    .run();
}

async function categoriesForKey(env, key) {
  const row = await env.DB.prepare(
    "SELECT categories FROM reading_log WHERE paper_key = ?1"
  )
    .bind(key)
    .first();
  if (!row?.categories) return [];
  try {
    const parsed = JSON.parse(row.categories);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function bumpPrefs(prefs, categories, deltaPrimary, deltaSecondary) {
  if (!categories.length) return prefs;
  const [primary, ...rest] = categories;
  prefs[primary] = (prefs[primary] ?? 0) + deltaPrimary;
  const secondary = deltaSecondary !== undefined ? deltaSecondary : deltaPrimary * 0.25;
  for (const cat of rest) {
    prefs[cat] = (prefs[cat] ?? 0) + secondary;
  }
  return prefs;
}

async function bumpCategoryPrefsFromVote(env, key, bucket) {
  const cats = await categoriesForKey(env, key);
  const prefs = await loadCategoryPreferences(env);
  if (bucket === "liked") {
    bumpPrefs(prefs, cats, 1.0, 0.25);
  } else {
    bumpPrefs(prefs, cats, -0.5, -0.15);
  }
  await saveCategoryPreferences(env, prefs);
}

async function bumpCategoryPrefsFromRead(env, key) {
  const cats = await categoriesForKey(env, key);
  const prefs = await loadCategoryPreferences(env);
  bumpPrefs(prefs, cats, 0.5, 0.15);
  await saveCategoryPreferences(env, prefs);
}
