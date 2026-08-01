"use strict";

// DoctorAgent Inbox — popup logic
// Manages local settings (server URL + API token) and dispatches
// encryption/submit requests to the background service worker.
// Also provides a Vault tab for browsing and searching archived files.
//
// shared.js is loaded before this file (see popup.html) and exposes:
//   makeSelectionFilename(), makePageFilename(), makeManualFilename()

const $ = (id) => document.getElementById(id);

const els = {
  server: $("server"),
  token: $("token"),
  manual: $("manual"),
  save: $("save"),
  test: $("test"),
  sendManual: $("send-manual"),
  sendSelection: $("send-selection"),
  sendPage: $("send-page"),
  status: $("status"),
  // Vault tab elements
  vaultSearch: $("vault-search"),
  vaultSearchBtn: $("vault-search-btn"),
  vaultRefresh: $("vault-refresh"),
  vaultList: $("vault-list"),
  vaultCount: $("vault-count"),
};

// ── Status helpers ─────────────────────────────────────────────────────
function showStatus(msg, kind = "info", spin = false) {
  els.status.className = "show " + kind;
  els.status.innerHTML = (spin ? '<span class="spin"></span>' : "") + msg;
}

// ── Tab switching ──────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
    tab.classList.add("active");
    const target = tab.dataset.tab;
    const panel = $("tab-" + target);
    if (panel) panel.classList.add("active");
  });
});

// ── Settings persistence ───────────────────────────────────────────────
const getServer = () => els.server.value.trim().replace(/\/+$/, "");
const getToken = () => els.token.value.trim();

async function loadSettings() {
  const { server, token } = await chrome.storage.local.get(["server", "token"]);
  if (server) els.server.value = server;
  if (token) els.token.value = token;
  refreshSendState();
}

async function saveSettings() {
  const server = getServer();
  const token = getToken();
  if (!server) {
    showStatus("服务器地址不能为空", "err");
    return;
  }
  await chrome.storage.local.set({ server, token });
  showStatus("配置已保存", "ok");
  refreshSendState();
}

function refreshSendState() {
  const ready = getToken().length > 0;
  els.sendManual.disabled = !ready || els.manual.value.trim().length === 0;
}

// ── Connection test ────────────────────────────────────────────────────
async function testConnection() {
  const server = getServer();
  const token = getToken();
  if (!server) {
    showStatus("请先填写服务器地址", "err");
    return;
  }
  showStatus("正在连接…", "info", true);
  try {
    const res = await fetch(`${server}/health`, {
      method: "GET",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (res.ok) {
      const data = await res.json();
      showStatus(`连接成功 · ${data.version || "ok"}`, "ok");
    } else if (res.status === 401) {
      showStatus("服务端可达，但令牌无效 (401)", "err");
    } else {
      showStatus(`服务端返回 ${res.status}`, "err");
    }
  } catch (err) {
    showStatus(`无法连接：${err.message}`, "err");
  }
}

// ── Submit via background worker ───────────────────────────────────────
async function submitContent(text, source, filename) {
  const server = getServer();
  const token = getToken();
  if (!token) {
    showStatus("请先填写并保存 API 令牌", "err");
    return;
  }
  showStatus("正在加密发送…", "info", true);
  try {
    const res = await chrome.runtime.sendMessage({
      type: "SUBMIT",
      server,
      token,
      text,
      source,
      filename,
    });
    if (res.ok) {
      const detail = res.state === "COMPLETED" ? "已归档到 Vault" : `状态: ${res.state}`;
      showStatus(`✓ ${detail} · task ${res.task_id.slice(0, 8)}`, "ok");
    } else {
      showStatus(`发送失败: ${res.error || res.state || "未知错误"}`, "err");
    }
  } catch (err) {
    showStatus(`发送异常: ${err.message}`, "err");
  }
}

// ── Page content retrieval ─────────────────────────────────────────────
async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// content.js is injected on demand (activeTab + scripting) rather than
// statically on every page; inject it into the target tab before messaging.
async function ensureContentScript(tabId) {
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
}

async function sendSelection() {
  const tab = await getActiveTab();
  if (!tab) return;
  try {
    await ensureContentScript(tab.id);
    const res = await chrome.tabs.sendMessage(tab.id, { type: "GET_SELECTION" });
    if (res && res.text && res.text.trim()) {
      await submitContent(res.text, "selection", makeSelectionFilename());
    } else {
      showStatus("当前页面没有选中的文字", "err");
    }
  } catch {
    showStatus("无法读取该页面的选区（可能是受限页面）", "err");
  }
}

async function sendPage() {
  const tab = await getActiveTab();
  if (!tab) return;
  try {
    await ensureContentScript(tab.id);
    const res = await chrome.tabs.sendMessage(tab.id, { type: "GET_PAGE_TEXT" });
    if (res && res.text && res.text.trim()) {
      await submitContent(res.text, "page", makePageFilename(res.title));
    } else {
      showStatus("无法提取页面正文", "err");
    }
  } catch {
    showStatus("无法读取该页面的正文（可能是受限页面）", "err");
  }
}

// ── Vault: file listing (GET /vault/files) ─────────────────────────────
async function loadVaultFiles() {
  const server = getServer();
  const token = getToken();
  if (!server || !token) {
    renderVaultEmpty("请先配置服务器地址和令牌");
    return;
  }
  renderVaultLoading();
  try {
    const res = await fetch(`${server}/vault/files?limit=50`, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      renderVaultEmpty(`加载失败: HTTP ${res.status}`);
      return;
    }
    const data = await res.json();
    renderVaultFiles(data.files || []);
  } catch (err) {
    renderVaultEmpty(`加载失败: ${err.message}`);
  }
}

// ── Vault: search (POST /vault/search) ─────────────────────────────────
async function searchVault() {
  const server = getServer();
  const token = getToken();
  const query = els.vaultSearch.value.trim();
  if (!server || !token) {
    renderVaultEmpty("请先配置服务器地址和令牌");
    return;
  }
  if (!query) {
    renderVaultEmpty("请输入搜索关键词");
    return;
  }
  renderVaultLoading();
  try {
    const res = await fetch(`${server}/vault/search`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ query, top_k: 20 }),
    });
    if (!res.ok) {
      renderVaultEmpty(`搜索失败: HTTP ${res.status}`);
      return;
    }
    const results = await res.json();
    // Map search results to the same shape as file list items for rendering.
    const mapped = results.map((r) => ({
      task_id: "",
      vault_path: r.vault_path,
      category: r.category,
      summary: r.summary,
      tags: [],
      score: r.score,
    }));
    renderVaultFiles(mapped, true);
  } catch (err) {
    renderVaultEmpty(`搜索失败: ${err.message}`);
  }
}

