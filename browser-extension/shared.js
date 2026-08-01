"use strict";

// DoctorAgent Inbox — shared utilities used by both popup.js and background.js.
//
// This file is loaded as a classic <script> in popup.html and via
// importScripts() in the Manifest V3 service worker (background.js).
// It must not use ES module syntax (import/export) so that both contexts
// can consume it without a build step.

/**
 * Generate a filename for a selection submission.
 * @param {number} [ts] — override timestamp (mainly for testing)
 * @returns {string}
 */
function makeSelectionFilename(ts) {
  return `selection-${ts || Date.now()}.txt`;
}

/**
 * Generate a filename for a full-page submission.
 *
 * The page title is sanitised to a short slug (alphanumerics, CJK, hyphens)
 * so the filename is safe across filesystems.
 *
 * @param {string} title — page title (may be empty)
 * @param {number} [ts] — override timestamp (mainly for testing)
 * @returns {string}
 */
function makePageFilename(title, ts) {
  const slug = (title || "page")
    .slice(0, 40)
    .replace(/[^\w\u4e00-\u9fa5-]+/g, "_");
  return `page-${slug}-${ts || Date.now()}.txt`;
}

/**
 * Generate a filename for a manual text submission.
 * @param {number} [ts] — override timestamp (mainly for testing)
 * @returns {string}
 */
function makeManualFilename(ts) {
  return `manual-${ts || Date.now()}.txt`;
}

// Expose on globalThis so both window (popup) and self (worker) see them.
if (typeof globalThis !== "undefined") {
  globalThis.makeSelectionFilename = makeSelectionFilename;
  globalThis.makePageFilename = makePageFilename;
  globalThis.makeManualFilename = makeManualFilename;
}
