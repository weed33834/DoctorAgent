"use strict";

// DoctorAgent Inbox — content script
// Runs in page context to extract the current text selection or the
// page's main textual content.  Has no access to extension storage or
// the API token — it only returns plain text to the popup/background.

/**
 * Return the user's current selection on the page, trimmed.
 * Falls back to an empty string when nothing is selected.
 */
function getSelectionText() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return "";
  return sel.toString().trim();
}

/**
 * Extract the page's main readable content.
 *
 * Priority: <article> → <main> → document body.  Script/style/noscript
 * elements are stripped so only human-visible text remains.  Whitespace
 * is collapsed to keep the payload compact.
 */
function getPageText() {
  const root =
    document.querySelector("article") ||
    document.querySelector("main") ||
    document.body;
  if (!root) return "";

  const clone = root.cloneNode(true);
  clone
    .querySelectorAll("script, style, noscript, iframe, svg, canvas")
    .forEach((el) => el.remove());

  const raw = clone.innerText || clone.textContent || "";
  // Collapse runs of whitespace while preserving paragraph breaks.
  return raw
    .replace(/\r\n/g, "\n")
    .replace(/[ \t\f\v]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// ── Message bridge ─────────────────────────────────────────────────────
// content.js is injected on demand via chrome.scripting.executeScript (it is
// no longer statically listed under content_scripts with <all_urls>). Guard
// against duplicate listener registration if it is injected twice into the
// same page (e.g. the user clicks "send page" twice).
if (!self.__doctoragentContentReady) {
  self.__doctoragentContentReady = true;
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg.type !== "string") return;
    if (msg.type === "GET_SELECTION") {
      sendResponse({ text: getSelectionText() });
    } else if (msg.type === "GET_PAGE_TEXT") {
      sendResponse({ text: getPageText(), title: document.title || "" });
    }
    // Return true to signal an async response is not needed (synchronous here).
    return false;
  });
}