// ── Vault rendering helpers ────────────────────────────────────────────
function renderVaultLoading() {
  els.vaultList.innerHTML =
    '<div class="vault-empty"><span class="spin"></span>正在加载…</div>';
  els.vaultCount.textContent = "…";
}

function renderVaultEmpty(msg) {
  els.vaultList.innerHTML = `<div class="vault-empty">${msg}</div>`;
  els.vaultCount.textContent = "0";
}

function renderVaultFiles(files, isSearch) {
  if (!files || files.length === 0) {
    renderVaultEmpty(isSearch ? "未找到匹配的文件" : "Vault 为空");
    return;
  }
  els.vaultCount.textContent = files.length;
  els.vaultList.innerHTML = files
    .map((f) => {
      const cat = f.category
        ? `<span class="vi-cat">${escapeHtml(f.category)}</span>`
        : "";
      const summary = f.summary
        ? `<div class="vi-summary">${escapeHtml(f.summary)}</div>`
        : "";
      const tags = (f.tags || [])
        .map((t) => `<span class="vi-tag">${escapeHtml(t)}</span>`)
        .join("");
      const tagBlock = tags ? `<div class="vi-tags">${tags}</div>` : "";
      const score =
        isSearch && f.score != null
          ? ` <span style="color:var(--text-dim);font-size:9px">★${f.score.toFixed(2)}</span>`
          : "";
      const id = f.task_id ? ` · ${f.task_id.slice(0, 8)}` : "";
      return `<div class="vault-item">${cat}${score}${id}${summary}${tagBlock}</div>`;
    })
    .join("");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = String(str);
  return div.innerHTML;
}

// ── Wire up events ─────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", loadSettings);
els.save.addEventListener("click", saveSettings);
els.test.addEventListener("click", testConnection);
els.manual.addEventListener("input", refreshSendState);
els.sendManual.addEventListener("click", () => {
  const text = els.manual.value.trim();
  if (text) submitContent(text, "manual", makeManualFilename());
});
els.sendSelection.addEventListener("click", sendSelection);
els.sendPage.addEventListener("click", sendPage);
els.vaultRefresh.addEventListener("click", loadVaultFiles);
els.vaultSearchBtn.addEventListener("click", searchVault);
els.vaultSearch.addEventListener("keydown", (e) => {
  if (e.key === "Enter") searchVault();
});
