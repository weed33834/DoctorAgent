"use strict";

// DoctorAgent Inbox — background service worker (Manifest V3)
//
// Responsibilities:
//   1. Register right-click context menu items for one-click submission.
//   2. Encrypt extracted text with AES-256-GCM using a PBKDF2-SHA256 key
//      derived from the API token (mirrors the server-side decryption in
//      doctoragent/api/server.py::_decrypt_browser_submission).
//   3. POST the encrypted payload to /inbox/submit.
//
// The token never leaves the extension in plaintext — only the derived
// ciphertext is sent over the wire.

// Load shared utilities (filename generators) so popup.js and background.js
// use a single source of truth for filename formatting.
importScripts("shared.js");

const PBKDF2_ITERATIONS = 600000;
const SALT_LEN = 16;
const NONCE_LEN = 12;
const KEY_LEN = 256; // bits

// ── Context menu setup ─────────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  try {
    chrome.contextMenus.create({
      id: "aegis-send-selection",
      title: "发送选区到 DoctorAgent",
      contexts: ["selection"],
    });
    chrome.contextMenus.create({
      id: "aegis-send-page",
      title: "发送本页到 DoctorAgent",
      contexts: ["page"],
    });
  } catch {
    // Re-install may throw if IDs already exist — safe to ignore.
  }
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const { server, token } = await chrome.storage.local.get(["server", "token"]);
  if (!server || !token) {
    // No settings yet — silently ignore; user configures via popup.
    return;
  }
  if (info.menuItemId === "aegis-send-selection" && info.selectionText) {
    const fname = makeSelectionFilename();
    await encryptAndSubmit(server, token, info.selectionText, "selection", fname);
  } else if (info.menuItemId === "aegis-send-page" && tab && tab.id != null) {
    try {
      // content.js is injected on demand (activeTab + scripting) instead of
      // being statically matched against <all_urls>, so it only runs when the
      // user explicitly sends a page. Restricted pages (chrome://, web store)
      // will reject the injection and fall through to the catch below.
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
      const res = await chrome.tabs.sendMessage(tab.id, { type: "GET_PAGE_TEXT" });
      if (res && res.text) {
        const fname = makePageFilename(res.title || "");
        await encryptAndSubmit(server, token, res.text, "page", fname);
      }
    } catch {
      // Content script may not be injected on restricted pages (chrome://,
      // web store, etc.) — nothing we can do.
    }
  }
});

// ── Popup message handler ──────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "SUBMIT") {
    encryptAndSubmit(msg.server, msg.token, msg.text, msg.source, msg.filename)
      .then((result) => sendResponse(result))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // async response
  }
  return false;
});

// ── Crypto: PBKDF2-SHA256 → AES-256-GCM ────────────────────────────────

/** Encode an ArrayBuffer/Uint8Array to base64 (chunked to avoid call-stack limits). */
function toBase64(buf) {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/** Derive a non-extractable AES-256-GCM CryptoKey from token + salt. */
async function deriveAesKey(token, salt) {
  const enc = new TextEncoder();
  const baseKey = await crypto.subtle.importKey(
    "raw",
    enc.encode(token),
    "PBKDF2",
    false,
    ["deriveKey"]
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    baseKey,
    { name: "AES-GCM", length: KEY_LEN },
    false,
    ["encrypt"]
  );
}

/** Encrypt plaintext → { content, nonce, salt } base64 fields. */
async function encryptPayload(token, plaintext) {
  const enc = new TextEncoder();
  const salt = crypto.getRandomValues(new Uint8Array(SALT_LEN));
  const nonce = crypto.getRandomValues(new Uint8Array(NONCE_LEN));
  const key = await deriveAesKey(token, salt);
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce },
    key,
    enc.encode(plaintext)
  );
  return {
    content: toBase64(ciphertext),
    nonce: toBase64(nonce),
    salt: toBase64(salt),
    // Carry the iteration count so the server derives the key with exactly
    // this value instead of guessing (see server._decrypt_browser_submission).
    iterations: PBKDF2_ITERATIONS,
  };
}

// ── Submit pipeline ────────────────────────────────────────────────────
async function encryptAndSubmit(server, token, text, source, filename) {
  if (!text || !text.trim()) {
    return { ok: false, error: "内容为空" };
  }
  const base = server.replace(/\/+$/, "");
  const payload = await encryptPayload(token, text);
  payload.source = source || "browser";
  if (filename) payload.filename = filename;

  let res;
  try {
    res = await fetch(base + "/inbox/submit", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    return { ok: false, error: `网络错误: ${err.message}` };
  }

  const data = await res.json().catch(() => ({}));
  if (res.ok && data.ok) {
    return {
      ok: true,
      task_id: data.task_id,
      state: data.state,
      inbox_path: data.inbox_path,
    };
  }
  const detail = data.detail || data.message || `HTTP ${res.status}`;
  return { ok: false, error: detail, state: data.state };
}
