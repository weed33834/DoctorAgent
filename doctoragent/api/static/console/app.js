// DoctorAgent 控制台前端逻辑。零依赖单文件 SPA。
// 所有 API 调用走 fetch + Bearer token；本地访问可无 token。

(function () {
  "use strict";

  // ── 全局错误兜底（在所有逻辑之前注册，防止静默失败） ──
  // 捕获未处理的 Promise rejection（如 saveChatSessions 配额超限逃逸）
  window.addEventListener("unhandledrejection", function (e) {
    const reason = e.reason;
    const msg = (reason && (reason.message || reason.toString && reason.toString())) || "未知错误";
    // AbortError 是正常的取消操作，不提示
    if (reason && reason.name === "AbortError") { e.preventDefault(); return; }
    console.error("[unhandledrejection]", reason);
    // 延迟 toast 以确保 toastContainer 已就绪
    setTimeout(function () {
      if (typeof toast === "function") toast("发生未捕获错误：" + msg, "error");
    }, 100);
    e.preventDefault();
  });
  // 捕获同步错误
  window.addEventListener("error", function (e) {
    if (e.error && e.error.name === "AbortError") return;
    console.error("[window.onerror]", e.error || e.message);
    setTimeout(function () {
      if (typeof toast === "function") toast("页面错误：" + (e.message || "未知"), "error");
    }, 100);
    return false;
  });

  // ── Token 持久化 ──
  const TOKEN_KEY = "doctoragent_api_token";
  const tokenInput = document.getElementById("tokenInput");
  const saveTokenBtn = document.getElementById("saveTokenBtn");
  function getToken() {
    return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || "";
  }
  function setToken(t) {
    sessionStorage.setItem(TOKEN_KEY, t);
    localStorage.setItem(TOKEN_KEY, t);
  }
  tokenInput.value = getToken();
  // 探测一个需要鉴权的端点，验证 Token 是否真正有效（/health 对本地免鉴权，
  // 无法反映 Token 状态，因此单独探测敏感端点 /api/v1/agent/skills）。
  async function verifyToken() {
    const t = getToken();
    const dot = document.getElementById("healthDot");
    const txt = document.getElementById("healthText");
    if (!t) {
      dot.className = "health-dot unknown";
      txt.textContent = "未设Token";
      return false;
    }
    try {
      await api("/api/v1/agent/skills");
      dot.className = "health-dot ok";
      txt.textContent = "已认证";
      return true;
    } catch (e) {
      if (e.status === 401 || e.status === 403) {
        dot.className = "health-dot fail";
        txt.textContent = "Token无效";
      } else {
        // 网络错误或服务离线，不一定是 Token 问题
        dot.className = "health-dot fail";
        txt.textContent = "离线";
      }
      return false;
    }
  }
  saveTokenBtn.addEventListener("click", async () => {
    setToken(tokenInput.value.trim());
    await checkHealth();
    const ok = await verifyToken();
    toast(ok ? "Token 已保存且有效" : "Token 已保存但鉴权失败", ok ? "success" : "error");
  });
  // 回车也能保存
  tokenInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveTokenBtn.click(); }
  });

  // ── 图表注册表（用于主题切换时刷新）──
  const chartRegistry = {};

  // ── 主题切换 ──
  const THEME_KEY = "doctoragent_theme";
  const themeToggle = document.getElementById("themeToggle");
  const cmdPaletteBtn = document.getElementById("cmdPaletteBtn");
  const helpBtn = document.getElementById("helpBtn");
  const helpModal = document.getElementById("helpModal");
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    themeToggle.textContent = t === "light" ? "☀" : "🌙";
    // 主题切换时重绘所有活跃图表以应用新颜色
    Object.values(chartRegistry).forEach((c) => {
      if (c) {
        const opts = c.options || {};
        if (opts.scales) {
          const tickColor = getCssVar("--text-dim");
          const gridColor = getCssVar("--border");
          if (opts.scales.x && opts.scales.x.ticks) opts.scales.x.ticks.color = tickColor;
          if (opts.scales.y && opts.scales.y.ticks) opts.scales.y.ticks.color = tickColor;
          if (opts.scales.x && opts.scales.x.grid) opts.scales.x.grid.color = gridColor;
          if (opts.scales.y && opts.scales.y.grid) opts.scales.y.grid.color = gridColor;
        }
        c.update("none");
      }
    });
  }
  function getCssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  applyTheme(localStorage.getItem(THEME_KEY) || "dark");
  themeToggle.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  });

  // ── HTTP helper（支持 timeout / signal / 离线检测） ──
  let authWarnTimer = null;
  async function api(path, opts = {}) {
    // 离线检测
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      const e = new Error("网络已断开，请检查连接");
      e.status = 0;
      throw e;
    }
    const headers = Object.assign({}, opts.headers || {});
    const token = getToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    if (opts.body && typeof opts.body === "object"
        && !(opts.body instanceof FormData)
        && !(opts.body instanceof Blob)) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    // timeout：默认 30s，可由 opts.timeoutMs 覆盖；opts.signal 优先（外部取消）
    const timeoutMs = opts.timeoutMs || 30000;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    // 若调用方传了 signal，将其与 timeout signal 联动
    if (opts.signal) {
      if (opts.signal.aborted) ctrl.abort();
      else opts.signal.addEventListener("abort", () => ctrl.abort(), { once: true });
    }
    let res;
    try {
      res = await fetch(path, Object.assign({}, opts, { headers, signal: ctrl.signal }));
    } catch (e) {
      clearTimeout(timer);
      if (e.name === "AbortError") {
        const err = new Error(opts.timeoutMs ? "请求超时" : "请求已取消");
        err.status = 0; err.aborted = true;
        throw err;
      }
      throw e;
    }
    clearTimeout(timer);
    if (!res.ok) {
      let msg = res.status + " " + res.statusText;
      try {
        const j = await res.json();
        if (j.detail) msg = j.detail;
      } catch (e) { /* ignore */ }
      const err = new Error(msg);
      err.status = res.status;
      if ((res.status === 401 || res.status === 403) && !authWarnTimer) {
        toast("鉴权失败：请在右上角配置有效的 API Token", "error", {
          label: "去配置",
          onClick: function () {
            const ti = document.getElementById("tokenInput");
            if (ti) { ti.focus(); ti.select(); }
          },
        });
        // 高亮 Token 输入框引导用户
        const ti = document.getElementById("tokenInput");
        if (ti) {
          ti.style.borderColor = "var(--danger)";
          ti.style.boxShadow = "0 0 0 3px rgba(248,113,113,0.2)";
          setTimeout(function () {
            ti.style.borderColor = "";
            ti.style.boxShadow = "";
          }, 4000);
        }
        authWarnTimer = setTimeout(() => { authWarnTimer = null; }, 4000);
      }
      throw err;
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res.text();
  }

  // ── 通用工具函数 ──
  // debounce：搜索输入防抖
  function debounce(fn, wait) {
    let t = null;
    return function () {
      const args = arguments, ctx = this;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(ctx, args), wait || 200);
    };
  }
  // escapeHtml：防 XSS
  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // parseNum：安全解析数字，NaN 或空值时返回默认值（0 是合法值，不会被丢弃）
  function parseNum(val, defaultVal) {
    if (val == null || val === "") return defaultVal;
    const n = typeof val === "number" ? val : parseFloat(val);
    return isNaN(n) ? defaultVal : n;
  }
  function parseIntSafe(val, defaultVal) {
    if (val == null || val === "") return defaultVal;
    const n = typeof val === "number" ? val : parseInt(val, 10);
    return isNaN(n) ? defaultVal : n;
  }
  // renderError：统一错误组件（带重试按钮）
  function renderError(msg, retryFn) {
    const id = "err" + Date.now();
    const retryHtml = typeof retryFn === "function"
      ? '<button class="btn btn-sm error-retry-btn" data-err-id="' + id + '">重试</button>' : "";
    return '<div class="error-box"><span class="error-icon">⚠</span>' +
      '<span class="error-msg">' + escapeHtml(msg) + '</span>' + retryHtml + '</div>';
  }
  // 全局委托：点击重试按钮
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".error-retry-btn");
    if (!btn) return;
    const box = btn.closest(".error-box");
    if (box && box._retry) { box._retry(); }
  });
  // countUp：数字平滑递增动画
  function countUp(el, target, duration) {
    if (!el) return;
    const start = 0;
    const dur = duration || 800;
    const startTime = performance.now();
    const isFloat = !Number.isInteger(target);
    function step(now) {
      const t = Math.min((now - startTime) / dur, 1);
      const eased = 1 - Math.pow(1 - t, 3); // easeOutCubic
      const val = start + (target - start) * eased;
      el.textContent = isFloat ? val.toFixed(2) : Math.round(val).toLocaleString();
      if (t < 1) requestAnimationFrame(step);
      else el.textContent = isFloat ? Number(target).toFixed(2) : Number(target).toLocaleString();
    }
    requestAnimationFrame(step);
  }
  // relativeTime：ISO 时间 → "3 分钟前"
  function relativeTime(dateStr) {
    if (!dateStr) return "—";
    const d = typeof dateStr === "string" ? new Date(dateStr) : dateStr;
    if (isNaN(d.getTime())) return String(dateStr);
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 2592000) return Math.floor(diff / 86400) + " 天前";
    if (diff < 31536000) return Math.floor(diff / 2592000) + " 个月前";
    return Math.floor(diff / 31536000) + " 年前";
  }

  // ── Toast（堆叠队列 + 行动按钮 + 淡出动画 + aria-live） ──
  const toastContainer = document.getElementById("toastContainer") || document.getElementById("toast");
  function toast(msg, type, actionOpts) {
    if (!toastContainer) return;
    const el = document.createElement("div");
    el.className = "toast" + (type ? " " + type : "");
    el.setAttribute("role", "status");
    let html = '<span class="toast-msg">' + escapeHtml(msg) + "</span>";
    if (actionOpts && actionOpts.label) {
      html += '<button class="toast-action-btn">' + escapeHtml(actionOpts.label) + "</button>";
    }
    html += '<button class="toast-close" aria-label="关闭">×</button>';
    el.innerHTML = html;
    toastContainer.appendChild(el);
    // 入场动画
    requestAnimationFrame(() => {
      el.classList.add("toast-show");
      // success 类型：在 toast 右上角发射小量 confetti 庆祝
      if (type === "success" && typeof window.confetti === "function") {
        try {
          const r = el.getBoundingClientRect();
          window.confetti(r.right - 12, r.top + 10, 8);
        } catch (e) { /* ignore */ }
      }
    });
    // 行动按钮
    if (actionOpts && actionOpts.onClick) {
      el.querySelector(".toast-action-btn").addEventListener("click", () => {
        try { actionOpts.onClick(); } catch (e) { /* ignore */ }
        dismissToast(el);
      });
    }
    el.querySelector(".toast-close").addEventListener("click", () => dismissToast(el));
    // 自动消失
    const timer = setTimeout(() => dismissToast(el), actionOpts && actionOpts.duration || 4000);
    el._timer = timer;
  }
  function dismissToast(el) {
    if (!el || !el.parentNode) return;
    clearTimeout(el._timer);
    el.classList.add("toast-leave");
    el.classList.remove("toast-show");
    setTimeout(() => { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
  }
  // 兼容旧 toastEl 引用（如有代码直接操作 toastEl）
  if (toastContainer && toastContainer.id === "toast") {
    toastContainer.classList.add("toast-container");
  }

  // ── 确认弹窗（危险操作二次确认 + loading 态 + 淡出） ──
  // 用法1：confirmDialog({ title, message, okText, danger }).then(ok => {...})
  // 用法2：confirmDialog({ ..., onConfirm: async () => { await api(...) } })  // 自动 loading
  const confirmModal = document.getElementById("confirmModal");
  let confirmResolve = null;
  function confirmDialog(opts) {
    const o = opts || {};
    document.getElementById("confirmTitle").textContent = o.title || "确认操作";
    document.getElementById("confirmMessage").textContent = o.message || "确定要执行此操作吗？";
    document.getElementById("confirmIcon").textContent = o.icon || "⚠";
    const okBtn = document.getElementById("confirmOk");
    okBtn.textContent = o.okText || "确认";
    okBtn.className = "btn " + (o.danger ? "btn-danger" : "btn-primary");
    confirmModal.classList.remove("hidden", "modal-leaving");
    confirmModal.classList.add("modal-show");
    return new Promise(async (resolve) => {
      confirmResolve = async (result) => {
        if (!result) { _closeConfirm(); resolve(false); return; }
        if (typeof o.onConfirm === "function") {
          // 显示 loading
          okBtn.disabled = true;
          const orig = okBtn.textContent;
          okBtn.innerHTML = '<span class="btn-spinner"></span> 执行中…';
          try { await o.onConfirm(); _closeConfirm(); resolve(true); }
          catch (e) { toast(e.message, "error"); okBtn.disabled = false; okBtn.textContent = orig; }
          return;
        }
        _closeConfirm(); resolve(true);
      };
    });
  }
  function _closeConfirm() {
    confirmModal.classList.add("modal-leaving");
    confirmModal.classList.remove("modal-show");
    setTimeout(() => {
      confirmModal.classList.add("hidden");
      confirmModal.classList.remove("modal-leaving");
    }, 200);
    confirmResolve = null;
  }
  function closeConfirm(result) {
    if (confirmResolve) confirmResolve(result);
  }
  document.getElementById("confirmCancel").addEventListener("click", () => closeConfirm(false));
  document.getElementById("confirmOk").addEventListener("click", () => closeConfirm(true));
  confirmModal.addEventListener("click", (e) => {
    if (e.target === confirmModal) closeConfirm(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !confirmModal.classList.contains("hidden")) closeConfirm(false);
  });

  // ── Tabs（URL hash 路由 + AbortController 取消旧请求） ──
  let currentTabSignal = null;
  // 侧边栏模式不需要滑动指示器
  function moveTabIndicator(tab) { /* no-op in sidebar mode */ }
  function switchTab(view, pushState) {
    const tab = document.querySelector('.sidebar-item[data-view="' + view + '"]');
    if (!tab) return;
    // 取消上一个 tab 的请求
    if (currentTabSignal) { try { currentTabSignal.abort(); } catch (e) {} }
    currentTabSignal = new AbortController();
    // 离开对话页时中止正在进行的流式响应
    if (view !== "chat" && chatState && chatState.abortCtrl) {
      try { chatState.abortCtrl.abort(); } catch (e) { /* ignore */ }
      chatState.abortCtrl = null;
      const sendBtn = document.getElementById("chatSendBtn");
      const stopBtn = document.getElementById("chatStopBtn");
      if (sendBtn) sendBtn.classList.remove("hidden");
      if (stopBtn) stopBtn.classList.add("hidden");
    }
    document.querySelectorAll(".sidebar-item").forEach((t) => { t.classList.remove("active"); t.setAttribute("aria-selected", "false"); });
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    tab.classList.add("active");
    tab.setAttribute("aria-selected", "true");
    const viewEl = document.getElementById("view-" + view);
    if (viewEl) viewEl.classList.add("active");
    moveTabIndicator(tab);
    if (pushState !== false) history.pushState({ view }, "", "#" + view);
    if (view === "vault") loadVaultStatus();
    if (view === "system") loadSystem();
    if (view === "config") loadConfigEditor();
    if (view === "connections") loadConnections();
    if (view === "tenants") loadTenants();
    if (view === "safety") loadSafety();
    if (view === "agents") loadAgentsGraph();
    if (view === "audit") loadAuditStats();
    if (view === "kg") loadKgPage();
    if (view === "dag") loadDagPage();
    if (view === "eval") loadEvalPage();
    if (view === "evo") loadEvoPage();
    if (view === "rag") loadRagPage();
    if (view === "mem") loadMemPage();
    if (view === "hooks") loadHooksPage();
    if (view === "obs") loadObsPage();
    if (view === "rl") loadRlPage();
    if (view === "collab") loadCollabPage();
    if (view === "plugins") loadPluginsPage();
    if (view === "exp") loadExpPage();
    if (view === "prompts") loadPromptsPage();
    if (view === "compliance") loadCompliancePanel();
    if (view === "chat") initChat();
  }
  document.querySelectorAll(".sidebar-item").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.view));
  });
  // 初始化：从 URL hash 恢复
  // 注意：实际 switchTab 调用延迟到 IIFE 末尾（所有 let/const 声明完成后），
  // 否则 view 的 load 函数会访问尚未初始化的变量（TDZ）。
  const initialView = (location.hash || "").replace("#", "") || "chat";
  moveTabIndicator(document.querySelector(".sidebar-item.active"));
  // 浏览器前进/后退
  window.addEventListener("popstate", (e) => {
    const view = (location.hash || "").replace("#", "") || "chat";
    switchTab(view, false);
  });
  // 窗口 resize 时重新定位指示器
  window.addEventListener("resize", () => {
    moveTabIndicator(document.querySelector(".sidebar-item.active"));
  });

  // ── 视图切换（医生视图 / 管理视图） ──
  // 医生视图只显示临床相关标签；管理视图显示全部。
  const CLINICAL_TAB_KEYWORDS = ["chat", "clinical", "phi", "deident", "safety", "rules", "audit"];
  function isClinicalTab(view) {
    const v = (view || "").toLowerCase();
    return CLINICAL_TAB_KEYWORDS.some(function (kw) { return v.indexOf(kw) !== -1; });
  }
  function switchView(view) {
    const tabButtons = document.querySelectorAll(".sidebar-item[data-view]");
    tabButtons.forEach(function (btn) {
      const tabView = btn.dataset.view || "";
      const isClinical = isClinicalTab(tabView);
      if (view === "clinical") {
        btn.style.display = isClinical ? "" : "none";
      } else {
        btn.style.display = ""; // 管理视图显示全部
      }
    });
    try { localStorage.setItem("doctoragent_view", view); } catch (e) {}
    // 若当前激活标签在当前视图不可见，切换到第一个可见标签
    const activeTab = document.querySelector(".sidebar-item.active");
    if (activeTab && activeTab.style.display === "none") {
      const firstVisible = document.querySelector('.tab[data-view]:not([style*="display: none"])');
      if (firstVisible) switchTab(firstVisible.dataset.view, false);
    }
    // 标签显隐变化后重新定位指示器
    moveTabIndicator(document.querySelector(".sidebar-item.active"));
  }
  // 绑定视图切换器
  const viewSwitcher = document.getElementById("viewSwitcher");
  if (viewSwitcher) {
    viewSwitcher.querySelectorAll(".view-tab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        viewSwitcher.querySelectorAll(".view-tab").forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        switchView(btn.dataset.view);
      });
    });
  }
  // 页面加载时恢复视图（延迟到初始 switchTab 之后执行，避免与 hash 路由冲突）
  setTimeout(function () {
    let savedView = "clinical";
    try { savedView = localStorage.getItem("doctoragent_view") || "clinical"; } catch (e) {}
    const targetBtn = document.querySelector('#viewSwitcher .view-tab[data-view="' + savedView + '"]');
    if (targetBtn && !targetBtn.classList.contains("active")) {
      targetBtn.click();
    } else {
      switchView(savedView);
    }
  }, 0);

  // ── Health check ──
  const healthDot = document.getElementById("healthDot");
  const healthText = document.getElementById("healthText");
  async function checkHealth() {
    try {
      const h = await api("/health");
      healthDot.className = "health-dot ok";
      healthText.textContent = "在线";
      document.getElementById("serverVersion").textContent = "v" + h.version;
    } catch (e) {
      healthDot.className = "health-dot fail";
      healthText.textContent = "离线";
    }
  }
  checkHealth();
  // 若已保存 Token，启动时验证一次，让健康指示灯反映鉴权状态。
  if (getToken()) verifyToken();
  // 健康检查定时器：页面可见时轮询，隐藏时暂停以节省资源
  let healthInterval = null;
  function startHealthPolling() {
    if (healthInterval) return;
    healthInterval = setInterval(checkHealth, 30000);
  }
  function stopHealthPolling() {
    if (healthInterval) { clearInterval(healthInterval); healthInterval = null; }
  }
  startHealthPolling();
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stopHealthPolling();
    else { checkHealth(); startHealthPolling(); }
  });
  // 网络状态监听：断网/恢复时即时反馈
  window.addEventListener("offline", function () {
    if (healthDot) healthDot.className = "health-dot fail";
    if (healthText) healthText.textContent = "离线";
    toast("网络已断开，请检查连接", "error");
  });
  window.addEventListener("online", function () {
    toast("网络已恢复", "success");
    checkHealth();
  });

  // 智能对话是默认激活的视图，需在启动时初始化（initChat 定义在下方，
  // 因 chatState 用 let 声明存在 TDZ，故在 IIFE 末尾统一调用，避免引用未初始化变量）。

  // ════════════════════ 临床工作台 ════════════════════

  // ── 检验指标动态表单（替代 JSON textarea） ──
  const LAB_UNITS = {
    sodium: "mmol/L", potassium: "mmol/L", chloride: "mmol/L",
    creatinine: "μmol/L", glucose_fasting: "mmol/L", hba1c: "%",
    hemoglobin: "g/L", wbc: "10^9/L", platelets: "10^9/L",
    inr: "", troponin_i: "ng/mL", ptt: "s", pt: "s",
    d_dimer: "mg/L FEU", ck: "U/L", bnp: "pg/mL",
    tsh: "mIU/L", ldl: "mmol/L", crp: "mg/L", esr: "mm/h",
    ferritin: "ng/mL", alt: "U/L", ast: "U/L",
    bilirubin: "μmol/L", albumin: "g/L", lactate: "mmol/L"
  };
  function addLabRow(name, value, unit) {
    const template = document.getElementById("labRowTemplate");
    if (!template) return;
    const row = template.cloneNode(true);
    row.style.display = "flex";
    row.removeAttribute("id");
    const select = row.querySelector(".lab-name-select");
    const unitInput = row.querySelector(".lab-unit-input");
    const valueInput = row.querySelector(".lab-value-input");
    if (name) select.value = name;
    if (value !== undefined && value !== null && value !== "") valueInput.value = value;
    if (unit) unitInput.value = unit;
    else if (select.value && LAB_UNITS[select.value]) unitInput.value = LAB_UNITS[select.value];
    select.addEventListener("change", function () {
      if (LAB_UNITS[this.value]) unitInput.value = LAB_UNITS[this.value];
    });
    row.querySelector(".lab-remove-btn").addEventListener("click", function () {
      row.remove();
    });
    document.getElementById("labRows").appendChild(row);
  }
  // 从表单收集 labs 数据（转换为后端需要的格式）
  function collectLabs() {
    const labs = [];
    document.querySelectorAll("#labRows .lab-input-row").forEach(function (row) {
      const name = row.querySelector(".lab-name-select").value;
      const value = parseFloat(row.querySelector(".lab-value-input").value);
      const unit = row.querySelector(".lab-unit-input").value;
      if (name && !isNaN(value)) {
        labs.push({ test: name, value: value, unit: unit });
      }
    });
    return labs;
  }
  const labAddBtn = document.getElementById("labAddBtn");
  if (labAddBtn) {
    labAddBtn.addEventListener("click", function () { addLabRow(); });
  }


  const EXAMPLES = {
    safe: {
      patient_id: "synthetic-safe-001",
      medications: ["Lisinopril 10mg PO", "Metformin 500mg PO"],
      allergies: [],
      vitals: { heart_rate: 72, systolic_bp: 118, diastolic_bp: 76 },
      labs: [{ test: "potassium", value: 4.1, unit: "mmol/L" }],
      query: "该患者用药是否安全？",
    },
    "drug-interaction": {
      patient_id: "synthetic-ddi-001",
      medications: ["Warfarin 5mg PO", "Fluconazole 200mg PO"],
      allergies: [],
      vitals: { heart_rate: 78, systolic_bp: 130, diastolic_bp: 85 },
      labs: [{ test: "potassium", value: 4.2, unit: "mmol/L" }],
      query: "该患者用药是否安全？检查药物相互作用。",
    },
    "allergy-alert": {
      patient_id: "synthetic-allergy-001",
      medications: ["Amoxicillin 500mg PO"],
      allergies: ["Penicillin"],
      vitals: { heart_rate: 80, systolic_bp: 120, diastolic_bp: 78 },
      labs: [],
      query: "该患者用药是否安全？检查过敏交叉反应。",
    },
    "critical-vitals": {
      patient_id: "synthetic-vitals-001",
      medications: ["Warfarin 5mg PO"],
      allergies: [],
      vitals: { heart_rate: 35, systolic_bp: 88, diastolic_bp: 55 },
      labs: [{ test: "potassium", value: 6.8, unit: "mmol/L" }],
      query: "该患者生命体征是否危急？",
    },
    "critical-labs": {
      patient_id: "synthetic-labs-001",
      medications: ["Furosemide 40mg PO"],
      allergies: [],
      vitals: { heart_rate: 90, systolic_bp: 150, diastolic_bp: 95 },
      labs: [
        { test: "creatinine", value: 4.5, unit: "mg/dL" },
        { test: "hemoglobin", value: 6.5, unit: "g/dL" },
      ],
      query: "该患者实验室检验是否异常？",
    },
    "duplicate-therapy": {
      patient_id: "synthetic-dup-001",
      medications: ["Omeprazole 20mg PO", "Esomeprazole 40mg PO", "Lisinopril 10mg PO"],
      allergies: [],
      vitals: { heart_rate: 75, systolic_bp: 125, diastolic_bp: 80 },
      labs: [],
      query: "该患者是否存在重复用药？",
    },
  };

  const exampleSelect = document.getElementById("exampleSelect");
  exampleSelect.addEventListener("change", () => {
    const ex = EXAMPLES[exampleSelect.value];
    if (!ex) return;
    document.getElementById("ctx-patient-id").value = ex.patient_id || "";
    document.getElementById("ctx-medications").value = (ex.medications || []).join("\n");
    document.getElementById("ctx-allergies").value = (ex.allergies || []).join(", ");
    document.getElementById("ctx-hr").value = ex.vitals?.heart_rate ?? "";
    document.getElementById("ctx-sbp").value = ex.vitals?.systolic_bp ?? "";
    document.getElementById("ctx-dbp").value = ex.vitals?.diastolic_bp ?? "";
    // 清空现有检验行后填充示例
    document.getElementById("labRows").innerHTML = "";
    (ex.labs || []).forEach(function (l) {
      addLabRow(l.test, l.value, l.unit);
    });
    document.getElementById("ctx-query").value = ex.query || "";
  });

  function buildPatientContext() {
    const meds = document.getElementById("ctx-medications").value
      .split("\n").map((s) => s.trim()).filter(Boolean);
    const allergies = document.getElementById("ctx-allergies").value
      .split(",").map((s) => s.trim()).filter(Boolean);
    const hr = document.getElementById("ctx-hr").value;
    const sbp = document.getElementById("ctx-sbp").value;
    const dbp = document.getElementById("ctx-dbp").value;
    const vitals = {};
    if (hr) vitals.heart_rate = Number(hr);
    if (sbp) vitals.systolic_bp = Number(sbp);
    if (dbp) vitals.diastolic_bp = Number(dbp);
    const labs = collectLabs();
    const ctx = {
      patient_id: document.getElementById("ctx-patient-id").value.trim() || "demo-001",
      medications: meds,
      allergies: allergies,
      vitals: vitals,
      labs: labs,
    };
    return ctx;
  }

  const SEV_ORDER = { contraindicated: 0, critical: 1, warning: 2, info: 3 };
  const SEV_COLOR = {
    critical: "#f87171",
    contraindicated: "#c084fc",
    warning: "#fbbf24",
    info: "#60a5fa",
  };
  const SEV_LABEL = {
    critical: "危急",
    contraindicated: "禁忌",
    warning: "警示",
    info: "提示",
  };
  function sevClass(s) {
    return "sev-" + (s || "info");
  }
  function sevBadge(s) {
    const cls = s || "info";
    return '<span class="badge ' + cls + '">' + cls + "</span>";
  }

  // ── 可视化图表渲染 ──
  // 生命体征正常区间（用于柱状图背景着色与异常高亮）。
  const VITALS_REF = {
    heart_rate: { label: "心率", unit: "bpm", min: 60, max: 100 },
    systolic_bp: { label: "收缩压", unit: "mmHg", min: 90, max: 120 },
    diastolic_bp: { label: "舒张压", unit: "mmHg", min: 60, max: 80 },
  };

  function destroyChart(key) {
    if (chartRegistry[key]) {
      chartRegistry[key].destroy();
      delete chartRegistry[key];
    }
  }

  function renderSeverityChart(findings) {
    const canvas = document.getElementById("severityChart");
    if (!canvas || typeof Chart === "undefined") return;
    destroyChart("severity");
    const counts = { critical: 0, contraindicated: 0, warning: 0, info: 0 };
    (findings || []).forEach((f) => {
      const s = f.severity || "info";
      if (counts[s] !== undefined) counts[s] += 1;
    });
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const labels = Object.keys(counts).map((k) => SEV_LABEL[k] || k);
    const data = Object.values(counts);
    const colors = Object.keys(counts).map((k) => SEV_COLOR[k]);
    chartRegistry.severity = new Chart(canvas, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [{
          data: data,
          backgroundColor: colors,
          borderColor: getCssVar("--panel"),
          borderWidth: 3,
          hoverOffset: 8,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "62%",
        plugins: {
          legend: {
            position: "right",
            labels: {
              color: getCssVar("--text-dim"),
              font: { size: 11 },
              padding: 10,
              boxWidth: 12,
              boxHeight: 12,
              generateLabels: function (chart) {
                return chart.data.labels.map((label, i) => ({
                  text: label + " · " + chart.data.datasets[0].data[i],
                  fillStyle: chart.data.datasets[0].backgroundColor[i],
                  strokeStyle: chart.data.datasets[0].backgroundColor[i],
                  index: i,
                }));
              },
            },
          },
          tooltip: {
            backgroundColor: getCssVar("--panel-3"),
            titleColor: getCssVar("--text"),
            bodyColor: getCssVar("--text-dim"),
            borderColor: getCssVar("--border"),
            borderWidth: 1,
            callbacks: {
              label: function (ctx) {
                const v = ctx.parsed;
                const pct = total ? ((v / total) * 100).toFixed(0) : 0;
                return " " + v + " 条 (" + pct + "%)";
              },
            },
          },
        },
        animation: { animateRotate: true, animateScale: true, duration: 600 },
      },
    });
  }

  function renderVitalsChart(vitals) {
    const canvas = document.getElementById("vitalsChart");
    if (!canvas || typeof Chart === "undefined") return;
    destroyChart("vitals");
    const keys = Object.keys(VITALS_REF).filter((k) => vitals && vitals[k] !== undefined && vitals[k] !== null);
    if (!keys.length) {
      chartRegistry.vitals = new Chart(canvas, {
        type: "bar",
        data: { labels: ["暂无生命体征"], datasets: [{ data: [0], backgroundColor: getCssVar("--panel-2") }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } },
      });
      return;
    }
    const labels = keys.map((k) => VITALS_REF[k].label);
    const values = keys.map((k) => vitals[k]);
    // 异常值高亮：超出正常区间的柱子用危险色，否则用主色。
    const bgColors = keys.map((k) => {
      const ref = VITALS_REF[k];
      const v = vitals[k];
      return (v < ref.min || v > ref.max) ? SEV_COLOR.critical : getCssVar("--primary");
    });
    // 正常区间以背景区域注释呈现。
    const refAnnotations = keys.map((k) => {
      const ref = VITALS_REF[k];
      return { min: ref.min, max: ref.max };
    });
    chartRegistry.vitals = new Chart(canvas, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          {
            label: "测量值",
            data: values,
            backgroundColor: bgColors,
            borderRadius: 6,
            barPercentage: 0.55,
            categoryPercentage: 0.7,
          },
          {
            label: "正常上限",
            data: keys.map((k) => VITALS_REF[k].max),
            type: "line",
            borderColor: SEV_COLOR.info,
            borderDash: [5, 4],
            borderWidth: 1.5,
            pointRadius: 0,
            fill: false,
            tension: 0,
          },
          {
            label: "正常下限",
            data: keys.map((k) => VITALS_REF[k].min),
            type: "line",
            borderColor: SEV_COLOR.info,
            borderDash: [5, 4],
            borderWidth: 1.5,
            pointRadius: 0,
            fill: false,
            tension: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: { color: getCssVar("--text-dim"), font: { size: 10 }, boxWidth: 12, boxHeight: 12, padding: 8 },
          },
          tooltip: {
            backgroundColor: getCssVar("--panel-3"),
            titleColor: getCssVar("--text"),
            bodyColor: getCssVar("--text-dim"),
            borderColor: getCssVar("--border"),
            borderWidth: 1,
            callbacks: {
              afterLabel: function (ctx) {
                if (ctx.datasetIndex === 0) {
                  const ref = VITALS_REF[keys[ctx.dataIndex]];
                  const v = values[ctx.dataIndex];
                  const status = (v < ref.min || v > ref.max) ? "⚠ 异常" : "✓ 正常";
                  return "正常区间 " + ref.min + "–" + ref.max + " " + ref.unit + " · " + status;
                }
                return "";
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: getCssVar("--text-dim"), font: { size: 11 } },
            grid: { color: getCssVar("--border"), display: false },
          },
          y: {
            beginAtZero: true,
            ticks: { color: getCssVar("--text-dim"), font: { size: 10 } },
            grid: { color: getCssVar("--border") },
          },
        },
        animation: { duration: 700, easing: "easeOutQuart" },
      },
    });
  }

  function renderClinicalResult(r) {
    const out = document.getElementById("clinicalResult");
    const reviewBadge = document.getElementById("reviewBadge");
    const chartsBox = document.getElementById("clinicalCharts");
    reviewBadge.classList.toggle("hidden", !r.requires_human_review);

    // 渲染图表（严重度分布 + 生命体征）
    const hasFindings = r.safety_findings && r.safety_findings.length;
    const ctx = buildPatientContext();
    const hasVitals = ctx.vitals && Object.keys(ctx.vitals).length;
    if (hasFindings || hasVitals) {
      chartsBox.classList.remove("hidden");
      renderSeverityChart(r.safety_findings || []);
      renderVitalsChart(ctx.vitals);
    } else {
      chartsBox.classList.add("hidden");
    }

    let html = "";

    // 患者上下文快照（用药/过敏 chips）
    if (ctx.medications.length || ctx.allergies.length) {
      html += '<div class="section-label">患者上下文快照</div>';
      if (ctx.medications.length) {
        html += '<div class="med-list">';
        ctx.medications.forEach((m) => {
          const parts = m.split(/\s+/);
          const drug = parts[0] || m;
          const dose = parts.slice(1).join(" ");
          html += '<span class="med-chip">💊 ' + escapeHtml(drug) +
            (dose ? '<span class="dose">' + escapeHtml(dose) + "</span>" : "") + "</span>";
        });
        html += "</div>";
      }
      if (ctx.allergies.length) {
        html += '<div class="allergy-list">';
        ctx.allergies.forEach((a) => {
          html += '<span class="allergy-chip">⚠ ' + escapeHtml(a) + "</span>";
        });
        html += "</div>";
      }
    }

    // 智能体执行时间轴（基于结果阶段合成）
    html += renderExecutionTimeline(r);

    // 安全发现
    if (r.safety_findings && r.safety_findings.length) {
      html += '<div class="section-label">安全发现（确定性规则引擎）</div>';
      const sorted = r.safety_findings.slice().sort((a, b) =>
        (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));
      sorted.forEach((f, i) => {
        html += '<div class="finding ' + sevClass(f.severity) + '" style="animation-delay:' + (i * 0.05) + 's">';
        html += '<div class="finding-head">';
        html += '<span class="finding-rule">' + escapeHtml(f.rule_type || "") + "</span>";
        html += sevBadge(f.severity);
        html += "</div>";
        html += '<div class="finding-text">' + escapeHtml(f.finding || "") + "</div>";
        if (f.recommendation)
          html += '<div class="finding-rec">建议：' + escapeHtml(f.recommendation) + "</div>";
        if (f.source)
          html += '<div class="finding-source">来源：' + escapeHtml(f.source) + "</div>";
        if (f.affected_resources && f.affected_resources.length)
          html += '<div class="finding-source">涉及：' +
            escapeHtml(f.affected_resources.join(", ")) + "</div>";
        html += "</div>";
      });
    }

    // 病史摘要
    if (r.history_summary) {
      html += '<div class="section-label">病史摘要</div>';
      html += '<div class="finding sev-info"><div class="finding-text">' +
        escapeHtml(r.history_summary) + "</div></div>";
    }

    // 文献
    if (r.literature && r.literature.length) {
      html += '<div class="section-label">文献证据</div>';
      r.literature.forEach((l) => {
        html += '<div class="finding sev-info">';
        if (l.title) html += '<div class="finding-text">' + escapeHtml(l.title) + "</div>";
        if (l.summary) html += '<div class="finding-rec">' + escapeHtml(l.summary) + "</div>";
        if (l.pmid) html += '<div class="finding-source">PMID: ' + escapeHtml(String(l.pmid)) + "</div>";
        html += "</div>";
      });
    }

    // 文档草稿（SOAP 卡片化展示）
    if (r.documentation) {
      html += renderDocumentationCard(r.documentation);
    }

    // 护栏结果
    if (r.guardrail_result && Object.keys(r.guardrail_result).length) {
      const g = r.guardrail_result;
      html += '<div class="section-label">护栏审查</div>';
      html += '<div class="finding ' + sevClass(g.action === "block" ? "critical" : "warning") + '">';
      html += '<div class="finding-head"><span class="finding-rule">GUARDRAIL</span>' +
        sevBadge(g.action) + "</div>";
      html += '<div class="finding-text">通过：' + (g.passed ? "是" : "否") + "</div>";
      if (g.warnings && g.warnings.length)
        g.warnings.forEach((w) =>
          html += '<div class="finding-rec">⚠ ' + escapeHtml(w) + "</div>");
      if (g.violations && g.violations.length)
        g.violations.forEach((v) =>
          html += '<div class="finding-rec">✗ ' + escapeHtml(v) + "</div>");
      html += "</div>";
    }

    // 引用
    if (r.citations && r.citations.length) {
      html += '<div class="section-label">引用</div><ul class="citation-list">';
      r.citations.forEach((c) => html += "<li>" + escapeHtml(c) + "</li>");
      html += "</ul>";
    }

    // 免责声明
    if (r.disclaimer) {
      html += '<div class="disclaimer">⚠ ' + escapeHtml(r.disclaimer) + "</div>";
    }

    if (!html) html = '<p class="placeholder">无结果返回。</p>';
    out.innerHTML = html;
  }

  // 基于工作流结果合成执行时间轴（确定性规则 → 专家 → 护栏）。
  function renderExecutionTimeline(r) {
    const items = [];
    // 确定性规则阶段
    const findings = r.safety_findings || [];
    const criticalCount = findings.filter((f) =>
      f.severity === "critical" || f.severity === "contraindicated").length;
    items.push({
      status: criticalCount ? "warning" : "success",
      title: "确定性规则引擎",
      body: findings.length
        ? "命中 " + findings.length + " 条规则（" + criticalCount + " 条阻断级）"
        : "未命中规则，患者上下文在安全阈值内",
    });
    // 专家 Agent 阶段
    if (r.history_summary) {
      items.push({
        status: "success",
        title: "病史专家 Agent",
        body: "已生成结构化病史摘要",
      });
    }
    if (r.literature && r.literature.length) {
      items.push({
        status: "success",
        title: "文献专家 Agent",
        body: "检索到 " + r.literature.length + " 条证据",
      });
    }
    // 文书阶段
    if (r.documentation) {
      items.push({
        status: "success",
        title: "文书 Agent",
        body: "已生成 SOAP 草稿" +
          (r.documentation.icd10_codes && r.documentation.icd10_codes.length
            ? " · ICD-10 " + r.documentation.icd10_codes.length + " 项" : ""),
      });
    }
    // 护栏审查阶段
    if (r.guardrail_result && Object.keys(r.guardrail_result).length) {
      const g = r.guardrail_result;
      items.push({
        status: g.action === "block" ? "error" : (g.action === "flag" ? "warning" : "success"),
        title: "护栏审查",
        body: "动作：" + (g.action || "allow") + " · " + (g.passed ? "通过" : "未通过"),
      });
    }
    if (!items.length) return "";
    let html = '<div class="section-label">执行时间轴</div><div class="timeline">';
    items.forEach((it) => {
      html += '<div class="timeline-item ' + it.status + '">';
      html += '<div class="timeline-title">' + escapeHtml(it.title) + "</div>";
      html += '<div class="timeline-body">' + escapeHtml(it.body) + "</div>";
      html += "</div>";
    });
    html += "</div>";
    return html;
  }

  // 把 documentation 渲染为 SOAP 四象限卡片；若仅含纯文本 draft 则回退到 pre。
  function renderDocumentationCard(doc) {
    const draft = doc.draft || doc.soap_note || "";
    // 尝试按 SOAP 段落切分。
    const sections = parseSoap(draft);
    let html = '<div class="section-label">文档草稿（SOAP）</div>';
    if (sections) {
      html += '<div class="soap-grid">';
      ["S", "O", "A", "P"].forEach((k) => {
        const meta = { S: "主观 (S)", O: "客观 (O)", A: "评估 (A)", P: "计划 (P)" }[k];
        html += '<div class="soap-section"><h5>' + meta + "</h5><p>" +
          escapeHtml(sections[k] || "—") + "</p></div>";
      });
      html += "</div>";
    } else if (draft) {
      html += "<pre>" + escapeHtml(draft) + "</pre>";
    }
    if (doc.icd10_codes && doc.icd10_codes.length) {
      html += '<div class="med-list" style="margin-top:10px">';
      doc.icd10_codes.forEach((c) => {
        html += '<span class="med-chip">🏷 ' + escapeHtml(c) + "</span>";
      });
      html += "</div>";
    }
    return html;
  }

  function parseSoap(text) {
    if (!text) return null;
    // 匹配 S/O/A/P 段落标题（支持中英文 + 冒号/换行）。
    const re = /(?:^|\n)\s*(?:S|主观|Subjective)\s*[:：]\s*([\s\S]*?)(?=\n\s*(?:O|客观|Objective)\s*[:：]|$)/i;
    const ro = /(?:^|\n)\s*(?:O|客观|Objective)\s*[:：]\s*([\s\S]*?)(?=\n\s*(?:A|评估|Assessment)\s*[:：]|$)/i;
    const ra = /(?:^|\n)\s*(?:A|评估|Assessment)\s*[:：]\s*([\s\S]*?)(?=\n\s*(?:P|计划|Plan)\s*[:：]|$)/i;
    const rp = /(?:^|\n)\s*(?:P|计划|Plan)\s*[:：]\s*([\s\S]*?)$/i;
    const s = text.match(re);
    const o = text.match(ro);
    const a = text.match(ra);
    const p = text.match(rp);
    if (!s && !o && !a && !p) return null;
    return {
      S: (s ? s[1] : "").trim(),
      O: (o ? o[1] : "").trim(),
      A: (a ? a[1] : "").trim(),
      P: (p ? p[1] : "").trim(),
    };
  }

  // escapeHtml 已在上方工具函数区统一定义

  const runBtn = document.getElementById("runClinicalBtn");
  const clinicalLoading = document.getElementById("clinicalLoading");
  runBtn.addEventListener("click", async () => {
    let ctx;
    try { ctx = buildPatientContext(); }
    catch (e) { toast(e.message, "error"); return; }
    const query = document.getElementById("ctx-query").value.trim();
    if (!query) { toast("请填写临床问题", "error"); return; }

    runBtn.disabled = true;
    clinicalLoading.classList.remove("hidden");
    document.getElementById("clinicalResult").innerHTML = "";
    document.getElementById("clinicalCharts").classList.add("hidden");
    destroyChart("severity");
    destroyChart("vitals");
    try {
      const r = await api("/clinical/analyze", {
        method: "POST",
        body: { patient_context: ctx, query: query },
      });
      renderClinicalResult(r);
      toast(
        r.requires_human_review ? "分析完成 — 需医生复核" : "分析完成",
        r.requires_human_review ? "error" : "success"
      );
    } catch (e) {
      document.getElementById("clinicalResult").innerHTML =
        '<p class="placeholder" style="color:var(--danger)">请求失败：' +
        escapeHtml(e.message) + "</p>";
      toast("分析失败：" + e.message, "error");
    } finally {
      runBtn.disabled = false;
      clinicalLoading.classList.add("hidden");
    }
  });

  // ════════════════════ 文档 Vault ════════════════════

  async function loadVaultStatus() {
    try {
      const s = await api("/vault/status");
      document.getElementById("stat-inbox").textContent = s.inbox_files ?? 0;
      document.getElementById("stat-vault").textContent = s.vault_files ?? 0;
      const cats = s.categories || {};
      document.getElementById("stat-cats").textContent =
        Object.keys(cats).length + " 类";
      renderVaultTree(cats);
    } catch (e) { /* 静默 */ }
  }

  // ── Vault 文件浏览树（轻量分类列表） ──
  function renderVaultTree(categories) {
    const body = document.getElementById("vaultTreeBody");
    if (!body) return;
    const keys = Object.keys(categories || {});
    if (!keys.length) {
      body.innerHTML = emptyState("📂", "暂无分类", "Vault 中尚无分类文档");
      return;
    }
    body.innerHTML = keys.map((k) => {
      const v = categories[k];
      const count = Array.isArray(v) ? v.length : (typeof v === "number" ? v : 0);
      return '<div class="vault-tree-item">' +
        '<span class="vault-tree-cat">' + escapeHtml(k) + "</span>" +
        '<span class="vault-tree-count">' + count + "</span>" +
        "</div>";
    }).join("");
  }

  document.getElementById("vaultTreeToggle").addEventListener("click", () => {
    document.getElementById("vaultTree").classList.toggle("collapsed");
  });

  // ── Vault 通用渲染辅助 ──
  function vaultSkeleton(items) {
    return '<div class="skeleton-list">' +
      Array.from({length: items || 3}).map(() =>
        '<div class="skeleton-item"><div class="skeleton-line w40"></div>' +
        '<div class="skeleton-line w80"></div>' +
        '<div class="skeleton-line w60"></div></div>'
      ).join("") + "</div>";
  }

  // 关键词高亮：在已转义文本上按 query 词项包裹 <mark>
  function vaultHighlight(escapedText, query) {
    if (!query) return escapedText;
    const terms = String(query).trim().split(/\s+/).filter(Boolean);
    if (!terms.length) return escapedText;
    const re = new RegExp("(" + terms.map((t) =>
      escapeHtml(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    ).join("|") + ")", "gi");
    return escapedText.replace(re, '<mark class="vault-mark">$&</mark>');
  }

  // score 进度条（0..1，按 max 归一化）
  function vaultScoreBar(score, max) {
    const m = (max && max > 0) ? max : 1;
    const pct = Math.round(Math.max(0, Math.min(1, (score ?? 0) / m)) * 100);
    return '<div class="vault-score">' +
      '<div class="vault-score-bar"><div class="vault-score-fill" style="width:' + pct + '%"></div></div>' +
      '<span class="vault-score-label">' + (score ?? 0).toFixed(3) + "</span>" +
      "</div>";
  }

  // RAG 引用源卡片网格
  function renderVaultSources(sources) {
    if (!Array.isArray(sources) || !sources.length) return "";
    const max = Math.max.apply(null, sources.map((s) => s.score ?? 0).concat([1]));
    return '<div class="vault-section-label">引用来源 · ' + sources.length + "</div>" +
      '<div class="vault-sources">' +
      sources.map((src, i) =>
        '<div class="vault-source-card" style="animation-delay:' + (i * 60) + 'ms">' +
        '<div class="vault-source-head">' +
        '<span class="vault-source-docid">' + escapeHtml(src.doc_id || src.id || "—") + "</span>" +
        "</div>" +
        '<div class="vault-source-title">' + escapeHtml(src.title || "未命名文档") + "</div>" +
        '<div class="vault-source-snippet">' + escapeHtml(src.snippet || src.summary || "") + "</div>" +
        '<button class="vault-source-more" data-toggle="vault-source" type="button">展开</button>' +
        vaultScoreBar(src.score ?? 0, max) +
        "</div>"
      ).join("") + "</div>";
  }

  document.getElementById("searchBtn").addEventListener("click", async () => {
    const q = document.getElementById("searchQuery").value.trim();
    if (!q) return;
    const semantic = document.getElementById("searchSemantic").checked;
    const out = document.getElementById("searchResults");
    out.innerHTML = vaultSkeleton(3);
    try {
      const r = await api("/vault/search", {
        method: "POST",
        body: { query: q, top_k: 10, semantic: semantic },
      });
      const results = r.results || r;
      if (!Array.isArray(results) || !results.length) {
        out.innerHTML = emptyState("🔍", "无匹配结果", "试试调整关键词或开启语义搜索");
        return;
      }
      const maxScore = Math.max.apply(null, results.map((it) => it.score ?? 0).concat([1]));
      out.innerHTML = results.map((it, i) => {
        const score = it.score ?? 0;
        const snippet = vaultHighlight(escapeHtml(it.summary || it.snippet || ""), q);
        const path = vaultHighlight(escapeHtml(it.vault_path || it.path || ""), q);
        return '<div class="search-item" style="animation-delay:' + (i * 50) + 'ms">' +
          '<div class="search-item-head">' +
          '<span class="search-item-cat">' + escapeHtml(it.category || "文档") + "</span>" +
          "</div>" +
          '<div class="search-item-path">' + path + "</div>" +
          '<div class="search-item-summary">' + snippet + "</div>" +
          vaultScoreBar(score, maxScore) +
          "</div>";
      }).join("");
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' +
        escapeHtml(e.message) + "</p>";
    }
  });

  document.getElementById("askBtn").addEventListener("click", async () => {
    const q = document.getElementById("askQuestion").value.trim();
    if (!q) return;
    const out = document.getElementById("askResult");
    out.innerHTML = vaultSkeleton(2);
    try {
      const r = await api("/vault/ask", {
        method: "POST",
        body: { question: q, top_k: 5, use_memory: true },
      });
      const answer = r.answer || r.result || "";
      const sources = r.sources || r.references || [];
      if (!answer && !sources.length) {
        out.innerHTML = emptyState("💬", "暂无回答", "Agent 未能生成回答");
        return;
      }
      let html = "";
      if (answer) {
        html += '<div class="vault-section-label">回答</div>' +
          '<div class="vault-answer">' + escapeHtml(answer) + "</div>";
      }
      if (sources.length) html += renderVaultSources(sources);
      out.innerHTML = html;
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' +
        escapeHtml(e.message) + "</p>";
    }
  });

  document.getElementById("agentBtn").addEventListener("click", async () => {
    const t = document.getElementById("agentTask").value.trim();
    if (!t) return;
    const out = document.getElementById("agentResult");
    out.innerHTML = vaultSkeleton(3);
    try {
      const r = await api("/vault/agent", {
        method: "POST",
        body: { task: t, max_iterations: 10 },
      });
      const steps = r.steps || [];
      const answer = r.answer || r.result || r.summary || "";
      if (!answer && !steps.length) {
        out.innerHTML = emptyState("🤖", "Agent 未返回结果", "尝试调整任务描述");
        return;
      }
      let html = "";
      const meta = [];
      if (steps.length) meta.push("步骤 " + steps.length);
      if (r.total_tool_calls) meta.push("工具调用 " + r.total_tool_calls);
      if (r.iterations) meta.push("迭代 " + r.iterations);
      if (meta.length) {
        html += '<div class="vault-agent-meta">' +
          meta.map((m) => "<span>" + escapeHtml(m) + "</span>").join("") + "</div>";
      }
      if (answer) {
        html += '<div class="vault-agent-summary">' + escapeHtml(answer) + "</div>";
      }
      if (steps.length) {
        html += '<div class="vault-section-label">执行步骤 · ' + steps.length + "</div>";
        html += steps.map((s, i) =>
          '<div class="vault-step" style="animation-delay:' + (i * 60) + 'ms">' +
          '<div class="vault-step-head">' +
          '<span class="vault-step-num">' + (i + 1) + "</span>" +
          '<span class="vault-step-type">' + escapeHtml(s.step_type || "step") + "</span>" +
          (s.tool_name ? '<span class="vault-step-tool">' + escapeHtml(s.tool_name) + "</span>" : "") +
          "</div>" +
          '<div class="vault-step-body">' + escapeHtml(s.content || s.output || "") + "</div>" +
          "</div>"
        ).join("");
      }
      out.innerHTML = html || emptyState("🤖", "Agent 未返回结果", "尝试调整任务描述");
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' +
        escapeHtml(e.message) + "</p>";
    }
  });

  // 引用源 snippet 展开/收起（事件委托，整页一次）
  document.addEventListener("click", (e) => {
    const btn = e.target.closest('[data-toggle="vault-source"]');
    if (!btn) return;
    const card = btn.closest(".vault-source-card");
    if (!card) return;
    const expanded = card.classList.toggle("expanded");
    btn.textContent = expanded ? "收起" : "展开";
  });

  // ════════════════════ 审计日志 ════════════════════

  async function loadAuditStats() {
    try {
      const s = await api("/audit/statistics");
      document.getElementById("as-total").textContent = (s.total_events || 0).toLocaleString();
      const sev = s.by_severity || {};
      document.getElementById("as-critical").textContent = sev.CRITICAL || 0;
      document.getElementById("as-high").textContent = sev.HIGH || 0;
      document.getElementById("as-medium").textContent = sev.MEDIUM || 0;
      document.getElementById("as-types").textContent = Object.keys(s.by_event_type || {}).length;
    } catch (e) { /* 忽略，统计可选 */ }
  }

  function sevBadgeClass(sev) {
    const s = (sev || "").toUpperCase();
    if (s === "CRITICAL") return "critical";
    if (s === "HIGH") return "warning";
    if (s === "MEDIUM") return "info";
    return "";
  }

  document.getElementById("auditQueryBtn").addEventListener("click", async () => {
    const ev = document.getElementById("auditEvent").value.trim();
    const sevFilter = document.getElementById("auditSeverity").value;
    const limit = document.getElementById("auditLimit").value || 100;
    const out = document.getElementById("auditResult");
    out.innerHTML = '<div class="skeleton-list">' +
      Array.from({length: 3}).map(() =>
        '<div class="skeleton-item"><div class="skeleton-line w30"></div><div class="skeleton-line w80"></div></div>'
      ).join("") + "</div>";
    try {
      const params = new URLSearchParams();
      if (ev) params.set("event_type", ev);
      params.set("limit", limit);
      const r = await api("/audit/logs?" + params.toString());
      let logs = Array.isArray(r) ? r : (r.records || []);
      // 前端按严重度二次筛选（后端不支持 severity 过滤）
      if (sevFilter) {
        logs = logs.filter((l) => (l.severity || "").toUpperCase() === sevFilter);
      }
      if (!logs.length) {
        out.innerHTML = emptyState("📋", "无审计记录", "调整筛选条件后重试");
        return;
      }
      let html = '<div class="section-label">共 ' + logs.length + " 条记录</div>";
      html += '<table class="data-table audit-table"><thead><tr>' +
        "<th>时间</th><th>事件</th><th>级别</th><th>HMAC</th><th>详情</th></tr></thead><tbody>";
      logs.forEach((l) => {
        const ts = l.timestamp || l.ts || "";
        const ev2 = l.event_type || l.event || "";
        const sev = l.severity || "INFO";
        const hmac = l.hmac || "";
        const detail = l.details || l.detail || l.message || {};
        const detailStr = typeof detail === "object" ? JSON.stringify(detail) : String(detail);
        html += "<tr>" +
          '<td class="audit-time mono">' + escapeHtml(ts.replace("T", " ").replace(/\.\d+.*$/, "")) + "</td>" +
          '<td class="audit-event">' + escapeHtml(ev2) + "</td>" +
          '<td><span class="badge ' + sevBadgeClass(sev) + '">' + escapeHtml(sev) + "</span></td>" +
          '<td class="audit-hmac mono" title="' + escapeHtml(hmac) + '">' +
            (hmac ? escapeHtml(hmac.slice(0, 8)) + "…" : "—") + "</td>" +
          '<td class="audit-detail">' + escapeHtml(detailStr.length > 80 ? detailStr.slice(0, 80) + "…" : detailStr) + "</td>" +
          "</tr>";
      });
      html += "</tbody></table>";
      out.innerHTML = html;
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' +
        escapeHtml(e.message) + "</p>";
    }
  });

  document.getElementById("auditVerifyBtn").addEventListener("click", async () => {
    const out = document.getElementById("auditResult");
    const pill = document.getElementById("auditVerifyPill");
    setPill("auditVerifyPill", "校验中…", "warn");
    out.innerHTML = '<p class="placeholder">校验 HMAC 链完整性…</p>';
    try {
      const r = await api("/audit/verify");
      const tampered = r.tampered || r.valid === false;
      const total = r.total || r.checked || r.entries || "—";
      setPill("auditVerifyPill", tampered ? "检测到篡改" : "完整性通过", tampered ? "fail" : "ok");
      let html = '<div class="verify-banner ' + (tampered ? "fail" : "ok") + '">' +
        '<span class="verify-icon">' + (tampered ? "⚠" : "✓") + "</span>" +
        "<div><div class='verify-title'>" + (tampered ? "检测到篡改" : "完整性校验通过") + "</div>" +
        "<div class='verify-sub'>审计链 HMAC-SHA256 校验" + (tampered ? "发现异常" : "全部通过") + "</div></div></div>";
      html += '<div class="kv-grid">';
      html += kvCell("校验结果", tampered ? "篡改" : "通过");
      html += kvCell("校验条数", total);
      if (r.tampered_count != null) html += kvCell("篡改条数", r.tampered_count);
      if (r.message) html += kvCell("消息", r.message);
      html += "</div>";
      out.innerHTML = html;
      toast(tampered ? "检测到篡改！" : "完整性校验通过", tampered ? "error" : "success");
    } catch (e) {
      setPill("auditVerifyPill", "校验失败", "fail");
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' +
        escapeHtml(e.message) + "</p>";
    }
  });

  document.getElementById("auditExportBtn").addEventListener("click", async () => {
    try {
      const r = await api("/audit/export", {
        method: "POST",
        body: { format: "ndjson" },
      });
      const blob = new Blob([typeof r === "string" ? r : JSON.stringify(r)],
        { type: "application/x-ndjson" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "audit-export-" + new Date().toISOString().slice(0, 10) + ".ndjson";
      a.click();
      URL.revokeObjectURL(url);
      toast("已导出 NDJSON", "success");
    } catch (e) { toast("导出失败：" + e.message, "error"); }
  });

  // ════════════════════ 系统状态 ════════════════════

  async function loadSystem() {
    const heroStatus = document.getElementById("sysHeroStatus");
    const heroTitle = document.getElementById("sysHeroTitle");
    const heroSub = document.getElementById("sysHeroSub");
    heroStatus.innerHTML = '<span class="health-dot unknown"></span>';
    heroSub.textContent = "检测中…";
    const setMetric = (id, v) => { document.getElementById(id).textContent = v; };

    let health = null, config = null, stats = null, pool = null;
    try { health = await api("/health"); } catch (e) { /* ignore */ }
    try { config = await api("/config"); } catch (e) { /* ignore */ }
    try { stats = await api("/audit/statistics"); } catch (e) { /* ignore */ }
    try { pool = await api("/system/pipeline-pool"); } catch (e) { /* ignore */ }

    if (!health) {
      heroStatus.innerHTML = '<span class="health-dot fail"></span>';
      heroTitle.textContent = "DoctorAgent";
      heroSub.textContent = "服务离线";
      setMetric("m-status", "离线");
      return;
    }
    heroStatus.innerHTML = '<span class="health-dot ok"></span>';
    heroTitle.textContent = health.app_name || "DoctorAgent";
    heroSub.textContent = "运行中 · v" + health.version;
    setMetric("m-status", "在线");
    setMetric("m-version", "v" + (health.version || "—"));
    setMetric("m-env", config ? (config.env || "—") : "—");

    if (config) {
      const m = config.model || {};
      const s = config.security || {};
      setMetric("m-model", m.model_name || "—");
      setMetric("m-ctx", m.ctx_size ? m.ctx_size.toLocaleString() : "—");
      setMetric("m-enc", s.encryption || "—");
      setMetric("m-kdf", s.kdf || "—");

      // 配置卡片
      const cardsEl = document.getElementById("sysConfigCards");
      const cards = [
        { title: "模型", items: [
          ["Base URL", m.base_url],
          ["模型", m.model_name],
          ["上下文窗口", m.ctx_size],
          ["Temperature", m.temperature],
          ["超时(秒)", m.timeout],
          ["Fallback", m.fallback_model_name || "无"],
        ]},
        { title: "安全", items: [
          ["加密", s.encryption],
          ["KDF", s.kdf],
          ["主密钥提供方", s.master_key_provider],
          ["语义搜索", s.enable_semantic_search ? "启用" : "关闭"],
          ["Windows Hello", s.windows_hello_enabled ? "启用" : "关闭"],
          ["云回退", s.cloud_fallback_enabled ? "启用" : "关闭"],
        ]},
      ];
      cardsEl.innerHTML = cards.map((c) =>
        '<div class="config-card"><h4>' + escapeHtml(c.title) + "</h4>" +
        c.items.map((it) =>
          '<div class="kv-row"><span class="kv-key">' + escapeHtml(it[0]) +
          '</span><span class="kv-val">' + escapeHtml(it[1] == null ? "—" : String(it[1])) +
          "</span></div>"
        ).join("") + "</div>"
      ).join("");

      // 脱敏后原始 JSON
      let masked;
      try { masked = JSON.parse(JSON.stringify(config)); }
      catch (e) { masked = Object.assign({}, config); } // 深拷贝失败回退浅拷贝
      if (masked.security) {
        if (masked.security.master_key_password) masked.security.master_key_password = "***";
        if (masked.security.alert_webhook_secret) masked.security.alert_webhook_secret = "***";
      }
      document.getElementById("systemConfig").textContent = JSON.stringify(masked, null, 2);
    }

    if (stats) {
      setMetric("m-audit", (stats.total_events || 0).toLocaleString());
    }
    // 资源池
    const poolEl = document.getElementById("sysPool");
    if (pool) {
      const tenants = pool.tenants || [];
      poolEl.innerHTML = '<div class="kv-row"><span class="kv-key">已池化租户</span><span class="kv-val">' +
        (pool.pooled_tenants || 0) + "</span></div>" +
        (tenants.length
          ? tenants.map((t) => '<div class="kv-row"><span class="kv-key">' +
              escapeHtml(t.tenant_id || t.id || "—") + '</span><span class="kv-val">' +
              escapeHtml(t.status || "active") + "</span></div>").join("")
          : '<p class="hint">无活跃租户资源池</p>');
    } else {
      poolEl.innerHTML = '<p class="hint">资源池信息不可用</p>';
    }
  }

  document.getElementById("refreshSystemBtn").addEventListener("click", loadSystem);

  // ════════════════════ 配置管理 ════════════════════

  const configEditor = document.getElementById("configEditor");
  const configSaveBtn = document.getElementById("configSaveBtn");
  const configStatus = document.getElementById("configStatus");
  const cfgCardsWrap = document.getElementById("cfgCardsWrap");

  // 配置编辑器未保存提示
  let hasUnsavedChanges = false;
  if (configEditor) {
    configEditor.addEventListener("input", function () { hasUnsavedChanges = true; });
  }
  window.addEventListener("beforeunload", function (e) {
    if (hasUnsavedChanges) {
      e.preventDefault();
      e.returnValue = "有未保存的更改，确定离开吗？";
      return e.returnValue;
    }
  });

  // 配置中需脱敏/标记为敏感的字段名（与 maskSecrets/stripMasked 保持一致）
  const CFG_SECRET_KEYS = new Set([
    "master_key_password", "alert_webhook_secret",
    "webhook_default_secret", "s3_secret_key", "webdav_password",
    "smart_client_secret", "fhir_auth_token", "secret",
  ]);

  // 顶级字段 → 中文分组标题
  const CFG_GROUP_TITLES = {
    app_name: "常规", env: "运行环境", debug: "调试",
    discovery_enabled: "设备发现", model: "LLM 模型",
    security: "安全", paths: "路径", resources: "资源",
    integrations: "集成", hooks: "钩子",
    auto_key_rotation: "密钥轮换", compliance: "合规", clinical: "临床",
  };

  const CFG_METRIC_IDS = ["cfg-m-app", "cfg-m-env", "cfg-m-model", "cfg-m-enc", "cfg-m-tenants", "cfg-m-audit"];

  function cfgMetric(id, v) {
    const el = document.getElementById(id);
    if (el) el.textContent = v == null ? "—" : String(v);
  }

  async function renderConfigMetrics(config) {
    cfgMetric("cfg-m-app", config ? config.app_name : "—");
    cfgMetric("cfg-m-env", config ? config.env : "—");
    const m = (config && config.model) || {};
    cfgMetric("cfg-m-model", m.model_name || "—");
    const s = (config && config.security) || {};
    cfgMetric("cfg-m-enc", s.encryption || "—");
    // 租户数 / 审计事件总数来自独立端点，失败优雅降级为 —
    let tenants = "—", auditTotal = "—";
    try {
      const pool = await api("/system/pipeline-pool");
      const list = (pool && pool.tenants) || [];
      tenants = (pool && pool.pooled_tenants != null) ? pool.pooled_tenants : list.length;
    } catch (e) { /* 忽略 */ }
    try {
      const stats = await api("/audit/statistics");
      if (stats) {
        const t = stats.total_events != null ? stats.total_events : stats.total;
        if (t != null) auditTotal = Number(t).toLocaleString();
      }
    } catch (e) { /* 忽略 */ }
    cfgMetric("cfg-m-tenants", tenants);
    cfgMetric("cfg-m-audit", auditTotal);
  }

  // 把任意配置值渲染为可读字符串
  function formatConfigValue(v) {
    if (v == null) return "—";
    if (typeof v === "boolean") return v ? "启用" : "关闭";
    if (typeof v === "number") return String(v);
    if (typeof v === "string") return v === "" ? "（空）" : v;
    if (Array.isArray(v)) {
      if (!v.length) return "0 项";
      if (v.every((x) => typeof x !== "object" || x === null)) return v.join(", ");
      return v.length + " 项";
    }
    if (typeof v === "object") return "{…}";
    return String(v);
  }

  function renderConfigCards(config) {
    if (!config || typeof config !== "object") {
      cfgCardsWrap.innerHTML = '<p class="hint">暂无可展示的配置。</p>';
      return;
    }
    const groups = [];
    const scalars = [];
    Object.keys(config).forEach((k) => {
      const v = config[k];
      if (v && typeof v === "object" && !Array.isArray(v)) groups.push([k, v]);
      else scalars.push([k, v]);
    });
    let html = "";
    if (scalars.length) html += cfgCardHtml("常规", scalars);
    groups.forEach(([k, v]) => {
      html += cfgCardHtml(CFG_GROUP_TITLES[k] || k, Object.entries(v), k);
    });
    cfgCardsWrap.innerHTML = '<div class="config-cards">' + html + "</div>";
  }

  function cfgCardHtml(title, entries, groupKey) {
    const items = entries.map(([k, v]) => {
      const isSecret = CFG_SECRET_KEYS.has(k);
      const val = isSecret ? "***" : formatConfigValue(v);
      const valHtml = isSecret
        ? '<span class="cfg-secret"><span class="cfg-lock" title="敏感字段已脱敏">🔒</span> ' + escapeHtml(val) + "</span>"
        : escapeHtml(val);
      return '<div class="kv-row"><span class="kv-key">' + escapeHtml(k) +
        '</span><span class="kv-val">' + valHtml + "</span></div>";
    }).join("");
    return '<div class="config-card cfg-card" data-cfg-group="' + escapeHtml(groupKey || title) + '">' +
      '<div class="cfg-card-head">' +
        '<span class="cfg-card-glyph">⚙</span>' +
        "<h4>" + escapeHtml(title) + "</h4>" +
        '<span class="cfg-card-badge">' + entries.length + " 项</span>" +
        '<span class="cfg-card-chevron" aria-hidden="true">▾</span>' +
      "</div>" +
      '<div class="cfg-card-body">' + (items || '<p class="hint">无字段</p>') + "</div>" +
    "</div>";
  }

  // 保存反馈横幅（type: "ok" | "fail" | null）
  function setCfgBanner(type, message) {
    if (!message) { configStatus.innerHTML = ""; return; }
    const ok = type === "ok";
    configStatus.innerHTML = '<div class="cfg-banner ' + (ok ? "ok" : "fail") + '">' +
      '<span class="cfg-banner-icon">' + (ok ? "✓" : "⚠") + "</span>" +
      '<div class="cfg-banner-body"><div class="cfg-banner-title">' +
      (ok ? "操作成功" : "操作失败") + '</div><div class="cfg-banner-sub">' +
      escapeHtml(message) + "</div></div></div>";
  }

  async function loadConfigEditor() {
    // 骨架屏占位
    cfgCardsWrap.innerHTML =
      '<div class="skeleton-list">' +
        '<div class="skeleton-item"><div class="skeleton-line w60"></div><div class="skeleton-line w40"></div></div>' +
        '<div class="skeleton-item"><div class="skeleton-line w50"></div><div class="skeleton-line w80"></div></div>' +
        '<div class="skeleton-item"><div class="skeleton-line w40"></div><div class="skeleton-line w60"></div></div>' +
      "</div>";
    configEditor.value = "";
    configSaveBtn.disabled = true;
    CFG_METRIC_IDS.forEach((id) => cfgMetric(id, "—"));
    setCfgBanner(null, "");
    try {
      const c = await api("/config");
      // 脱敏：把敏感字段值替换为占位符，编辑时若保留占位符则后端原值不变
      maskSecrets(c);
      configEditor.value = JSON.stringify(c, null, 2);
      configSaveBtn.disabled = false;
      renderConfigCards(c);
      renderConfigMetrics(c);
      setCfgBanner("ok", "已加载当前配置（敏感字段已脱敏，保留 *** 提交则不修改原值）");
    } catch (e) {
      configEditor.value = "";
      cfgCardsWrap.innerHTML = '<p class="hint" style="color:var(--danger)">加载失败：' + escapeHtml(e.message) + "</p>";
      setCfgBanner("fail", "加载失败：" + e.message);
    }
  }

  function maskSecrets(c) {
    const sec = c.security || {};
    if (sec.master_key_password) sec.master_key_password = "***";
    if (sec.alert_webhook_secret) sec.alert_webhook_secret = "***";
    const integ = c.integrations || {};
    if (integ.webhook_default_secret) integ.webhook_default_secret = "***";
    if (integ.s3_secret_key) integ.s3_secret_key = "***";
    if (integ.webdav_password) integ.webdav_password = "***";
    (integ.webhook_endpoints || []).forEach((e) => { if (e.secret) e.secret = "***"; });
    const clin = c.clinical || {};
    if (clin.smart_client_secret) clin.smart_client_secret = "***";
    if (clin.fhir_auth_token) clin.fhir_auth_token = "***";
  }

  document.getElementById("configLoadBtn").addEventListener("click", loadConfigEditor);

  document.getElementById("configFormatBtn").addEventListener("click", () => {
    if (!configEditor.value.trim()) { setCfgBanner("fail", "配置为空，无法格式化"); return; }
    try {
      const obj = JSON.parse(configEditor.value);
      configEditor.value = JSON.stringify(obj, null, 2);
      setCfgBanner("ok", "JSON 已格式化（校验通过）");
    } catch (e) {
      setCfgBanner("fail", "JSON 解析失败：" + e.message);
      toast("JSON 解析失败", "error");
    }
  });

  configSaveBtn.addEventListener("click", async () => {
    if (!configEditor.value.trim()) {
      setCfgBanner("fail", "配置为空，无法保存");
      toast("配置为空", "error");
      return;
    }
    let body;
    try { body = JSON.parse(configEditor.value); }
    catch (e) {
      setCfgBanner("fail", "JSON 无效：" + e.message);
      toast("配置 JSON 无效", "error");
      return;
    }
    // 危险操作二次确认
    const ok = await confirmDialog({
      title: "保存配置",
      message: "将把当前配置持久化到 settings.json，可能影响应用运行行为。确认继续？",
      okText: "保存",
      danger: true,
      icon: "⚠",
    });
    if (!ok) return;
    // 移除值为 *** 的脱敏占位符，让后端保留原值（不覆盖）
    stripMasked(body);
    setCfgBanner(null, "");
    try {
      const r = await api("/config", { method: "PUT", body: body });
      maskSecrets(r);
      configEditor.value = JSON.stringify(r, null, 2);
      renderConfigCards(r);
      renderConfigMetrics(r);
      setCfgBanner("ok", "配置已保存并持久化到 settings.json");
      toast("配置已保存", "success");
      hasUnsavedChanges = false;
    } catch (e) {
      setCfgBanner("fail", "保存失败：" + e.message);
      toast("保存失败：" + e.message, "error");
    }
  });

  function stripMasked(obj) {
    if (!obj || typeof obj !== "object") return;
    for (const k of Object.keys(obj)) {
      if (obj[k] === "***") delete obj[k];
      else if (typeof obj[k] === "object") stripMasked(obj[k]);
    }
  }

  // 可视化 / 原始 JSON 标签切换
  document.querySelectorAll(".cfg-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.getAttribute("data-cfg-tab");
      document.querySelectorAll(".cfg-tab").forEach((t) => t.classList.toggle("active", t === tab));
      document.querySelectorAll("[data-cfg-pane]").forEach((p) => {
        p.classList.toggle("hidden", p.getAttribute("data-cfg-pane") !== target);
      });
    });
  });

  // 配置卡片折叠/展开 + 导航（事件委托）
  cfgCardsWrap.addEventListener("click", (e) => {
    const head = e.target.closest(".cfg-card-head");
    if (!head) return;
    const card = head.parentElement;
    if (card && card.classList.contains("cfg-card")) card.classList.toggle("collapsed");
  });

  // 配置卡片双击跳转到设置中心对应子页
  cfgCardsWrap.addEventListener("dblclick", (e) => {
    const card = e.target.closest(".cfg-card");
    if (!card) return;
    const group = card.dataset.cfgGroup || "";
    // 映射卡片组名 → 设置中心子标签 data-stab
    const groupToStab = {
      "模型": "advanced",
      "安全": "advanced",
      "路径": "advanced",
      "监控": "advanced",
      "常规": "advanced",
    };
    const stab = groupToStab[group] || "advanced";
    // 切换到设置中心
    switchTab("settings", true);
    // 激活对应子标签
    setTimeout(() => {
      const tab = document.querySelector('.settings-tab[data-stab="' + stab + '"]');
      if (tab) tab.click();
    }, 100);
  });

  // ════════════════════ 连接管理 ════════════════════

  // ── 连接管理：统计指标卡 ──
  function connMetricCards(conns) {
    const total = conns.length;
    const enabled = conns.filter((c) => c.is_enabled).length;
    const local = conns.filter((c) => c.is_local).length;
    const platforms = new Set(conns.map((c) => c.platform_type).filter(Boolean)).size;
    return [
      { label: "连接总数", value: total, icon: "🔌" },
      { label: "已启用", value: enabled, icon: "✅" },
      { label: "本地连接", value: local, icon: "💻" },
      { label: "连接类型数", value: platforms, icon: "🗂" },
    ].map((m) =>
      '<div class="metric-card"><div class="metric-label">' + m.icon + " " + escapeHtml(m.label) +
      '</div><div class="metric-value">' + m.value + "</div></div>"
    ).join("");
  }

  // ── 连接管理：单张卡片 HTML ──
  function connCardHtml(c, idx) {
    const enabledPill = c.is_enabled
      ? '<span class="status-pill ok">启用</span>'
      : '<span class="status-pill fail">禁用</span>';
    const localTag = c.is_local ? '<span class="tag">本地</span>' : "";
    const platformBadge = '<span class="conn-card-platform">' +
      escapeHtml(c.platform_type || "—") + "</span>";
    const testResult = '<div class="conn-test-result" id="conn-test-' +
      escapeHtml(String(c.id)) + '"></div>';
    return '<div class="conn-card" data-id="' + escapeHtml(String(c.id)) +
      '" style="animation-delay:' + (idx * 40) + 'ms">' +
      '<div class="conn-card-head">' +
        '<div class="conn-card-title">' +
          '<span class="conn-card-name">' + escapeHtml(c.name || "未命名") + "</span>" +
          platformBadge +
        "</div>" +
        '<div class="conn-card-actions">' +
          '<button class="btn btn-sm" onclick="testConn(\'' + c.id + "')\">测试</button>" +
          '<button class="btn btn-danger btn-sm" onclick="deleteConn(\'' + c.id + "')\">删除</button>" +
        "</div>" +
      "</div>" +
      '<div class="conn-card-meta">' + enabledPill + localTag +
        '<span class="conn-card-priority">优先级 ' + (c.priority || 0) + "</span>" +
      "</div>" +
      '<div class="conn-card-row"><span class="conn-card-label">模型</span>' +
        '<span class="conn-card-value">' + escapeHtml(c.model_name || "—") + "</span></div>" +
      '<div class="conn-card-row"><span class="conn-card-label">Base URL</span>' +
        '<span class="conn-card-value mono">' + escapeHtml(c.base_url || "—") + "</span></div>" +
      testResult +
    "</div>";
  }

  async function loadConnections() {
    const out = document.getElementById("connList");
    const metrics = document.getElementById("connMetrics");
    metrics.innerHTML = "";
    out.innerHTML = '<div class="skeleton-list">' +
      Array.from({length: 3}).map(() =>
        '<div class="skeleton-item"><div class="skeleton-line w40"></div>' +
        '<div class="skeleton-line w80"></div><div class="skeleton-line w60"></div></div>'
      ).join("") + "</div>";
    try {
      const conns = await api("/connections");
      if (!Array.isArray(conns) || !conns.length) {
        out.innerHTML = emptyState("🔌", "暂无连接", "在下方新增一个 LLM 提供方连接");
        return;
      }
      metrics.innerHTML = connMetricCards(conns);
      out.innerHTML = conns.map((c, i) => connCardHtml(c, i)).join("");
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' +
        escapeHtml(e.message) + "</p>";
    }
  }

  window.testConn = async function (id) {
    const box = document.getElementById("conn-test-" + id);
    if (box) {
      box.className = "conn-test-result loading-state";
      box.innerHTML = '<span class="conn-spin"></span>' +
        '<span class="conn-test-msg">测试中…</span>';
    }
    toast("测试中…");
    try {
      const r = await api("/connections/" + id + "/test", { method: "POST" });
      const ok = !!r.success;
      if (box) {
        box.className = "conn-test-result " + (ok ? "ok" : "fail");
        box.innerHTML = '<span class="status-pill ' + (ok ? "ok" : "fail") + '">' +
          (ok ? "正常" : "异常") + "</span>" +
          '<span class="conn-test-msg">' +
          escapeHtml(r.message || (ok ? "连接正常" : "连接失败")) + "</span>";
      }
      toast(ok ? "连接正常：" + (r.message || "") : "连接失败：" + (r.message || ""),
        ok ? "success" : "error");
    } catch (e) {
      if (box) {
        box.className = "conn-test-result fail";
        box.innerHTML = '<span class="status-pill fail">异常</span>' +
          '<span class="conn-test-msg">' + escapeHtml(e.message) + "</span>";
      }
      toast("测试失败：" + e.message, "error");
    }
  };

  window.deleteConn = async function (id) {
    let name = id;
    const card = document.querySelector('.conn-card[data-id="' + id + '"]');
    if (card) {
      const nm = card.querySelector(".conn-card-name");
      if (nm) name = nm.textContent;
    }
    const ok = await confirmDialog({
      title: "删除连接",
      message: "确认删除连接 " + name + "？此操作不可撤销。",
      okText: "删除",
      danger: true,
      icon: "🗑",
    });
    if (!ok) return;
    try {
      await api("/connections/" + id, { method: "DELETE" });
      toast("已删除", "success");
      loadConnections();
    } catch (e) { toast("删除失败：" + e.message, "error"); }
  };

  document.getElementById("connLoadBtn").addEventListener("click", loadConnections);
  document.getElementById("connCreateBtn").addEventListener("click", async () => {
    const body = {
      name: document.getElementById("conn-name").value.trim(),
      platform_type: document.getElementById("conn-platform-type").value.trim(),
      base_url: document.getElementById("conn-base-url").value.trim(),
      model_name: document.getElementById("conn-model-name").value.trim(),
      auth_method: document.getElementById("conn-auth-method").value.trim() || "none",
      api_key: document.getElementById("conn-api-key").value.trim(),
      timeout: Number(document.getElementById("conn-timeout").value) || 120,
    };
    if (!body.name || !body.platform_type || !body.base_url) {
      toast("名称、连接类型、Base URL 必填", "error");
      return;
    }
    try {
      await api("/connections", { method: "POST", body: body });
      toast("连接已创建", "success");
      document.getElementById("conn-name").value = "";
      document.getElementById("conn-base-url").value = "";
      document.getElementById("conn-model-name").value = "";
      document.getElementById("conn-api-key").value = "";
      document.getElementById("conn-platform-type").value = "ollama";
      document.getElementById("conn-auth-method").value = "none";
      document.getElementById("conn-timeout").value = "120";
      loadConnections();
    } catch (e) { toast("创建失败：" + e.message, "error"); }
  });

  // ════════════════════ 租户管理 ════════════════════

  // ── 租户管理深化 ──
  // 把 ISO 时间转为"刚刚 / N 分钟前 / N 小时前 / N 天前 / N 个月前 / N 年前"
  function relativeTime(dateStr) {
    if (!dateStr) return "—";
    const d = new Date(dateStr);
    if (isNaN(d.getTime())) return escapeHtml(String(dateStr));
    const diff = Math.max(0, Date.now() - d.getTime());
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return "刚刚";
    const min = Math.floor(sec / 60);
    if (min < 60) return min + " 分钟前";
    const hr = Math.floor(min / 60);
    if (hr < 24) return hr + " 小时前";
    const day = Math.floor(hr / 24);
    if (day < 30) return day + " 天前";
    const month = Math.floor(day / 30);
    if (month < 12) return month + " 个月前";
    return Math.floor(month / 12) + " 年前";
  }

  let tenantCache = []; // 缓存已加载租户，供搜索框实时过滤

  function renderTenantMetrics(tenants) {
    const total = tenants.length;
    const providers = new Set(
      tenants.map((t) => t.key_provider_type).filter(Boolean)
    );
    const now = new Date();
    const startOfToday = new Date(
      now.getFullYear(), now.getMonth(), now.getDate()
    ).getTime();
    const sevenDaysAgo = now.getTime() - 7 * 24 * 3600 * 1000;
    const todayNew = tenants.filter((t) => {
      const d = new Date(t.created_at);
      return !isNaN(d.getTime()) && d.getTime() >= startOfToday;
    }).length;
    const active = tenants.filter((t) => {
      const d = new Date(t.created_at);
      return !isNaN(d.getTime()) && d.getTime() >= sevenDaysAgo;
    }).length;
    return (
      '<div class="metric-grid">' +
        '<div class="metric-card"><div class="metric-label">租户总数</div>' +
        '<div class="metric-value">' + total + "</div></div>" +
        '<div class="metric-card"><div class="metric-label">密钥提供方类型</div>' +
        '<div class="metric-value">' + providers.size + "</div></div>" +
        '<div class="metric-card"><div class="metric-label">今日新增</div>' +
        '<div class="metric-value">' + todayNew + "</div></div>" +
        '<div class="metric-card" title="近 7 天创建的租户">' +
        '<div class="metric-label">活跃租户</div>' +
        '<div class="metric-value">' + active + "</div></div>" +
      "</div>"
    );
  }

  function renderTenantGrid(tenants) {
    if (!tenants.length) {
      return emptyState("🗂️", "还没有租户", "在下方创建你的第一个租户");
    }
    let html = '<div class="tenant-grid">';
    tenants.forEach((t, i) => {
      const tid = escapeHtml(t.tenant_id || "");
      const name = escapeHtml(t.name || "");
      const provider = escapeHtml(t.key_provider_type || "");
      html +=
        '<div class="tenant-card" style="animation-delay:' + (i * 40) + 'ms">' +
          '<div class="tenant-id-row">' +
            '<code class="tenant-id-mono">' + tid + "</code>" +
            '<button class="tenant-copy-btn" data-copy="' + tid + '" title="复制租户 ID" type="button">⧉</button>' +
          "</div>" +
          '<div class="tenant-name">' + (name || "未命名") + "</div>" +
          '<div class="tenant-meta">' +
            '<span class="tenant-badge tag">' + (provider || "—") + "</span>" +
            '<span class="tenant-time">' + relativeTime(t.created_at) + "</span>" +
          "</div>" +
        "</div>";
    });
    html += "</div>";
    return html;
  }

  // 依据搜索框关键词过滤并重渲染（指标卡始终反映全量缓存）
  function applyTenantFilter() {
    const metricsEl = document.getElementById("tenantMetrics");
    const listEl = document.getElementById("tenantList");
    if (metricsEl) metricsEl.innerHTML = renderTenantMetrics(tenantCache);
    const q = (
      (document.getElementById("tenantSearch") || {}).value || ""
    ).trim().toLowerCase();
    const filtered = !q
      ? tenantCache
      : tenantCache.filter(
          (t) =>
            (t.tenant_id || "").toLowerCase().includes(q) ||
            (t.name || "").toLowerCase().includes(q)
        );
    listEl.innerHTML = renderTenantGrid(filtered);
    listEl.querySelectorAll(".tenant-copy-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const val = btn.getAttribute("data-copy") || "";
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(val).then(
            () => toast("已复制：" + val, "success"),
            () => toast("复制失败", "error")
          );
        } else {
          toast("已复制：" + val, "success");
        }
      });
    });
  }

  async function loadTenants() {
    const out = document.getElementById("tenantList");
    const metricsEl = document.getElementById("tenantMetrics");
    if (metricsEl) metricsEl.innerHTML = "";
    out.innerHTML =
      '<div class="skeleton-list">' +
      '<div class="skeleton-item"><div class="skeleton-line w60"></div><div class="skeleton-line w40"></div></div>'.repeat(
        4
      ) +
      "</div>";
    try {
      const tenants = await api("/tenants");
      tenantCache = Array.isArray(tenants) ? tenants : [];
      applyTenantFilter();
    } catch (e) {
      out.innerHTML =
        '<p class="placeholder" style="color:var(--danger)">' +
        escapeHtml(e.message) +
        "</p>";
    }
  }

  document.getElementById("tenantLoadBtn").addEventListener("click", loadTenants);
  const tenantSearchEl = document.getElementById("tenantSearch");
  if (tenantSearchEl) {
    tenantSearchEl.addEventListener("input", debounce(applyTenantFilter, 200));
  }
  document.getElementById("tenantCreateBtn").addEventListener("click", async () => {
    const body = {
      tenant_id: document.getElementById("tenant-id").value.trim(),
      name: document.getElementById("tenant-name").value.trim(),
      key_provider_type:
        document.getElementById("tenant-provider").value || "filepassword",
    };
    const pw = document.getElementById("tenant-password").value;
    if (pw) body.password = pw;
    if (!body.tenant_id || !body.name) {
      toast("租户 ID 和名称必填", "error");
      return;
    }
    try {
      await api("/tenants", { method: "POST", body: body });
      toast("租户已创建", "success");
      document.getElementById("tenant-id").value = "";
      document.getElementById("tenant-name").value = "";
      document.getElementById("tenant-password").value = "";
      await loadTenants();
      // 入场高亮：fadeSlideIn 之外再给新卡片一个高亮脉冲
      document.querySelectorAll(".tenant-card").forEach((c) => {
        const code = c.querySelector(".tenant-id-mono");
        if (code && code.textContent === body.tenant_id) {
          c.classList.add("tenant-card-new");
          c.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
    } catch (e) {
      toast("创建失败：" + e.message, "error");
    }
  });

  // ════════════════════ 集成运维 ════════════════════

  function setPill(id, text, kind) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = "status-pill" + (kind ? " " + kind : "");
  }
  function emptyState(icon, text, hint) {
    return '<div class="chat-empty"><div class="chat-empty-icon">' + icon + "</div><p>" +
      escapeHtml(text) + "</p>" + (hint ? '<p class="hint">' + escapeHtml(hint) + "</p>" : "") + "</div>";
  }

  // 同步
  async function syncStatus() {
    const out = document.getElementById("syncResult");
    setPill("syncPill", "查询中…", "info");
    out.innerHTML = '<div class="skeleton-item"><div class="skeleton-line w60"></div><div class="skeleton-line w40"></div></div>';
    try {
      const r = await api("/sync/status");
      const active = r.available && !r.running;
      const running = r.running;
      setPill("syncPill",
        running ? "同步中" : (r.available ? "就绪" : "不可用"),
        running ? "warn" : (r.available ? "ok" : "fail"));
      const lastTimes = r.last_sync_times || {};
      const lastKeys = Object.keys(lastTimes);
      let html = '<div class="kv-grid">';
      html += kvCell("设备 ID", r.device_id || "—");
      html += kvCell("引擎状态", r.available ? "可用" : "不可用");
      html += kvCell("当前任务", running ? "运行中" : "空闲");
      html += kvCell("发现对等端", r.peers_discovered || 0);
      html += "</div>";
      html += '<div class="section-label">最近同步时间</div>';
      if (lastKeys.length) {
        html += '<div class="timeline">';
        lastKeys.forEach((k) => {
          html += '<div class="timeline-item"><span class="timeline-dot"></span>' +
            '<div><div class="timeline-title">' + escapeHtml(k) + "</div>" +
            '<div class="timeline-time">' + escapeHtml(lastTimes[k] || "—") + "</div></div></div>";
        });
        html += "</div>";
      } else {
        html += emptyState("🕐", "暂无同步记录", "触发一次同步后查看");
      }
      if (r.message) html += '<p class="hint">' + escapeHtml(r.message) + "</p>";
      out.innerHTML = html;
    } catch (e) {
      setPill("syncPill", "查询失败", "fail");
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' + escapeHtml(e.message) + "</p>";
    }
  }
  function kvCell(k, v) {
    return '<div class="kv-cell"><div class="kv-label">' + escapeHtml(k) +
      '</div><div class="kv-value">' + escapeHtml(v == null ? "—" : String(v)) + "</div></div>";
  }
  document.getElementById("syncStatusBtn").addEventListener("click", syncStatus);
  document.getElementById("syncTriggerBtn").addEventListener("click", async () => {
    const out = document.getElementById("syncResult");
    setPill("syncPill", "触发中…", "warn");
    out.innerHTML = '<p class="placeholder">正在触发同步…</p>';
    try {
      const r = await api("/sync/trigger", { method: "POST" });
      toast("同步已触发", "success");
      syncStatus();
    } catch (e) {
      setPill("syncPill", "触发失败", "fail");
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' + escapeHtml(e.message) + "</p>";
      toast("触发失败：" + e.message, "error");
    }
  });

  // Webhook
  async function whEndpoints() {
    const out = document.getElementById("whResult");
    out.innerHTML = '<div class="skeleton-item"><div class="skeleton-line w50"></div><div class="skeleton-line w80"></div></div>';
    try {
      const r = await api("/webhooks/endpoints");
      setPill("whPill", r.enabled ? "已启用" : "未启用", r.enabled ? "ok" : "info");
      const eps = r.endpoints || [];
      let html = '<div class="kv-grid">';
      html += kvCell("状态", r.enabled ? "启用" : "关闭");
      html += kvCell("端点数", eps.length);
      html += "</div>";
      if (eps.length) {
        html += '<div class="section-label">端点列表</div>';
        html += '<table class="data-table"><thead><tr><th>URL</th><th>事件</th><th>状态</th></tr></thead><tbody>';
        eps.forEach((ep) => {
          const events = Array.isArray(ep.events) ? ep.events.join(", ") : (ep.events || "—");
          html += "<tr><td class='mono'>" + escapeHtml(ep.url || ep.endpoint || "—") + "</td>" +
            "<td>" + escapeHtml(events) + "</td>" +
            '<td><span class="badge ' + (ep.active === false ? "" : "info") + '">' +
            (ep.active === false ? "停用" : "启用") + "</span></td></tr>";
        });
        html += "</tbody></table>";
      } else {
        html += emptyState("📡", "暂无 Webhook 端点", "在配置中添加出站端点");
      }
      out.innerHTML = html;
    } catch (e) {
      setPill("whPill", "查询失败", "fail");
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' + escapeHtml(e.message) + "</p>";
    }
  }
  document.getElementById("whEndpointsBtn").addEventListener("click", whEndpoints);
  document.getElementById("whDeliveriesBtn").addEventListener("click", async () => {
    const out = document.getElementById("whResult");
    out.innerHTML = '<div class="skeleton-item"><div class="skeleton-line w60"></div></div>';
    try {
      const r = await api("/webhooks/deliveries");
      const list = Array.isArray(r) ? r : (r.deliveries || []);
      if (!list.length) {
        out.innerHTML = emptyState("📭", "暂无投递记录", "发送测试事件后查看");
        return;
      }
      let html = '<div class="section-label">投递记录（' + list.length + " 条）</div>";
      html += '<div class="timeline">';
      list.slice(0, 30).forEach((d) => {
        const ok = d.success !== false && d.status_code && d.status_code < 400;
        html += '<div class="timeline-item">' +
          '<span class="timeline-dot ' + (ok ? "ok" : "fail") + '"></span>' +
          '<div><div class="timeline-title">' + escapeHtml(d.event || d.event_type || "事件") +
          (d.status_code ? ' · <span class="badge ' + (ok ? "info" : "critical") + '">' + d.status_code + "</span>" : "") +
          "</div><div class='timeline-time mono'>" + escapeHtml(d.timestamp || d.ts || "—") + "</div>" +
          (d.url ? '<div class="hint mono">' + escapeHtml(d.url) + "</div>" : "") + "</div></div>";
      });
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' + escapeHtml(e.message) + "</p>";
    }
  });
  document.getElementById("whTestBtn").addEventListener("click", async () => {
    const out = document.getElementById("whResult");
    out.innerHTML = '<p class="placeholder">发送测试事件中…</p>';
    try {
      const r = await api("/webhooks/test", { method: "POST" });
      toast("测试事件已发送", "success");
      let html = '<div class="kv-grid">';
      Object.keys(r || {}).slice(0, 6).forEach((k) => {
        html += kvCell(k, typeof r[k] === "object" ? JSON.stringify(r[k]) : r[k]);
      });
      html += "</div>";
      out.innerHTML = html || '<p class="placeholder">已发送（无返回详情）</p>';
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' + escapeHtml(e.message) + "</p>";
      toast("发送失败：" + e.message, "error");
    }
  });

  // 备份
  document.getElementById("backupBtn").addEventListener("click", async () => {
    const out = document.getElementById("backupResult");
    setPill("backupPill", "备份中…", "warn");
    out.innerHTML = '<div class="skeleton-item"><div class="skeleton-line w60"></div></div>';
    try {
      const r = await api("/backup/remote", { method: "POST" });
      const ok = r.ok !== false;
      setPill("backupPill", ok ? "备份成功" : "备份失败", ok ? "ok" : "fail");
      toast(ok ? "备份完成（上传 " + (r.uploaded || 0) + "）" : "备份失败", ok ? "success" : "error");
      let html = '<div class="kv-grid">';
      html += kvCell("状态", ok ? "成功" : "失败");
      html += kvCell("已上传", r.uploaded || 0);
      if (r.duration_ms != null) html += kvCell("耗时(ms)", r.duration_ms);
      if (r.backend) html += kvCell("后端", r.backend);
      if (r.message) html += kvCell("消息", r.message);
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      setPill("backupPill", "备份失败", "fail");
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' + escapeHtml(e.message) + "</p>";
      toast("备份失败：" + e.message, "error");
    }
  });

  // Inbox 投递（明文，走 /inbox/ingest）
  document.getElementById("inboxSubmitBtn").addEventListener("click", async () => {
    const out = document.getElementById("inboxResult");
    const content = document.getElementById("inboxContent").value;
    if (!content.trim()) { toast("请输入内容", "error"); return; }
    out.innerHTML = '<div class="skeleton-item"><div class="skeleton-line w60"></div><div class="skeleton-line w40"></div></div>';
    try {
      const r = await api("/inbox/ingest", {
        method: "POST",
        body: {
          content: content,
          filename: document.getElementById("inboxFilename").value.trim(),
        },
      });
      const ok = r.ok !== false;
      toast(ok ? "已投递并分类完成" : "已投递，分类状态：" + (r.state || ""),
        ok ? "success" : "error");
      let html = '<div class="kv-grid">';
      html += kvCell("状态", ok ? "成功" : (r.state || "已投递"));
      if (r.file_id) html += kvCell("文件 ID", r.file_id);
      if (r.category) html += kvCell("分类", r.category);
      if (r.state) html += kvCell("流水线状态", r.state);
      if (r.message) html += kvCell("消息", r.message);
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">' + escapeHtml(e.message) + "</p>";
      toast("投递失败：" + e.message, "error");
    }
  });

  // ════════════════════ PHI 脱敏 ════════════════════

  const DEID_EXAMPLE =
    "患者张三，男，62 岁，联系电话 13800138000，身份证号 110101199003071234，" +
    "邮箱 zhangsan@example.com，住址北京市海淀区中关村大街 1 号。" +
    "主诉胸痛 3 天，既往糖尿病史。主治医生李四，分机 8866。";

  document.getElementById("deid-example-btn").addEventListener("click", () => {
    document.getElementById("deid-input").value = DEID_EXAMPLE;
  });

  const deidBtn = document.getElementById("deid-run-btn");

  // PHI 实体类型 → 配色 token（与 style.css 中 .deid-t-* 类对应）
  const DEID_TYPES = [
    "patient-name", "mrn", "dob", "phone", "email",
    "ssn", "address", "medical-record", "date", "ip-address",
  ];
  function deidTypeClass(t) {
    const n = String(t || "").toLowerCase().replace(/_/g, "-");
    return DEID_TYPES.indexOf(n) >= 0 ? "deid-t-" + n : "deid-t-other";
  }

  // 脱敏处理中的骨架屏（替代 "脱敏中…" 文案）
  function deidSkeleton() {
    return '<div class="skeleton-list">' +
      '<div class="skeleton-item"><div class="skeleton-line w40"></div>' +
      '<div class="skeleton-line w80"></div><div class="skeleton-line w60"></div></div>' +
      '<div class="skeleton-item"><div class="skeleton-line w50"></div>' +
      '<div class="skeleton-line w90"></div></div>' +
      '<div class="skeleton-item"><div class="skeleton-line w30"></div>' +
      '<div class="skeleton-line w70"></div></div></div>';
  }

  async function loadDeidentify() {
    const text = document.getElementById("deid-input").value;
    const strategy = document.getElementById("deid-strategy").value || "redact";
    const out = document.getElementById("deid-result");
    if (!text.trim()) { toast("请输入待脱敏文本", "error"); return; }
    out.innerHTML = deidSkeleton();
    deidBtn.disabled = true;
    try {
      const r = await api("/api/v1/deidentify", {
        method: "POST",
        body: { text: text, strategy: strategy },
      });
      renderDeidResult(r, strategy);
      toast("脱敏完成", "success");
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">请求失败：' +
        escapeHtml(e.message) + "</p>";
      toast("脱敏失败：" + e.message, "error");
    } finally {
      deidBtn.disabled = false;
    }
  }
  deidBtn.addEventListener("click", loadDeidentify);

  function renderDeidResult(r, strategy) {
    const out = document.getElementById("deid-result");
    const matches = Array.isArray(r.matches) ? r.matches : [];
    const usedStrategy = r.strategy || strategy;
    const original = r.original || "";
    const deidentified = r.deidentified || "";
    const typesFound = Array.isArray(r.types_found) ? r.types_found :
      Array.from(new Set(matches.map((m) => m.type).filter(Boolean)));

    // 统计指标：实体总数 / 实体类型数 / 脱敏字符数 / 策略
    const entityCount = r.match_count != null ? r.match_count : matches.length;
    const typeCount = typesFound.length;
    const deidChars = deidentified.length;

    // 实体类型分布（用于横向条形图）
    const distrib = {};
    matches.forEach((m) => {
      const t = m.type || "PHI";
      distrib[t] = (distrib[t] || 0) + 1;
    });
    const distribEntries = Object.keys(distrib)
      .map((t) => ({ type: t, count: distrib[t] }))
      .sort((a, b) => b.count - a.count);
    const maxCount = distribEntries.reduce((mx, d) => Math.max(mx, d.count), 0);

    let html = '<div class="deid-result-anim">';

    // ① 统计指标卡（脱敏后才渲染 → 天然"脱敏前隐藏"）
    html += '<div class="metric-grid">' +
      '<div class="metric-card"><div class="metric-label">实体总数</div>' +
      '<div class="metric-value">' + entityCount + "</div></div>" +
      '<div class="metric-card"><div class="metric-label">实体类型数</div>' +
      '<div class="metric-value">' + typeCount + "</div></div>" +
      '<div class="metric-card"><div class="metric-label">脱敏字符数</div>' +
      '<div class="metric-value">' + deidChars + "</div></div>" +
      '<div class="metric-card deid-metric-strategy"><div class="metric-label">策略</div>' +
      '<div class="metric-value">' + escapeHtml(usedStrategy) + "</div></div>" +
      "</div>";

    // ② 原文 / 脱敏对比视图（左右双栏 + 箭头，窄屏堆叠由 CSS 处理）
    html += '<div class="section-label">原文 / 脱敏对比</div>';
    html += '<div class="deid-diff">';
    html += '<div class="deid-diff-col">';
    html += '<div class="deid-diff-head">原文（PHI 高亮）</div>';
    html += '<div class="deid-original deid-highlight">' +
      highlightPhi(original, matches) + "</div>";
    html += "</div>";
    html += '<div class="deid-arrow" aria-hidden="true">→</div>';
    html += '<div class="deid-diff-col">';
    html += '<div class="deid-diff-head">脱敏后（' + escapeHtml(usedStrategy) + "）</div>";
    html += '<div class="deid-redacted deid-text">' + escapeHtml(deidentified) + "</div>";
    html += "</div>";
    html += "</div>";

    // ③ 实体类型分布（纯 CSS 横向条形图）
    html += '<div class="section-label">实体类型分布</div>';
    if (!distribEntries.length) {
      html += '<p class="placeholder">未识别到 PHI 实体，无分布数据。</p>';
    } else {
      html += '<div class="deid-barchart">';
      distribEntries.forEach((d) => {
        const pct = maxCount > 0 ? Math.round((d.count / maxCount) * 100) : 0;
        html += '<div class="deid-bar ' + deidTypeClass(d.type) + '">' +
          '<span class="deid-bar-label">' + escapeHtml(d.type) + "</span>" +
          '<div class="deid-bar-track">' +
          '<div class="deid-bar-fill" style="width:' + pct + '%"></div>' +
          "</div>" +
          '<span class="deid-bar-count">' + d.count + "</span>" +
          "</div>";
      });
      html += "</div>";
    }

    // ④ matches 表格美化（mono 字体 + 实体类型 badge 着色 + hover 高亮）
    html += '<div class="section-label">识别到的 PHI 实体（' + entityCount + " 条）</div>";
    if (!matches.length) {
      html += '<p class="placeholder">未识别到 PHI 实体。</p>';
    } else {
      html += '<table class="data-table deid-table"><thead><tr>' +
        "<th>类型</th><th>原文</th><th>位置</th>" +
        "</tr></thead><tbody>";
      matches.forEach((m) => {
        html += "<tr>" +
          '<td><span class="badge deid-badge ' + deidTypeClass(m.type) + '">' +
          escapeHtml(m.type || "—") + "</span></td>" +
          '<td class="mono">' + escapeHtml(m.value || "") + "</td>" +
          '<td class="mono">' + (m.start ?? "") + " – " + (m.end ?? "") + "</td>" +
          "</tr>";
      });
      html += "</tbody></table>";
    }

    html += "</div>"; // .deid-result-anim
    out.innerHTML = html;
  }

  function highlightPhi(text, matches) {
    if (!matches || !matches.length) return escapeHtml(text);
    const ordered = matches
      .filter((m) => typeof m.start === "number" && typeof m.end === "number")
      .slice().sort((a, b) => a.start - b.start);
    if (!ordered.length) return escapeHtml(text);
    let html = "";
    let cursor = 0;
    ordered.forEach((m) => {
      if (m.start < cursor) return; // 跳过重叠
      html += escapeHtml(text.slice(cursor, m.start));
      html += '<mark class="phi-mark" title="' + escapeHtml(m.type || "PHI") + '">' +
        escapeHtml(text.slice(m.start, m.end)) + "</mark>";
      cursor = m.end;
    });
    html += escapeHtml(text.slice(cursor));
    return html;
  }

  // ════════════════════ 确定性安全规则 ════════════════════

  const SAFETY_EXAMPLE = {
    vitals: { heart_rate: 35, systolic_bp: 88 },
    labs: [{ test: "potassium", value: 6.8, unit: "mmol/L" }],
    medications: ["Warfarin", "Fluconazole"],
    allergies: ["Penicillin"],
  };

  // 取规则最高严重度，用于卡片左侧色条着色
  function safetyRuleTopSev(rule) {
    const levels = rule.severity_levels || (rule.severity ? [rule.severity] : []);
    if (!levels.length) return null;
    let best = null;
    let bestOrder = 99;
    for (const s of levels) {
      const ord = SEV_ORDER[s] ?? 9;
      if (ord < bestOrder) { bestOrder = ord; best = s; }
    }
    return best;
  }

  async function loadSafety() {
    const out = document.getElementById("safety-rules");
    out.innerHTML = '<div class="skeleton-list">' +
      '<div class="skeleton-item"><div class="skeleton-line w40"></div><div class="skeleton-line w80"></div><div class="skeleton-line w60"></div></div>'.repeat(3) +
      "</div>";
    try {
      const r = await api("/api/v1/safety/rules");
      const rules = Array.isArray(r.rules) ? r.rules : [];
      if (!rules.length) {
        out.innerHTML = emptyState("🛡️", "暂无已注册规则",
          "后端规则引擎未注册任何确定性安全规则。");
        return;
      }

      // 统计指标
      const total = r.total ?? rules.length;
      let nCritical = 0, nWarning = 0, nInfo = 0;
      const ruleTypes = new Set();
      rules.forEach((rule) => {
        ruleTypes.add(rule.rule_type);
        (rule.severity_levels || []).forEach((s) => {
          if (s === "critical" || s === "contraindicated") nCritical++;
          else if (s === "warning") nWarning++;
          else if (s === "info") nInfo++;
        });
      });

      let html = '<div class="metric-grid">';
      html += '<div class="metric-card"><div class="metric-label">规则总数</div><div class="metric-value">' + total + "</div></div>";
      html += '<div class="metric-card safety-metric--critical"><div class="metric-label">Critical</div><div class="metric-value">' + nCritical + "</div></div>";
      html += '<div class="metric-card safety-metric--warning"><div class="metric-label">Warning</div><div class="metric-value">' + nWarning + "</div></div>";
      html += '<div class="metric-card safety-metric--info"><div class="metric-label">Info</div><div class="metric-value">' + nInfo + "</div></div>";
      html += '<div class="metric-card"><div class="metric-label">规则类型数</div><div class="metric-value">' + ruleTypes.size + "</div></div>";
      html += "</div>";

      html += '<div class="section-label">规则明细（卡片左侧色条对应最高严重度）</div>';
      html += '<div class="safety-rule-grid">';
      rules.forEach((rule, i) => {
        const topSev = safetyRuleTopSev(rule);
        const sevCls = topSev ? " is-" + topSev : "";
        const sevs = (rule.severity_levels || [])
          .map((s) => sevBadge(s)).join(" ");
        html += '<div class="safety-rule-card' + sevCls + '" style="animation-delay:' + (i * 50) + 'ms">';
        html += '<div class="rule-head">';
        html += '<span class="rule-type">' + escapeHtml(rule.rule_type || "—") + "</span>";
        html += '<span class="rule-sevs">' + (sevs || "") + "</span>";
        html += "</div>";
        html += '<div class="rule-desc">' + escapeHtml(rule.description || "") + "</div>";
        html += "</div>";
      });
      html += "</div>";

      out.innerHTML = html;
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">加载失败：' +
        escapeHtml(e.message) + "</p>";
    }
  }

  document.getElementById("safety-rules-btn").addEventListener("click", loadSafety);

  document.getElementById("safety-example-btn").addEventListener("click", () => {
    document.getElementById("safety-ctx").value =
      JSON.stringify(SAFETY_EXAMPLE, null, 2);
  });

  document.getElementById("safety-test-btn").addEventListener("click", async () => {
    const ctxStr = document.getElementById("safety-ctx").value.trim();
    const out = document.getElementById("safety-findings");
    if (!ctxStr) { toast("请填写 patient_context", "error"); return; }
    let ctx;
    try { ctx = JSON.parse(ctxStr); }
    catch (e) { toast("patient_context JSON 解析失败：" + e.message, "error"); return; }
    out.innerHTML = '<div class="skeleton-list"><div class="skeleton-item"><div class="skeleton-line w40"></div><div class="skeleton-line w80"></div></div><div class="skeleton-item"><div class="skeleton-line w60"></div><div class="skeleton-line w50"></div></div></div>';
    try {
      const r = await api("/api/v1/safety/rules/test", {
        method: "POST",
        body: { patient_context: ctx },
      });
      renderSafetyFindings(r);
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">试跑失败：' +
        escapeHtml(e.message) + "</p>";
      toast("试跑失败：" + e.message, "error");
    }
  });

  function renderSafetyFindings(r) {
    const out = document.getElementById("safety-findings");
    const findings = Array.isArray(r.findings) ? r.findings : [];
    let html = "";

    // blocking 警示横幅（复用 verify-banner.fail / .ok 样式）
    if (typeof r.blocking === "boolean") {
      if (r.blocking) {
        html += '<div class="verify-banner fail safety-blocking">' +
          '<span class="verify-icon">⛔</span>' +
          '<div><div class="verify-title">阻断判定：blocking=true</div>' +
          '<div class="verify-sub">命中 critical 级安全规则，需人工复核后方可继续。规则引擎不可被绕过。</div></div></div>';
      } else {
        html += '<div class="verify-banner ok safety-blocking">' +
          '<span class="verify-icon">✅</span>' +
          '<div><div class="verify-title">阻断判定：blocking=false</div>' +
          '<div class="verify-sub">未触发阻断条件，可继续后续流程。</div></div></div>';
      }
    }

    // severity 分布横向条形图
    const sevCounts = { critical: 0, contraindicated: 0, warning: 0, info: 0 };
    findings.forEach((f) => {
      const s = f.severity || "info";
      if (Object.prototype.hasOwnProperty.call(sevCounts, s)) sevCounts[s]++;
    });
    const maxCount = Math.max(1, ...Object.values(sevCounts));
    const sevOrder = ["critical", "contraindicated", "warning", "info"];
    if (findings.length) {
      html += '<div class="safety-barchart">';
      html += '<div class="safety-barchart-title">命中严重度分布（共 ' + findings.length + " 条）</div>";
      sevOrder.forEach((s) => {
        const c = sevCounts[s];
        const pct = (c / maxCount) * 100;
        html += '<div class="safety-bar">';
        html += '<span class="safety-bar-label">' + (SEV_LABEL[s] || s) + "</span>";
        html += '<div class="safety-bar-track"><div class="safety-bar-fill ' + s + '" style="width:' + pct + '%"></div></div>';
        html += '<span class="safety-bar-count">' + c + "</span>";
        html += "</div>";
      });
      html += "</div>";
    }

    if (!findings.length) {
      html += '<p class="placeholder">未命中任何规则（患者上下文在安全阈值内）。</p>';
      out.innerHTML = html;
      return;
    }

    const sorted = findings.slice().sort((a, b) =>
      (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));
    html += '<div class="section-label">命中规则明细（' +
      (r.total ?? findings.length) + " 条）</div>";
    sorted.forEach((f, i) => {
      html += '<div class="finding ' + sevClass(f.severity) + '" style="animation-delay:' + (i * 60) + 'ms">';
      html += '<div class="finding-head">';
      html += '<span class="finding-rule">' + escapeHtml(f.rule_type || "") + "</span>";
      html += sevBadge(f.severity);
      html += "</div>";
      html += '<div class="finding-text">' + escapeHtml(f.finding || "") + "</div>";
      if (f.recommendation)
        html += '<div class="finding-rec">建议：' + escapeHtml(f.recommendation) + "</div>";
      if (f.affected_resources && f.affected_resources.length)
        html += '<div class="finding-source">涉及：' +
          escapeHtml(f.affected_resources.join(", ")) + "</div>";
      html += "</div>";
    });
    out.innerHTML = html;
  }

  // ════════════════════ 多智能体编排可视化 ════════════════════

  // 已知的固定拓扑（与后端 graph.py 一致，编译后不可变）。
  const NODE_META = {
    START: { type: "endpoint", label: "START", sub: "入口", tip: "流程入口。" },
    rules: {
      type: "deterministic", label: "rules", sub: "确定性规则",
      tip: "确定性安全规则引擎：生命体征危急值、检验异常、药物相互作用与过敏交叉反应检测。LLM 无关，始终先行，构成不可绕过的安全底线。",
    },
    history: {
      type: "specialist", label: "history", sub: "病史专家",
      tip: "病史专家 Agent：读取 FHIR 记录，生成结构化病史摘要与问题清单。",
    },
    drug: {
      type: "specialist", label: "drug", sub: "药物专家",
      tip: "药物安全专家 Agent：核查药物相互作用、过敏交叉反应与生命体征/检验异常，附引证。",
    },
    literature: {
      type: "specialist", label: "literature", sub: "文献专家",
      tip: "文献专家 Agent：检索临床指南与文献，按证据等级排序，附 PMID/指南引证。",
    },
    fanin: {
      type: "aggregator", label: "fanin", sub: "聚合",
      tip: "聚合节点：汇聚三条并行专家分支的输出，合并引证与子动作（扇入）。",
    },
    documentation: {
      type: "specialist", label: "documentation", sub: "文书",
      tip: "文书 Agent：生成 SOAP 病历草稿与 ICD-10 编码建议，标注待医生签发。",
    },
    guardrail: {
      type: "deterministic", label: "guardrail", sub: "护栏",
      tip: "护栏审查：对综合输出做最终 LLM 护栏审查，决定放行/标记/阻断，必要时触发人工复核。",
    },
    END: { type: "endpoint", label: "END", sub: "出口", tip: "流程出口。" },
  };

  // SVG 布局坐标（视口 1200 x 420）。三条专家分支纵向排列以体现扇出/扇入。
  const AGENT_LAYOUT = {
    START: { x: 60, y: 210 },
    rules: { x: 200, y: 210 },
    history: { x: 420, y: 90 },
    drug: { x: 420, y: 210 },
    literature: { x: 420, y: 330 },
    fanin: { x: 640, y: 210 },
    documentation: { x: 840, y: 210 },
    guardrail: { x: 1040, y: 210 },
    END: { x: 1170, y: 210 },
  };

  const KNOWN_EDGES = [
    ["START", "rules"],
    ["rules", "history"],
    ["rules", "drug"],
    ["rules", "literature"],
    ["history", "fanin"],
    ["drug", "fanin"],
    ["literature", "fanin"],
    ["fanin", "documentation"],
    ["documentation", "guardrail"],
    ["guardrail", "END"],
  ];

  const SEMANTIC_TYPES = new Set(["deterministic", "specialist", "aggregator", "endpoint"]);

  // 把后端返回（nodes/edges 或 langgraph drawable graph）归一化为 {nodes, edges}。
  function normalizeAgentGraph(resp) {
    let rawNodes = [];
    let rawEdges = [];
    if (Array.isArray(resp.nodes)) {
      rawNodes = resp.nodes;
      rawEdges = resp.edges || [];
    } else if (resp.graph && Array.isArray(resp.graph.nodes)) {
      rawNodes = resp.graph.nodes;
      rawEdges = resp.graph.edges || [];
    }
    const mapId = (id) => {
      const s = String(id || "");
      if (s === "__start__" || s === "__start" || s === "START") return "START";
      if (s === "__end__" || s === "__end" || s === "END") return "END";
      return s;
    };
    const nodes = rawNodes.map((n) => {
      const id = mapId(n.id || n.name || n.node_id);
      return { id, rawType: n.type || n.category || n.kind };
    });
    const edges = rawEdges.map((e) => ({
      from: mapId(e.from || e.source || e.src),
      to: mapId(e.to || e.target || e.dst),
    })).filter((e) => e.from && e.to && e.from !== e.to);
    return { nodes, edges };
  }

  function agentNodeType(id, rawType) {
    if (NODE_META[id] && NODE_META[id].type) return NODE_META[id].type;
    if (rawType && SEMANTIC_TYPES.has(rawType)) return rawType;
    return "specialist";
  }

  async function loadAgentsGraph() {
    const container = document.getElementById("agents-graph");
    const engineEl = document.getElementById("agentsEngine");
    container.innerHTML = '<p class="placeholder">获取拓扑中…</p>';
    let resp = null;
    let apiError = null;
    try {
      resp = await api("/api/v1/agents/graph");
    } catch (e) {
      apiError = e.message;
    }
    if (engineEl) {
      engineEl.textContent = "engine: " + (resp && resp.engine ? resp.engine : "未知");
    }

    // 优先用 API 返回的边；为空则回退到已知固定拓扑。
    let edges = [];
    let typeMap = {};
    if (resp) {
      const norm = normalizeAgentGraph(resp);
      edges = norm.edges;
      norm.nodes.forEach((n) => { typeMap[n.id] = agentNodeType(n.id, n.rawType); });
    }
    // 只保留两端都能布局的边。
    edges = edges.filter((e) => AGENT_LAYOUT[e.from] && AGENT_LAYOUT[e.to]);
    if (!edges.length) {
      edges = KNOWN_EDGES.map(([from, to]) => ({ from, to }));
    }

    const nodeIds = new Set();
    edges.forEach((e) => { nodeIds.add(e.from); nodeIds.add(e.to); });
    // 确保已知节点全部画出。
    Object.keys(AGENT_LAYOUT).forEach((id) => nodeIds.add(id));

    renderAgentsSvg(container, nodeIds, edges, typeMap, apiError);
  }

  document.getElementById("agents-load-btn").addEventListener("click", loadAgentsGraph);

  function renderAgentsSvg(container, nodeIds, edges, typeMap, apiError) {
    const W = 1220, H = 420;
    const hw = 72, hh = 28; // 圆角矩形半宽/半高
    const ehw = 22;         // 入口/出口圆半径
    const typeOf = (id) => (typeMap && typeMap[id]) ||
      (NODE_META[id] && NODE_META[id].type) || "specialist";
    const labelOf = (id) => (NODE_META[id] && NODE_META[id].label) || id;
    const subOf = (id) => (NODE_META[id] && NODE_META[id].sub) || "";
    const tipOf = (id) => (NODE_META[id] && NODE_META[id].tip) || "";

    let svg = '<svg class="agents-svg" viewBox="0 0 ' + W + " " + H +
      '" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="多智能体编排拓扑">';
    svg += '<defs><marker id="agentArrow" viewBox="0 0 10 10" refX="9" refY="5" ' +
      'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
      '<path d="M0,0 L10,5 L0,10 z"></path></marker></defs>';

    // 边（先画，使节点叠在上层）。
    edges.forEach((e) => {
      const a = AGENT_LAYOUT[e.from], b = AGENT_LAYOUT[e.to];
      if (!a || !b) return;
      const aHw = typeOf(e.from) === "endpoint" ? ehw : hw;
      const bHw = typeOf(e.to) === "endpoint" ? ehw : hw;
      const x1 = a.x + aHw, y1 = a.y;
      const x2 = b.x - bHw, y2 = b.y;
      const dx = Math.max(24, (x2 - x1) / 2);
      const d = "M " + x1 + " " + y1 + " C " + (x1 + dx) + " " + y1 +
        ", " + (x2 - dx) + " " + y2 + ", " + x2 + " " + y2;
      svg += '<path class="agent-edge" d="' + d + '" marker-end="url(#agentArrow)"/>';
    });

    // 节点（按 x 再 y 排序，保证渲染顺序稳定）。
    const idArr = Array.from(nodeIds).filter((id) => AGENT_LAYOUT[id]);
    idArr.sort((a, b) => (AGENT_LAYOUT[a].x - AGENT_LAYOUT[b].x) ||
      (AGENT_LAYOUT[a].y - AGENT_LAYOUT[b].y));
    idArr.forEach((id) => {
      const p = AGENT_LAYOUT[id];
      const t = typeOf(id);
      if (t === "endpoint") {
        svg += '<g class="agent-node agent-endpoint">';
        svg += '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + ehw + '"/>';
        svg += '<text x="' + p.x + '" y="' + (p.y + 4) +
          '" class="agent-node-label">' + escapeHtml(labelOf(id)) + "</text>";
        svg += "<title>" + escapeHtml(tipOf(id)) + "</title>";
        svg += "</g>";
      } else {
        const x = p.x - hw, y = p.y - hh;
        svg += '<g class="agent-node agent-' + t + '">';
        svg += '<rect x="' + x + '" y="' + y + '" width="' + (hw * 2) +
          '" height="' + (hh * 2) + '" rx="10" ry="10"/>';
        svg += '<text x="' + p.x + '" y="' + (p.y - 3) +
          '" class="agent-node-label">' + escapeHtml(labelOf(id)) + "</text>";
        if (subOf(id)) {
          svg += '<text x="' + p.x + '" y="' + (p.y + 13) +
            '" class="agent-node-sub">' + escapeHtml(subOf(id)) + "</text>";
        }
        svg += "<title>" + escapeHtml(tipOf(id)) + "</title>";
        svg += "</g>";
      }
    });
    svg += "</svg>";

    let html = "";
    if (apiError) {
      html += '<div class="api-warn">⚠ API 调用失败：' + escapeHtml(apiError) +
        "（下方展示已知固定拓扑）</div>";
    }
    html += svg;
    container.innerHTML = html;
  }

  // ════════════════════ 智能对话 ════════════════════

  const CHAT_STORE_KEY = "doctoragent_chats";
  const PROMPT_STORE_KEY = "doctoragent_prompts";
  const ACTIVE_PROMPT_KEY = "doctoragent_active_prompt";
  const DRAFT_KEY = "doctoragent_draft";

  let chatState = {
    sessions: [],      // [{id, title, messages:[{role,content,ts}], createdAt}]
    currentId: null,
    webSearch: false,
    useKnowledge: true, // 知识库引用开关（RAG）
    attachedFile: null,
    abortCtrl: null,
    initialized: false,
  };
  // 输入历史回溯（↑/↓）
  let inputHistory = [];
  let historyIdx = -1;

  function loadChatSessions() {
    try { return JSON.parse(localStorage.getItem(CHAT_STORE_KEY) || "[]"); }
    catch (e) { return []; }
  }
  function saveChatSessions() {
    try {
      localStorage.setItem(CHAT_STORE_KEY, JSON.stringify(chatState.sessions));
    } catch (e) {
      // 配额超限（QuotaExceededError）：尝试裁剪旧会话消息后重试
      if (e && (e.name === "QuotaExceededError" || e.code === 22)) {
        try {
          // 保留每个会话最近 20 条消息，删除最旧的会话
          const trimmed = chatState.sessions.map(function (s) {
            return Object.assign({}, s, {
              messages: (s.messages || []).slice(-20),
            });
          });
          // 如果仍超限，只保留最近 3 个会话
          let toSave = trimmed;
          while (toSave.length > 3) {
            toSave = toSave.slice(1);
            try {
              localStorage.setItem(CHAT_STORE_KEY, JSON.stringify(toSave));
              chatState.sessions = toSave;
              toast("存储空间已满，已自动清理旧对话", "warn");
              return;
            } catch (e2) { /* 继续裁剪 */ }
          }
          // 最后一次尝试
          localStorage.setItem(CHAT_STORE_KEY, JSON.stringify(toSave));
          chatState.sessions = toSave;
          toast("存储空间已满，已自动清理旧对话", "warn");
        } catch (e2) {
          console.error("saveChatSessions: even trimmed data exceeds quota", e2);
          toast("存储空间已满，无法保存对话历史", "error");
        }
      } else {
        // 其他异常（如隐私模式/禁用 localStorage）
        console.error("saveChatSessions failed", e);
      }
    }
  }

  function initChat() {
    if (chatState.initialized) { renderChatHistory(); return; }
    chatState.initialized = true;
    chatState.sessions = loadChatSessions();
    if (!chatState.sessions.length) {
      createChatSession();
    } else {
      chatState.currentId = chatState.sessions[0].id;
    }
    renderChatHistory();
    renderChatMessages();

    // 新建对话
    document.getElementById("newChatBtn").addEventListener("click", () => {
      createChatSession();
      renderChatHistory();
      renderChatMessages();
    });

    // 清空全部对话
    document.getElementById("clearAllChatsBtn").addEventListener("click", () => {
      if (chatState.sessions.length === 0) { toast("没有对话可清空", "info"); return; }
      confirmDialog({
        title: "清空全部对话",
        message: "确定要删除所有 " + chatState.sessions.length + " 个对话吗？此操作不可撤销。",
        danger: true,
        icon: "🗑",
        okText: "清空",
        onConfirm: async function () {
          chatState.sessions = [];
          chatState.currentId = null;
          saveChatSessions();
          createChatSession();
          renderChatHistory();
          renderChatMessages();
          toast("已清空全部对话", "success");
        },
      });
    });

    // 侧栏折叠
    document.getElementById("toggleSidebarBtn").addEventListener("click", () => {
      const sidebar = document.getElementById("chatSidebar");
      const main = document.getElementById("chatMain");
      sidebar.classList.toggle("collapsed");
      main.classList.toggle("expanded");
      const btn = document.getElementById("toggleSidebarBtn");
      btn.textContent = sidebar.classList.contains("collapsed") ? "▶" : "◀";
    });

    // 搜索历史
    document.getElementById("chatSearchInput").addEventListener("input", debounce(() => {
      renderChatHistory();
    }, 200));
    // 刷新历史（重新从 localStorage 读取，并清除搜索）
    document.getElementById("chatRefreshBtn").addEventListener("click", () => {
      const btn = document.getElementById("chatRefreshBtn");
      btn.classList.add("spinning");
      setTimeout(() => btn.classList.remove("spinning"), 600);
      chatState.sessions = loadChatSessions();
      if (!chatState.sessions.length) {
        createChatSession();
      } else if (!chatState.sessions.find((s) => s.id === chatState.currentId)) {
        chatState.currentId = chatState.sessions[0].id;
      }
      document.getElementById("chatSearchInput").value = "";
      renderChatHistory();
      renderChatMessages();
      toast("历史已刷新", "success");
    });

    // 联网按钮
    document.getElementById("chatWebBtn").addEventListener("click", () => {
      chatState.webSearch = !chatState.webSearch;
      const btn = document.getElementById("chatWebBtn");
      btn.classList.toggle("active", chatState.webSearch);
      toast(chatState.webSearch ? "联网搜索已开启" : "联网搜索已关闭", chatState.webSearch ? "success" : "info");
    });

    // 导出对话（Markdown / PDF / Word / JSON）
    document.querySelectorAll(".chat-export-btn, .chat-export-json, .chat-export-pdf, .chat-export-word").forEach((btn) => {
      btn.addEventListener("click", () => {
        const chat = getCurrentChat();
        if (!chat) { toast("当前无对话可导出", "error"); return; }
        const fmt = btn.dataset.format;
        if (fmt === "pdf" || fmt === "docx") exportChatServer(chat, fmt);
        else exportChat(chat.id, fmt);
      });
    });

    // 知识库引用开关（切换 RAG 检索）
    const kbBtn = document.getElementById("chatKbBtn");
    if (kbBtn) {
      kbBtn.addEventListener("click", () => {
        chatState.useKnowledge = !chatState.useKnowledge;
        kbBtn.classList.toggle("active", chatState.useKnowledge);
        toast(chatState.useKnowledge ? "知识库引用已开启（回答会检索 Vault）" : "知识库引用已关闭", chatState.useKnowledge ? "success" : "info");
      });
      kbBtn.classList.toggle("active", chatState.useKnowledge !== false);
    }

    // 服务器端导出（PDF / DOCX）：POST 当前会话消息 → 下载文件
    async function exportChatServer(chat, format) {
      if (!chat || !chat.messages || chat.messages.length === 0) { toast("当前无对话可导出", "error"); return; }
      const messages = chat.messages.map(function (m) {
        return { role: m.role, content: typeof m.content === "string" ? m.content : (m.content && m.content.text) || "" };
      });
      try {
        const resp = await fetch("/api/v1/doc/export", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, getToken() ? { "Authorization": "Bearer " + getToken() } : {}),
          body: JSON.stringify({ format: format, title: chat.title || "对话", messages: messages }),
        });
        if (!resp.ok) { let b = {}; try { b = await resp.json(); } catch (e) {} toast("导出失败：" + (b.detail || resp.status), "error"); return; }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = (chat.title || "对话").replace(/[^\w\u4e00-\u9fa5]/g, "_") + "." + format;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 3000);
        toast("已导出为 " + format.toUpperCase(), "success");
      } catch (e) { toast("导出失败：" + e.message, "error"); }
    }

    // 上传文件
    const MAX_FILE_SIZE = 20 * 1024 * 1024; // 20MB
    const TEXT_FILE_EXTS = [".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".xml", ".yaml", ".yml", ".log", ".html", ".htm", ".js", ".py", ".java", ".c", ".cpp", ".go", ".rs", ".sql", ".ini", ".cfg", ".conf", ".env"];
    function isTextFile(file) {
      // 优先用 MIME 类型判断
      if (file.type) {
        if (file.type.startsWith("text/")) return true;
        if (file.type === "application/json" || file.type === "application/xml" ||
            file.type === "application/javascript" || file.type === "application/x-yaml") return true;
      }
      // 扩展名兜底
      const name = (file.name || "").toLowerCase();
      return TEXT_FILE_EXTS.some(function (ext) { return name.endsWith(ext); });
    }
    document.getElementById("chatUploadBtn").addEventListener("click", () => {
      document.getElementById("chatFileInput").click();
    });
    document.getElementById("chatFileInput").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      // 文件大小限制
      if (file.size > MAX_FILE_SIZE) {
        toast("文件过大（限制 " + Math.round(MAX_FILE_SIZE / 1024 / 1024) + "MB），当前 " + (file.size / 1024 / 1024).toFixed(1) + "MB", "error");
        e.target.value = "";
        return;
      }
      chatState.attachedFile = file;
      document.getElementById("chatFileName").textContent = file.name + " (" + (file.size / 1024).toFixed(1) + "KB)";
      renderAttachedFiles();
    });

    // 输入框自动高度 + Enter 发送
    const input = document.getElementById("chatInput");
    // 恢复草稿
    try { input.value = localStorage.getItem(DRAFT_KEY) || ""; } catch (e) {}
    const counter = document.getElementById("charCounter");
    const updateCharCounter = function () {
      if (counter) counter.textContent = input.value.length + " 字";
    };
    updateCharCounter();
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 160) + "px";
      updateCharCounter();
    });
    // 草稿自动持久化（立即保存 + 失焦保存 + 关闭页面前保存）
    input.addEventListener("input", function () {
      try { localStorage.setItem(DRAFT_KEY, input.value); } catch (e) {}
    });
    input.addEventListener("blur", function () {
      try { localStorage.setItem(DRAFT_KEY, input.value); } catch (e) {}
    });
    window.addEventListener("beforeunload", function () {
      try { localStorage.setItem(DRAFT_KEY, input.value); } catch (e) {}
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
        return;
      }
      // 输入历史回溯（↑/↓）：仅当输入框为空时启用回溯，避免影响光标移动
      if (e.key === "ArrowUp" && !e.shiftKey && input.value === "") {
        if (inputHistory.length > 0) {
          historyIdx = Math.min(historyIdx + 1, inputHistory.length - 1);
          input.value = inputHistory[inputHistory.length - 1 - historyIdx];
          updateCharCounter();
          setTimeout(function () { input.setSelectionRange(input.value.length, input.value.length); }, 0);
        }
        e.preventDefault();
      } else if (e.key === "ArrowDown" && !e.shiftKey && historyIdx >= 0) {
        historyIdx--;
        if (historyIdx < 0) {
          input.value = "";
        } else {
          input.value = inputHistory[inputHistory.length - 1 - historyIdx];
        }
        updateCharCounter();
        setTimeout(function () { input.setSelectionRange(input.value.length, input.value.length); }, 0);
        e.preventDefault();
      }
    });

    // 发送按钮
    document.getElementById("chatSendBtn").addEventListener("click", sendChatMessage);

    // ── 语音输入（ASR）：MediaRecorder 录音 → /api/v1/voice/transcribe ──
    let voiceRecorder = null;
    let voiceChunks = [];
    let voiceStream = null;
    const voiceBtn = document.getElementById("chatVoiceBtn");
    const voiceLabel = document.getElementById("chatVoiceLabel");
    const ttsBtn = document.getElementById("chatTtsBtn");

    async function checkVoiceStatus() {
      try {
        const r = await api("/api/v1/voice/status");
        if (!r || !r.transcribe) voiceBtn.classList.add("hidden");
      } catch (e) { /* 保持按钮可见，点击时再报错 */ }
    }
    checkVoiceStatus();

    function setVoiceRecording(on) {
      if (!voiceBtn) return;
      voiceBtn.classList.toggle("recording", on);
      if (voiceLabel) voiceLabel.textContent = on ? "停止" : "语音";
      voiceBtn.title = on ? "点击停止录音" : "语音输入（点击录音）";
    }

    async function startVoiceRecording() {
      if (!window.MediaRecorder) { alert("当前浏览器不支持 MediaRecorder 录音"); return; }
      try {
        voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) {
        alert("无法访问麦克风：" + e.message);
        return;
      }
      voiceChunks = [];
      const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
      voiceRecorder = new MediaRecorder(voiceStream, mime ? { mimeType: mime } : undefined);
      voiceRecorder.ondataavailable = function (ev) { if (ev.data && ev.data.size > 0) voiceChunks.push(ev.data); };
      voiceRecorder.onstop = transcribeVoice;
      voiceRecorder.start();
      setVoiceRecording(true);
    }

    function stopVoiceRecording() {
      if (voiceRecorder && voiceRecorder.state !== "inactive") voiceRecorder.stop();
      if (voiceStream) { voiceStream.getTracks().forEach(function (t) { t.stop(); }); voiceStream = null; }
      setVoiceRecording(false);
    }

    async function transcribeVoice() {
      if (voiceChunks.length === 0) return;
      const blob = new Blob(voiceChunks, { type: voiceChunks[0].type || "audio/webm" });
      const fd = new FormData();
      fd.append("file", blob, "voice.webm");
      const input = document.getElementById("chatInput");
      if (input) input.placeholder = "转写中…";
      try {
        const r = await api("/api/v1/voice/transcribe", { method: "POST", body: fd });
        if (input && r && r.text) {
          input.value = input.value ? input.value + " " + r.text : r.text;
          input.dispatchEvent(new Event("input"));
          input.focus();
        } else if (input) { input.placeholder = "转写失败，请重试"; }
      } catch (e) {
        console.error("Voice transcribe failed", e);
        if (input) input.placeholder = "语音转写失败";
      } finally {
        setTimeout(function () { if (input) input.placeholder = "输入消息，Enter 发送，Shift+Enter 换行…"; }, 1500);
      }
    }

    if (voiceBtn) {
      voiceBtn.addEventListener("click", function () {
        if (voiceRecorder && voiceRecorder.state === "recording") stopVoiceRecording();
        else startVoiceRecording();
      });
    }

    // ── 语音输出（TTS）：朗读最后一条助手回复 ──
    async function speakLastReply() {
      const current = chatState.sessions.find(function (s) { return s.id === chatState.currentId; });
      if (!current) return;
      let last = null;
      for (let i = current.messages.length - 1; i >= 0; i--) {
        if (current.messages[i].role === "assistant" && current.messages[i].content) {
          last = current.messages[i].content; break;
        }
      }
      if (!last) { alert("还没有可朗读的回复"); return; }
      const text = typeof last === "string" ? last : (last.text || "");
      if (!text) { alert("回复为空"); return; }
      try {
        const resp = await fetch("/api/v1/voice/synthesize", {
          method: "POST",
          headers: Object.assign({ "Content-Type": "application/json" }, getToken() ? { Authorization: "Bearer " + getToken() } : {}),
          body: JSON.stringify({ text: text }),
        });
        if (!resp.ok) {
          const body = await resp.json().catch(function () { return {}; });
          alert("语音合成不可用：" + (body.detail || resp.status));
          return;
        }
        const audioBlob = await resp.blob();
        const url = URL.createObjectURL(audioBlob);
        const audio = new Audio(url);
        audio.onended = function () { URL.revokeObjectURL(url); };
        audio.play();
      } catch (e) { console.error("TTS failed", e); alert("语音朗读失败：" + e.message); }
    }
    if (ttsBtn) ttsBtn.addEventListener("click", speakLastReply);

    // 停止按钮
    document.getElementById("chatStopBtn").addEventListener("click", () => {
      if (chatState.abortCtrl) {
        chatState.abortCtrl.abort();
        chatState.abortCtrl = null;
      }
      document.getElementById("chatSendBtn").classList.remove("hidden");
      document.getElementById("chatStopBtn").classList.add("hidden");
    });

    // 文件拖拽上传：复用 chatFileInput 的 change 处理逻辑
    const chatInputWrap = document.querySelector(".chat-input-area") || document.getElementById("chatMessages");
    if (chatInputWrap) {
      chatInputWrap.addEventListener("dragover", function (e) {
        e.preventDefault();
        chatInputWrap.classList.add("drag-over");
      });
      chatInputWrap.addEventListener("dragleave", function (e) {
        chatInputWrap.classList.remove("drag-over");
      });
      chatInputWrap.addEventListener("drop", function (e) {
        e.preventDefault();
        chatInputWrap.classList.remove("drag-over");
        const files = e.dataTransfer && e.dataTransfer.files;
        if (files && files.length > 0) {
          const fileInput = document.getElementById("chatFileInput");
          if (fileInput) {
            const dt = new DataTransfer();
            dt.items.add(files[0]);
            fileInput.files = dt.files;
            fileInput.dispatchEvent(new Event("change"));
          }
        }
      });
    }

    // 建议点击
    document.querySelectorAll(".chat-suggestion").forEach((s) => {
      s.addEventListener("click", () => {
        input.value = s.dataset.q;
        input.focus();
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 160) + "px";
      });
    });
  }

  function createChatSession() {
    const session = {
      id: "chat-" + Date.now(),
      title: "新对话",
      messages: [],
      createdAt: new Date().toISOString(),
    };
    chatState.sessions.unshift(session);
    chatState.currentId = session.id;
    saveChatSessions();
  }

  function getCurrentChat() {
    return chatState.sessions.find((s) => s.id === chatState.currentId);
  }

  function renderChatHistory() {
    const box = document.getElementById("chatHistory");
    if (!chatState.sessions.length) {
      box.innerHTML = '<div class="chat-empty">' +
        '<div class="chat-empty-icon">💬</div>' +
        '<p>暂无对话</p>' +
        '<p class="hint">点击「+ 新建对话」开始</p></div>';
      return;
    }
    const q = (document.getElementById("chatSearchInput")?.value || "").trim().toLowerCase();
    let sessions = chatState.sessions;
    if (q) {
      sessions = sessions.filter((s) => {
        if (s.title.toLowerCase().includes(q)) return true;
        return (s.messages || []).some((m) =>
          (m.content || "").toLowerCase().includes(q)
        );
      });
    }
    if (!sessions.length) {
      box.innerHTML = '<div class="chat-empty">' +
        '<div class="chat-empty-icon">🔍</div>' +
        '<p>未匹配到对话</p>' +
        '<p class="hint">尝试其他关键词</p></div>';
      return;
    }
    box.innerHTML = sessions.map((s, i) => {
      const active = s.id === chatState.currentId ? " active" : "";
      const msgCount = s.messages.length;
      const lastMsg = s.messages.length
        ? s.messages[s.messages.length - 1]
        : null;
      const preview = lastMsg
        ? (lastMsg.role === "user" ? "" : "") + (lastMsg.content || "").slice(0, 40)
        : "空对话";
      return '<div class="chat-history-item' + active + '" data-id="' + s.id +
        '" style="animation-delay:' + Math.min(i * 30, 300) + 'ms">' +
        '<div class="chat-history-title">' + escapeHtml(s.title) + "</div>" +
        '<div class="chat-history-preview">' + escapeHtml(preview) + "</div>" +
        '<div class="chat-history-meta">' + msgCount + " 条</div>" +
        '<button class="chat-history-del" data-id="' + s.id + '" title="删除对话">✕</button>' +
        "</div>";
    }).join("");
    box.querySelectorAll(".chat-history-item").forEach((el) => {
      el.addEventListener("click", (e) => {
        if (e.target.classList.contains("chat-history-del")) return;
        // 只更新 active 样式，不重新渲染整个列表（避免 dblclick 失效）
        chatState.currentId = el.dataset.id;
        box.querySelectorAll(".chat-history-item").forEach((item) => item.classList.remove("active"));
        el.classList.add("active");
        renderChatMessages();
      });
      // 对话标题双击编辑
      const titleEl = el.querySelector(".chat-history-title");
      if (titleEl) {
        titleEl.addEventListener("dblclick", (e) => {
          e.stopPropagation();
          e.preventDefault();
          const chat = chatState.sessions.find((s) => s.id === el.dataset.id);
          if (!chat) return;
          const newName = prompt("重命名对话", chat.title);
          if (newName && newName.trim()) {
            chat.title = newName.trim();
            saveChatSessions();
            renderChatHistory();
            toast("已重命名", "success");
          }
        });
      }
    });
    box.querySelectorAll(".chat-history-del").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteChatSession(el.dataset.id);
      });
    });
  }

  async function deleteChatSession(id) {
    const chat = chatState.sessions.find((s) => s.id === id);
    const label = chat ? chat.title : "该对话";
    const ok = await confirmDialog({
      title: "删除对话",
      message: "确定要删除「" + label + "」吗？所有消息将永久丢失，不可恢复。",
      okText: "删除",
      icon: "🗑",
    });
    if (!ok) return;
    chatState.sessions = chatState.sessions.filter((s) => s.id !== id);
    if (chatState.currentId === id) {
      chatState.currentId = chatState.sessions[0]?.id || null;
      if (!chatState.currentId) {
        createChatSession();
      }
    }
    saveChatSessions();
    renderChatHistory();
    renderChatMessages();
    toast("对话已删除", "success");
  }

  // 导出对话：format="json" 或 "md"
  function exportChat(chatId, format) {
    const chat = chatState.sessions.find(function (s) { return s.id === chatId; });
    if (!chat) { toast("未找到对话", "error"); return; }
    let content, mime, ext;
    if (format === "json") {
      content = JSON.stringify(chat, null, 2);
      mime = "application/json"; ext = "json";
    } else {
      content = "# " + chat.title + "\n\n";
      (chat.messages || []).forEach(function (m) {
        content += "**" + (m.role === "user" ? "👤 用户" : "🤖 助手") + "** (" + (m.ts || "") + ")\n\n" + m.content + "\n\n---\n\n";
      });
      mime = "text/markdown"; ext = "md";
    }
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = (chat.title || "对话").replace(/[^\w\u4e00-\u9fa5]/g, "_") + "." + ext;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast("已导出 " + ext.toUpperCase() + " 文件", "success");
  }

  function renderChatMessages() {
    const box = document.getElementById("chatMessages");
    const chat = getCurrentChat();
    if (!chat || !chat.messages.length) {
      const welcome = document.getElementById("chatWelcome");
      if (welcome) {
        box.innerHTML = "";
        box.appendChild(welcome);
        welcome.style.display = "";
      } else {
        box.innerHTML = getChatWelcomeHtml();
      }
      return;
    }
    box.innerHTML = chat.messages.map((m, i) => renderMessageHtml(m, i)).join("");
    box.scrollTop = box.scrollHeight;
    bindMessageActions();
    highlightCodeBlocks();
  }

  // 代码高亮：对未处理的代码块调用 hljs.highlightElement
  function highlightCodeBlocks() {
    if (!window.hljs) return;
    document.querySelectorAll("#chatMessages pre.msg-code-block code:not([data-highlighted])").forEach(function (el) {
      el.setAttribute("data-highlighted", "true");
      try { hljs.highlightElement(el); } catch (e) { /* ignore */ }
    });
  }

  function getChatWelcomeHtml() {
    return '<div class="chat-welcome" id="chatWelcome">' +
      '<div class="chat-welcome-icon">⚕</div>' +
      "<h2>DoctorAgent 智能助手</h2>" +
      "<p>基于多智能体编排的医疗 AI 助手，支持 RAG 检索、工具调用与临床推理</p>" +
      '<div class="chat-suggestions">' +
      '<button class="chat-suggestion" data-q="帮我搜索文档库中关于糖尿病管理的资料">📄 搜索文档库</button>' +
      '<button class="chat-suggestion" data-q="分析当前系统中有哪些已注册的 MCP 工具">🔧 查看 MCP 工具</button>' +
      '<button class="chat-suggestion" data-q="总结一下 Warfarin 与 Fluconazole 联用的风险">💊 药物相互作用</button>' +
      "</div></div>";
  }

  function renderMessageHtml(m, idx) {
    const isUser = m.role === "user";
    const avatar = isUser ? "🧑" : "⚕";
    let actions = "";
    if (!isUser) {
      actions = '<div class="msg-actions">' +
        '<button class="msg-action-btn" data-act="copy" data-idx="' + idx + '" title="复制">📋</button>' +
        '<button class="msg-action-btn" data-act="regen" data-idx="' + idx + '" title="重新生成">🔄</button>' +
        '<button class="msg-action-btn" data-act="del" data-idx="' + idx + '" title="删除">🗑</button>' +
        "</div>";
    } else {
      actions = '<div class="msg-actions">' +
        '<button class="msg-action-btn" data-act="copy" data-idx="' + idx + '" title="复制">📋</button>' +
        '<button class="msg-action-btn" data-act="edit" data-idx="' + idx + '" title="编辑重发">✏</button>' +
        '<button class="msg-action-btn" data-act="undo" data-idx="' + idx + '" title="撤回">↩</button>' +
        "</div>";
    }
    return '<div class="chat-msg ' + (isUser ? "msg-user" : "msg-assistant") + '">' +
      '<div class="msg-avatar">' + avatar + "</div>" +
      '<div class="msg-body">' +
      '<div class="msg-content">' + formatMessageContent(m.content) + "</div>" +
      (m.steps ? renderMsgSteps(m.steps) : "") +
      '<div class="msg-meta">' +
      '<span class="msg-time">' + (m.ts || "") + "</span>" +
      actions +
      "</div></div></div>";
  }

  function renderMsgSteps(steps) {
    if (!steps || !steps.length) return "";
    let html = '<details class="msg-steps"><summary>执行轨迹（' + steps.length + " 步）</summary>";
    steps.forEach((s, i) => {
      html += '<div class="msg-step"><span class="msg-step-type">' +
        escapeHtml(s.step_type || "") + "</span>" +
        (s.tool_name ? ' <span class="msg-step-tool">' + escapeHtml(s.tool_name) + "</span>" : "") +
        "<pre>" + escapeHtml(s.content || "") + "</pre></div>";
    });
    html += "</details>";
    return html;
  }

  function formatMessageContent(content) {
    if (!content) return "";
    // 简单 Markdown 渲染：代码块、粗体、列表、换行
    let html = escapeHtml(content);
    // 代码块
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) =>
      '<pre class="msg-code-block"><code>' + code + "</code></pre>");
    // 行内代码
    html = html.replace(/`([^`]+)`/g, '<code class="msg-inline-code">$1</code>');
    // 粗体
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // 列表项
    html = html.replace(/^(\d+\.)\s/gm, '<span class="msg-ol-num">$1</span> ');
    html = html.replace(/^[-•]\s/gm, '<span class="msg-bullet">•</span> ');
    // 换行
    html = html.replace(/\n/g, "<br>");
    return html;
  }

  function bindMessageActions() {
    document.querySelectorAll(".msg-action-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const idx = Number(btn.dataset.idx);
        const act = btn.dataset.act;
        const chat = getCurrentChat();
        if (!chat || !chat.messages[idx]) return;
        if (act === "copy") {
          try {
            await navigator.clipboard.writeText(chat.messages[idx].content);
            toast("已复制到剪贴板", "success");
          } catch (e) {
            // 回退方案：选中文本
            const range = document.createRange();
            const msgEls = document.querySelectorAll(".chat-msg");
            const target = msgEls[idx];
            if (target) {
              const contentEl = target.querySelector(".msg-content");
              if (contentEl) {
                range.selectNodeContents(contentEl);
                const sel = window.getSelection();
                sel.removeAllRanges();
                sel.addRange(range);
                toast("已选中，按 Ctrl+C 复制", "info");
              }
            }
          }
        } else if (act === "del") {
          const ok = await confirmDialog({
            title: "删除消息",
            message: "确定要删除这条消息吗？此操作不可撤销。",
            okText: "删除",
            icon: "🗑",
          });
          if (!ok) return;
          chat.messages.splice(idx, 1);
          saveChatSessions();
          renderChatMessages();
          toast("消息已删除", "success");
        } else if (act === "undo") {
          // 撤回：删除该用户消息及其后的所有消息（含助手回复）
          const ok = await confirmDialog({
            title: "撤回消息",
            message: "将撤回此消息及之后的所有回复，并回到输入框重新编辑。是否继续？",
            okText: "撤回",
            icon: "↩",
          });
          if (!ok) return;
          const withdrawn = chat.messages[idx].content;
          chat.messages.splice(idx);
          saveChatSessions();
          renderChatMessages();
          const inputEl = document.getElementById("chatInput");
          inputEl.value = withdrawn;
          inputEl.focus();
          inputEl.style.height = "auto";
          inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
          toast("已撤回，可在输入框编辑后重发", "success");
        } else if (act === "edit") {
          // 编辑重发：把内容回填到输入框，撤回后续消息
          const ok = await confirmDialog({
            title: "编辑并重发",
            message: "将清空此消息之后的回复，并把内容回填到输入框。是否继续？",
            okText: "编辑",
            icon: "✏",
          });
          if (!ok) return;
          const text = chat.messages[idx].content;
          chat.messages.splice(idx);
          saveChatSessions();
          renderChatMessages();
          const inputEl = document.getElementById("chatInput");
          inputEl.value = text;
          inputEl.focus();
          inputEl.style.height = "auto";
          inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
          toast("已回填，可编辑后发送", "info");
        } else if (act === "regen") {
          // 找到上一条用户消息，重新发送
          let userIdx = idx - 1;
          while (userIdx >= 0 && chat.messages[userIdx].role !== "user") userIdx--;
          if (userIdx < 0) { toast("未找到对应的用户消息", "error"); return; }
          const userMsg = chat.messages[userIdx].content;
          // 删除当前助手回复
          chat.messages.splice(idx, 1);
          saveChatSessions();
          renderChatMessages();
          // 重新发送
          streamAgentResponse(userMsg, true);
        }
      });
    });
  }

  function renderAttachedFiles() {
    const box = document.getElementById("chatAttachedFiles");
    if (!chatState.attachedFile) { box.innerHTML = ""; return; }
    box.innerHTML = '<span class="chat-attached-file">' +
      escapeHtml(chatState.attachedFile.name) +
      '<button class="chat-attached-remove" title="移除">✕</button></span>';
    box.querySelector(".chat-attached-remove").addEventListener("click", () => {
      chatState.attachedFile = null;
      document.getElementById("chatFileName").textContent = "";
      document.getElementById("chatFileInput").value = "";
      renderAttachedFiles();
    });
  }

  async function sendChatMessage() {
    // 并发保护：流式生成中不允许发送新消息
    if (chatState.abortCtrl) {
      toast("正在生成回复，请等待完成或点击停止", "info");
      return;
    }
    const input = document.getElementById("chatInput");
    if (!input) return;
    const text = input.value.trim();
    if (!text && !chatState.attachedFile) return;
    let chat = getCurrentChat();
    if (!chat) { createChatSession(); chat = getCurrentChat(); }
    if (!chat) { toast("无法创建对话", "error"); return; }

    // 上传文件（如有）
    let fileNote = "";
    if (chatState.attachedFile) {
      const file = chatState.attachedFile;
      const sendBtn = document.getElementById("chatSendBtn");
      // 上传中 loading 指示
      if (sendBtn) { sendBtn.disabled = true; sendBtn.style.opacity = "0.6"; }
      toast("正在上传文件…", "info");
      try {
        let uploadResult = null;
        if (isTextFile(file)) {
          // 文本文件：用 /inbox/ingest 明文接口
          const content = await file.text();
          uploadResult = await api("/inbox/ingest", {
            method: "POST",
            body: { content: content, filename: file.name },
            timeoutMs: 60000,
          });
        } else {
          // 二进制文件：用 /inbox/submit/batch multipart 接口
          const formData = new FormData();
          formData.append("files", file, file.name);
          const token = getToken();
          const headers = {};
          if (token) headers["Authorization"] = "Bearer " + token;
          const ctrl = new AbortController();
          const timer = setTimeout(function () { ctrl.abort(); }, 120000);
          try {
            const res = await fetch("/inbox/submit/batch", {
              method: "POST",
              headers: headers,
              body: formData,
              signal: ctrl.signal,
            });
            clearTimeout(timer);
            if (!res.ok) {
              let msg = res.status + " " + res.statusText;
              try { const j = await res.json(); if (j.detail) msg = j.detail; } catch (e2) { /* ignore */ }
              throw new Error(msg);
            }
            uploadResult = await res.json();
            if (uploadResult.failed > 0) {
              throw new Error("部分文件上传失败：" + (uploadResult.results || []).filter(function (r) { return !r.ok; }).map(function (r) { return r.message; }).join("; "));
            }
          } catch (fetchErr) {
            clearTimeout(timer);
            throw fetchErr;
          }
        }
        // 检查处理结果：HTTP 200 但 ok=false 表示文件已上传但处理失败
        if (uploadResult && uploadResult.ok === false) {
          const errMsg = uploadResult.message || uploadResult.state || "处理失败";
          toast("文件已上传但处理失败：" + errMsg, "warn");
          fileNote = "\n[已上传文件: " + file.name + "（处理失败: " + errMsg + "）]";
        } else {
          fileNote = "\n[已上传文件: " + file.name + "]";
          toast("文件已上传到 Vault", "success");
        }
      } catch (e) {
        // 上传失败：不拼接 fileNote，避免误导用户
        toast("文件上传失败：" + e.message, "error");
        if (sendBtn) { sendBtn.disabled = false; sendBtn.style.opacity = ""; }
        chatState.attachedFile = null;
        const fnEl = document.getElementById("chatFileName");
        const fiEl = document.getElementById("chatFileInput");
        if (fnEl) fnEl.textContent = "";
        if (fiEl) fiEl.value = "";
        renderAttachedFiles();
        // 上传失败时如果没有文本内容，不继续发送
        if (!text) return;
      }
      if (sendBtn) { sendBtn.disabled = false; sendBtn.style.opacity = ""; }
      chatState.attachedFile = null;
      const fnEl = document.getElementById("chatFileName");
      const fiEl = document.getElementById("chatFileInput");
      if (fnEl) fnEl.textContent = "";
      if (fiEl) fiEl.value = "";
      renderAttachedFiles();
    }

    // 构造消息内容
    const fullText = text + fileNote;
    const userMsg = { role: "user", content: fullText, ts: new Date().toLocaleTimeString() };
    chat.messages.push(userMsg);

    // 更新标题（首次消息）
    if (chat.title === "新对话") {
      chat.title = text.slice(0, 30) + (text.length > 30 ? "…" : "");
    }

    input.value = "";
    input.style.height = "auto";
    // 清除草稿
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
    // 更新字数计数器
    const counter = document.getElementById("charCounter");
    if (counter) counter.textContent = "0 字";
    // 记录输入历史
    if (text) { inputHistory.push(text); if (inputHistory.length > 50) inputHistory.shift(); historyIdx = -1; }
    saveChatSessions();
    renderChatHistory();
    renderChatMessages();

    // 流式发送
    await streamAgentResponse(text);
  }

  async function streamAgentResponse(userText, isRegen) {
    const chat = getCurrentChat();
    if (!chat) return;
    // 并发保护：如果已有进行中的流，先中止
    if (chatState.abortCtrl) {
      try { chatState.abortCtrl.abort(); } catch (e) { /* ignore */ }
      chatState.abortCtrl = null;
    }

    // 切换到停止按钮
    document.getElementById("chatSendBtn").classList.add("hidden");
    document.getElementById("chatStopBtn").classList.remove("hidden");

    // 创建占位助手消息
    const assistantIdx = chat.messages.length;
    const assistantMsg = { role: "assistant", content: "", ts: new Date().toLocaleTimeString(), steps: [] };
    chat.messages.push(assistantMsg);

    const box = document.getElementById("chatMessages");
    // 渲染占位消息（带打字光标）
    box.innerHTML = chat.messages.map((m, i) =>
      i === assistantIdx
        ? '<div class="chat-msg msg-assistant">' +
          '<div class="msg-avatar">⚕</div>' +
          '<div class="msg-body"><div class="msg-content"><span class="typing-cursor"></span></div></div></div>'
        : renderMessageHtml(m, i)
    ).join("");
    box.scrollTop = box.scrollHeight;

    // 构造请求
    const activePrompt = localStorage.getItem(ACTIVE_PROMPT_KEY) || "";
    let taskText = userText;
    if (activePrompt) {
      taskText = "[系统指令] " + activePrompt + "\n\n[用户请求] " + userText;
    }
    if (chatState.webSearch) {
      taskText = "[联网搜索已启用] 请在回答时结合可用的搜索工具或知识库进行检索。\n\n" + taskText;
    }

    // 构建历史对话上下文（排除当前用户消息和占位助手消息，取最近10条）
    const historyMsgs = chat.messages
      .slice(0, -2)
      .filter(m => (m.role === "user" || m.role === "assistant") && m.content)
      .map(m => ({ role: m.role, content: m.content }))
      .slice(-10);

    // 使用 SSE 流式接口
    chatState.abortCtrl = new AbortController();
    let fullContent = "";
    const steps = [];

    try {
      const headers = { "Content-Type": "application/json" };
      const token = getToken();
      if (token) headers["Authorization"] = "Bearer " + token;

      const response = await fetch("/vault/agent/stream", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ task: taskText, max_iterations: 5, session_id: chat.id, history: historyMsgs }),
        signal: chatState.abortCtrl.signal,
      });

      if (!response.ok) {
        const errText = await response.text();
        throw new Error(errText || response.status + " " + response.statusText);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const jsonStr = line.slice(6).trim();
          if (!jsonStr) continue;
          try {
            const evt = JSON.parse(jsonStr);
            if (evt.type === "thought" || evt.type === "action" || evt.type === "observation" || evt.type === "planning" || evt.type === "reflection") {
              steps.push({
                step_type: evt.type,
                content: evt.data || evt.content || "",
                tool_name: evt.tool || evt.tool_name || null,
              });
              updateStreamingMessage(assistantIdx, fullContent, steps);
            } else if (evt.type === "answer" || evt.type === "token") {
              fullContent += evt.data || evt.content || evt.token || "";
              updateStreamingMessage(assistantIdx, fullContent, steps);
            } else if (evt.type === "error") {
              fullContent += "\n\n⚠ 错误：" + (evt.data || evt.content || evt.message || "未知错误");
              updateStreamingMessage(assistantIdx, fullContent, steps);
            } else if (evt.type === "status") {
              steps.push({ step_type: "status", content: evt.data || evt.content || "", tool_name: null });
              updateStreamingMessage(assistantIdx, fullContent, steps);
            } else if (evt.type === "done") {
              break;
            }
          } catch (e) { /* 跳过无法解析的行 */ }
        }
      }

      // 如果流式没有返回内容，回退到同步接口
      if (!fullContent) {
        const r = await api("/vault/agent", {
          method: "POST",
          body: { task: taskText, max_iterations: 5, session_id: chat.id, history: historyMsgs },
        });
        fullContent = r.answer || "（无返回内容）";
        if (r.steps) {
          r.steps.forEach((s) => steps.push({
            step_type: s.step_type,
            content: s.content,
            tool_name: s.tool_name,
          }));
        }
      }

      assistantMsg.content = fullContent || "⚠ AI 未返回任何内容，可能是 LLM 连接失败或模型不可用。请检查连接配置。";
      assistantMsg.steps = steps;
      saveChatSessions();
      renderChatMessages();

    } catch (e) {
      if (e.name === "AbortError") {
        // 用户手动停止或超时：如果有部分内容则保留，否则提示
        assistantMsg.content = fullContent || "⏸ 已停止生成";
      } else {
        assistantMsg.content = "⚠ 请求失败：" + e.message;
      }
      assistantMsg.steps = steps;
      saveChatSessions();
      renderChatMessages();
      toast("对话出错：" + e.message, "error");
    } finally {
      chatState.abortCtrl = null;
      const sendBtn = document.getElementById("chatSendBtn");
      const stopBtn = document.getElementById("chatStopBtn");
      if (sendBtn) sendBtn.classList.remove("hidden");
      if (stopBtn) stopBtn.classList.add("hidden");
      // 标签页标题更新：页面不在前台时闪烁提醒
      if (document.hidden) {
        const originalTitle = document.title;
        let isNotifying = true;
        const titleTimer = setInterval(function () {
          document.title = isNotifying ? "🔔 新消息 - DoctorAgent" : originalTitle;
          isNotifying = !isNotifying;
        }, 1000);
        const visibilityHandler = function () {
          if (!document.hidden) {
            clearInterval(titleTimer);
            document.title = originalTitle;
            document.removeEventListener("visibilitychange", visibilityHandler);
          }
        };
        document.addEventListener("visibilitychange", visibilityHandler);
      }
    }
  }

  function updateStreamingMessage(idx, content, steps) {
    const box = document.getElementById("chatMessages");
    const msgEls = box.querySelectorAll(".chat-msg");
    const target = msgEls[idx];
    if (!target) return;
    const contentEl = target.querySelector(".msg-content");
    if (contentEl) {
      contentEl.innerHTML = formatMessageContent(content) + '<span class="typing-cursor"></span>';
    }
    box.scrollTop = box.scrollHeight;
  }

  // ════════════════════ 设置中心 ════════════════════

  // 子标签切换
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".settings-tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".settings-panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById("stab-" + tab.dataset.stab).classList.add("active");
      if (tab.dataset.stab === "skills") loadSkills();
      if (tab.dataset.stab === "mcp") loadMcpTools();
      if (tab.dataset.stab === "advanced") loadAdvConfig();
    });
  });

  // ── 提示词管理 ──
  function loadPromptTemplates() {
    try { return JSON.parse(localStorage.getItem(PROMPT_STORE_KEY) || "[]"); }
    catch (e) { return []; }
  }
  function savePromptTemplates(list) {
    try { localStorage.setItem(PROMPT_STORE_KEY, JSON.stringify(list)); }
    catch (e) { console.error("savePromptTemplates failed", e); toast("提示词模板保存失败（存储空间可能已满）", "warn"); }
  }
  function refreshPromptSelect() {
    const list = loadPromptTemplates();
    const sel = document.getElementById("promptSelect");
    sel.innerHTML = '<option value="">选择模板…</option>' +
      list.map((p) => '<option value="' + escapeHtml(p.id) + '">' + escapeHtml(p.name) + "</option>").join("");
    const activeId = localStorage.getItem(ACTIVE_PROMPT_KEY);
    if (activeId) {
      const active = list.find((p) => p.id === activeId);
      if (active) {
        document.getElementById("promptActive").checked = true;
      }
    }
  }
  refreshPromptSelect();

  document.getElementById("promptNewBtn").addEventListener("click", () => {
    document.getElementById("promptName").value = "";
    document.getElementById("promptContent").value = "";
    document.getElementById("promptSelect").value = "";
    document.getElementById("promptStatus").textContent = "新建模板";
  });

  document.getElementById("promptLoadBtn").addEventListener("click", () => {
    const id = document.getElementById("promptSelect").value;
    if (!id) return;
    const list = loadPromptTemplates();
    const p = list.find((x) => x.id === id);
    if (p) {
      document.getElementById("promptName").value = p.name;
      document.getElementById("promptContent").value = p.content;
      document.getElementById("promptStatus").textContent = "已载入：" + p.name;
    }
  });

  document.getElementById("promptSaveBtn").addEventListener("click", () => {
    const name = document.getElementById("promptName").value.trim();
    const content = document.getElementById("promptContent").value.trim();
    if (!name || !content) { toast("名称和内容必填", "error"); return; }
    const list = loadPromptTemplates();
    const selectedId = document.getElementById("promptSelect").value;
    // 若选中了已有模板且名称一致，则更新；否则新建。
    let id = selectedId;
    let isUpdate = false;
    if (id) {
      const existing = list.find((p) => p.id === id);
      if (existing) {
        existing.name = name;
        existing.content = content;
        existing.updatedAt = new Date().toISOString();
        isUpdate = true;
      }
    }
    if (!isUpdate) {
      id = "prompt-" + Date.now();
      list.unshift({ id, name, content, createdAt: new Date().toISOString() });
      // 限制最多 20 个
      if (list.length > 20) list.length = 20;
    }
    savePromptTemplates(list);
    // 如果勾选了激活
    if (document.getElementById("promptActive").checked) {
      localStorage.setItem(ACTIVE_PROMPT_KEY, id);
    }
    refreshPromptSelect();
    document.getElementById("promptSelect").value = id;
    document.getElementById("promptStatus").innerHTML =
      '<span style="color:var(--success)">模板「' + escapeHtml(name) +
      "」" + (isUpdate ? "已更新" : "已保存") + "</span>";
    toast(isUpdate ? "提示词模板已更新" : "提示词模板已保存", "success");
  });

  document.getElementById("promptDeleteBtn").addEventListener("click", async () => {
    const id = document.getElementById("promptSelect").value;
    if (!id) { toast("请先选择要删除的模板", "error"); return; }
    const list = loadPromptTemplates();
    const p = list.find((x) => x.id === id);
    const label = p ? p.name : "该模板";
    const ok = await confirmDialog({
      title: "删除提示词模板",
      message: "确定要删除「" + label + "」吗？此操作不可撤销。",
      okText: "删除",
      icon: "🗑",
    });
    if (!ok) return;
    let newList = list.filter((x) => x.id !== id);
    savePromptTemplates(newList);
    if (localStorage.getItem(ACTIVE_PROMPT_KEY) === id) {
      localStorage.removeItem(ACTIVE_PROMPT_KEY);
      document.getElementById("promptActive").checked = false;
    }
    refreshPromptSelect();
    document.getElementById("promptName").value = "";
    document.getElementById("promptContent").value = "";
    document.getElementById("promptStatus").textContent = "模板已删除";
    toast("模板已删除", "success");
  });

  document.getElementById("promptActive").addEventListener("change", (e) => {
    if (e.target.checked) {
      const id = document.getElementById("promptSelect").value;
      if (id) {
        localStorage.setItem(ACTIVE_PROMPT_KEY, id);
        toast("已设为当前对话激活提示词", "success");
      } else {
        e.target.checked = false;
        toast("请先选择一个模板", "error");
      }
    } else {
      localStorage.removeItem(ACTIVE_PROMPT_KEY);
    }
  });

  // ── Skill 管理 ──
  const SKILL_STORE_KEY = "doctoragent_custom_skills";
  function loadCustomSkills() {
    try { return JSON.parse(localStorage.getItem(SKILL_STORE_KEY) || "[]"); }
    catch (e) { return []; }
  }
  function saveCustomSkills(list) {
    try { localStorage.setItem(SKILL_STORE_KEY, JSON.stringify(list)); }
    catch (e) { console.error("saveCustomSkills failed", e); toast("自定义技能保存失败（存储空间可能已满）", "warn"); }
  }
  async function loadSkills() {
    const out = document.getElementById("skillList");
    const countEl = document.getElementById("skillCount");
    out.innerHTML = '<div class="skeleton-list">' +
      Array.from({length: 4}).map(() =>
        '<div class="skeleton-item"><div class="skeleton-line w40"></div><div class="skeleton-line w80"></div><div class="skeleton-line w60"></div></div>'
      ).join("") + "</div>";
    try {
      const r = await api("/api/v1/agent/skills");
      const builtin = (r.skills || []).map((s) => ({ ...s, _source: "builtin" }));
      const custom = loadCustomSkills().map((s) => ({ ...s, _source: "custom" }));
      const skills = [...custom, ...builtin];
      countEl.textContent = skills.length + " 个（" + custom.length + " 自定义 / " + builtin.length + " 内置）";
      if (!skills.length) {
        out.innerHTML = '<div class="chat-empty"><div class="chat-empty-icon">🧩</div><p>暂无已注册 Skill</p><p class="hint">点击「+ 新建自定义 Skill」创建</p></div>';
        return;
      }
      out.innerHTML = skills.map((s) => {
        const name = s.name || s.skill_id || "";
        const desc = s.description || "";
        const tools = s.tools || s.required_tools || [];
        const triggers = s.triggers || [];
        const isCustom = s._source === "custom";
        const sourceBadge = isCustom
          ? '<span class="badge info">自定义</span>'
          : '<span class="badge">内置</span>';
        const cat = s.category ? '<span class="tag">' + escapeHtml(s.category) + "</span>" : "";
        const delBtn = isCustom
          ? '<button class="skill-del-btn" data-name="' + escapeHtml(name) + '" title="删除">✕</button>'
          : "";
        const trigHtml = triggers.length
          ? '<div class="finding-source">触发词：' +
            triggers.map((t) => '<span class="tag">' + escapeHtml(t) + "</span>").join(" ") + "</div>"
          : "";
        const toolsHtml = tools.length
          ? '<div class="finding-source">工具：' +
            tools.map((t) => '<span class="tag">' + escapeHtml(t) + "</span>").join(" ") + "</div>"
          : "";
        return '<div class="finding sev-info skill-card' + (isCustom ? " skill-custom" : "") + '">' +
          '<div class="finding-head"><span class="finding-rule">' + escapeHtml(name) + "</span>" +
          sourceBadge + cat + delBtn + "</div>" +
          '<div class="finding-text">' + escapeHtml(desc) + "</div>" +
          trigHtml + toolsHtml +
          "</div>";
      }).join("");
      out.querySelectorAll(".skill-del-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const name = btn.dataset.name;
          const ok = await confirmDialog({
            title: "删除自定义 Skill",
            message: "确定要删除自定义 Skill「" + name + "」吗？",
            okText: "删除",
            icon: "🗑",
          });
          if (!ok) return;
          let list = loadCustomSkills();
          list = list.filter((s) => s.name !== name);
          saveCustomSkills(list);
          loadSkills();
          toast("自定义 Skill 已删除", "success");
        });
      });
    } catch (e) {
      out.innerHTML = '<p class="placeholder" style="color:var(--danger)">加载失败：' +
        escapeHtml(e.message) + "</p>";
    }
  }
  document.getElementById("skillLoadBtn").addEventListener("click", loadSkills);
  document.getElementById("skillToggleCreateBtn").addEventListener("click", () => {
    const form = document.getElementById("skillCreateForm");
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) {
      document.getElementById("skillName").focus();
    }
  });
  document.getElementById("skillCancelCreateBtn").addEventListener("click", () => {
    document.getElementById("skillCreateForm").classList.add("hidden");
    ["skillName", "skillDesc", "skillTriggers"].forEach((id) =>
      document.getElementById(id).value = ""
    );
  });
  document.getElementById("skillCreateBtn").addEventListener("click", () => {
    const name = document.getElementById("skillName").value.trim();
    const desc = document.getElementById("skillDesc").value.trim();
    const category = document.getElementById("skillCategory").value;
    const triggers = document.getElementById("skillTriggers").value
      .split(",").map((s) => s.trim()).filter(Boolean);
    if (!name || !desc) { toast("名称和描述必填", "error"); return; }
    const list = loadCustomSkills();
    if (list.find((s) => s.name === name)) {
      toast("已存在同名 Skill", "error"); return;
    }
    list.unshift({
      name, description: desc, category, triggers,
      createdAt: new Date().toISOString(),
    });
    saveCustomSkills(list);
    ["skillName", "skillDesc", "skillTriggers"].forEach((id) =>
      document.getElementById(id).value = ""
    );
    document.getElementById("skillCreateForm").classList.add("hidden");
    toast("自定义 Skill 已保存", "success");
    loadSkills();
  });

  // ── MCP 工具 ──
  let mcpToolsCache = [];
  async function loadMcpTools() {
    const listEl = document.getElementById("mcpToolList");
    const countEl = document.getElementById("mcpCount");
    listEl.innerHTML = '<div class="skeleton-list">' +
      Array.from({length: 5}).map(() =>
        '<div class="skeleton-item"><div class="skeleton-line w50"></div><div class="skeleton-line w90"></div></div>'
      ).join("") + "</div>";
    try {
      const r = await api("/mcp/tools");
      mcpToolsCache = r.tools || [];
      countEl.textContent = mcpToolsCache.length + " 个";
      if (!mcpToolsCache.length) {
        listEl.innerHTML = '<div class="chat-empty"><div class="chat-empty-icon">🔧</div><p>暂无 MCP 工具</p><p class="hint">后端未注册工具</p></div>';
        return;
      }
      listEl.innerHTML = mcpToolsCache.map((t, i) => {
        const sideEffect = t.side_effect || "unknown";
        const sideClass = sideEffect === "write" ? "critical" : (sideEffect === "read" ? "info" : "");
        const icon = sideEffect === "write" ? "✏" : (sideEffect === "read" ? "📖" : "⚙");
        return '<div class="mcp-tool-item" data-idx="' + i + '" style="animation-delay:' + Math.min(i * 25, 250) + 'ms">' +
          '<div class="mcp-tool-icon">' + icon + "</div>" +
          '<div class="mcp-tool-info">' +
          '<div class="mcp-tool-name">' + escapeHtml(t.name || "") + "</div>" +
          '<div class="mcp-tool-desc">' + escapeHtml((t.description || "").slice(0, 70)) + "</div>" +
          "</div>" +
          '<span class="badge ' + sideClass + '">' + escapeHtml(sideEffect) + "</span>" +
          "</div>";
      }).join("");
      listEl.querySelectorAll(".mcp-tool-item").forEach((el) => {
        el.addEventListener("click", () => showMcpToolDetail(Number(el.dataset.idx)));
      });
    } catch (e) {
      listEl.innerHTML = '<p class="placeholder" style="color:var(--danger)">加载失败：' +
        escapeHtml(e.message) + "</p>";
    }
  }
  document.getElementById("mcpLoadBtn").addEventListener("click", loadMcpTools);

  // 把后端返回的 parameters 归一化为参数数组：
  // [{name, type, description, required, enum, default}, ...]
  // 兼容两种格式：数组形式（后端实际返回）与 JSON Schema 形式（{properties, required}）。
  function normalizeToolParams(raw) {
    if (Array.isArray(raw)) {
      return raw
        .filter((p) => p && typeof p === "object")
        .map((p) => ({
          name: p.name || p.key || "",
          type: p.type || "string",
          description: p.description || "",
          required: p.required === true,
          enum: Array.isArray(p.enum) ? p.enum : null,
          default: p.default,
        }));
    }
    if (raw && typeof raw === "object") {
      const props = raw.properties || {};
      const reqList = raw.required || [];
      return Object.keys(props).map((k) => ({
        name: k,
        type: props[k].type || "string",
        description: props[k].description || "",
        required: reqList.includes(k),
        enum: Array.isArray(props[k].enum) ? props[k].enum : null,
        default: props[k].default,
      }));
    }
    return [];
  }

  function showMcpToolDetail(idx) {
    const tool = mcpToolsCache[idx];
    if (!tool) return;
    const panel = document.getElementById("mcpTestPanel");
    const paramList = normalizeToolParams(tool.parameters);
    const sideEffect = tool.side_effect || "unknown";

    let html = '<div class="mcp-detail">' +
      '<div class="mcp-detail-head">' +
      "<h3>" + escapeHtml(tool.name || "") + "</h3>" +
      '<span class="badge ' + (sideEffect === "write" ? "critical" : "info") + '">' +
      escapeHtml(sideEffect) + "</span></div>" +
      "<p class=\"mcp-detail-desc\">" + escapeHtml(tool.description || "") + "</p>";

    // 参数表单
    if (paramList.length) {
      html += '<div class="section-label">参数（' + paramList.length + "）</div>";
      html += '<div class="mcp-params">';
      paramList.forEach((p) => {
        const k = p.name;
        const type = p.type || "string";
        const ph = p.description || (p.default != null ? String(p.default) : "");
        html += '<div class="form-row"><label>' + escapeHtml(k) +
          (p.required ? ' <span style="color:var(--danger)">*</span>' : "") +
          ' <span class="tag">' + escapeHtml(type) + "</span></label>";
        if (type === "boolean") {
          html += '<select class="mcp-param-input" data-key="' + escapeHtml(k) +
            '" data-type="boolean"><option value="false" selected>false</option><option value="true">true</option></select>';
        } else if (Array.isArray(p.enum) && p.enum.length) {
          html += '<select class="mcp-param-input" data-key="' + escapeHtml(k) +
            '" data-type="string"><option value="">（选择）</option>' +
            p.enum.map((v) => '<option value="' + escapeHtml(String(v)) + '">' +
              escapeHtml(String(v)) + "</option>").join("") + "</select>";
        } else {
          html += '<input type="text" class="mcp-param-input" data-key="' + escapeHtml(k) +
            '" data-type="' + escapeHtml(type) +
            '" placeholder="' + escapeHtml(ph) + '" />';
        }
        if (p.description) {
          html += '<div class="hint">' + escapeHtml(p.description) + "</div>";
        }
        html += "</div>";
      });
      html += "</div>";
    } else {
      html += '<p class="hint">此工具无需参数。</p>';
    }
    html += '<div class="form-actions"><button id="mcpCallBtn" class="btn btn-primary">调用工具</button></div>';
    html += '<div id="mcpCallResult" class="result-area compact"></div>';
    html += "</div>";
    panel.innerHTML = html;

    const typeOf = (key) => {
      const found = paramList.find((p) => p.name === key);
      return found ? found.type : "string";
    };
    document.getElementById("mcpCallBtn").addEventListener("click", async () => {
      const args = {};
      let missing = false;
      panel.querySelectorAll(".mcp-param-input").forEach((inp) => {
        const key = inp.dataset.key;
        const val = (inp.value || "").trim();
        const pType = inp.dataset.type || typeOf(key);
        if (!val) {
          if (paramList.find((p) => p.name === key && p.required)) missing = true;
          return;
        }
        if (pType === "integer" || pType === "number") args[key] = Number(val);
        else if (pType === "boolean") args[key] = val === "true";
        else if (pType === "array" || pType === "object") {
          try { args[key] = JSON.parse(val); } catch (e) { args[key] = val; }
        } else args[key] = val;
      });
      if (missing) { toast("请填写必填参数", "error"); return; }
      const resultEl = document.getElementById("mcpCallResult");
      resultEl.textContent = "调用中…";
      try {
        const r = await api("/mcp", {
          method: "POST",
          body: {
            jsonrpc: "2.0",
            method: "tools/call",
            params: { name: tool.name, arguments: args },
            id: 1,
          },
        });
        const result = r.result || r;
        const text = result.content?.[0]?.text || JSON.stringify(result, null, 2);
        resultEl.innerHTML = "<pre>" + escapeHtml(text) + "</pre>";
        toast("工具调用完成", "success");
      } catch (e) {
        resultEl.innerHTML = '<span style="color:var(--danger)">调用失败：' +
          escapeHtml(e.message) + "</span>";
        toast("调用失败：" + e.message, "error");
      }
    });
  }

  // ── 高级配置（复用配置管理逻辑）──
  const advEditor = document.getElementById("advConfigEditor");
  const advSaveBtn = document.getElementById("advConfigSaveBtn");
  const advStatus = document.getElementById("advConfigStatus");

  async function loadAdvConfig() {
    advEditor.value = "加载中…";
    advSaveBtn.disabled = true;
    try {
      const c = await api("/config");
      maskSecrets(c);
      advEditor.value = JSON.stringify(c, null, 2);
      advSaveBtn.disabled = false;
      advStatus.textContent = "已加载当前配置（敏感字段已脱敏）";
    } catch (e) {
      advEditor.value = "";
      advStatus.innerHTML = '<span style="color:var(--danger)">加载失败：' +
        escapeHtml(e.message) + "</span>";
    }
  }
  document.getElementById("advConfigLoadBtn").addEventListener("click", loadAdvConfig);
  document.getElementById("advConfigFormatBtn").addEventListener("click", () => {
    try {
      advEditor.value = JSON.stringify(JSON.parse(advEditor.value), null, 2);
      advStatus.textContent = "已格式化";
    } catch (e) {
      advStatus.innerHTML = '<span style="color:var(--danger)">JSON 解析失败</span>';
    }
  });
  advSaveBtn.addEventListener("click", async () => {
    let body;
    try { body = JSON.parse(advEditor.value); }
    catch (e) { advStatus.innerHTML = '<span style="color:var(--danger)">JSON 无效</span>'; return; }
    stripMasked(body);
    advStatus.textContent = "保存中…";
    try {
      const r = await api("/config", { method: "PUT", body: body });
      maskSecrets(r);
      advEditor.value = JSON.stringify(r, null, 2);
      advStatus.innerHTML = '<span style="color:var(--success)">配置已保存</span>';
      toast("配置已保存", "success");
    } catch (e) {
      advStatus.innerHTML = '<span style="color:var(--danger)">保存失败：' +
        escapeHtml(e.message) + "</span>";
    }
  });

  // ════════════════════ 高级功能：通用辅助 ════════════════════
  // 仅作用于以下五个高级 tab，不复用也不覆盖既有工具函数。
  function advSkeleton(n) {
    n = n || 3;
    return '<div class="skeleton-list">' +
      Array.from({ length: n }).map(function () {
        return '<div class="skeleton-item"><div class="skeleton-line w60"></div>' +
          '<div class="skeleton-line w80"></div></div>';
      }).join("") + "</div>";
  }
  function advBtnBusy(btn, busy, text) {
    if (!btn) return;
    if (busy) {
      btn._advOrig = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="btn-spinner"></span> ' + (text || "处理中…");
    } else {
      btn.disabled = false;
      if (btn._advOrig != null) { btn.innerHTML = btn._advOrig; delete btn._advOrig; }
    }
  }
  function advErrorRetry(out, msg, retryFn) {
    out.innerHTML = renderError(msg, retryFn);
    const box = out.querySelector(".error-box");
    if (box) box._retry = retryFn;
  }
  function advMarkdown(text) {
    if (text == null || text === "") return "";
    if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
      try { return DOMPurify.sanitize(marked.parse(text)); }
      catch (e) { return escapeHtml(text); }
    }
    return escapeHtml(text);
  }
  function advMetricGrid(cards) {
    let html = '<div class="metric-grid">';
    cards.forEach(function (c) {
      const target = (typeof c.target === "number" && !isNaN(c.target)) ? c.target : null;
      html += '<div class="metric-card' + (c.cls ? " " + c.cls : "") + '"' +
        (c.style ? ' style="' + escapeHtml(c.style) + '"' : "") + ">" +
        '<div class="metric-label">' + escapeHtml(c.label) + "</div>" +
        '<div class="metric-value"' + (target != null ? ' data-target="' + target + '"' : "") + ">" +
        escapeHtml(c.value == null ? "—" : String(c.value)) + "</div></div>";
    });
    html += "</div>";
    return html;
  }
  function advAnimateMetrics(container) {
    if (!container) return;
    container.querySelectorAll("[data-target]").forEach(function (el) {
      const t = parseFloat(el.getAttribute("data-target"));
      if (!isNaN(t)) countUp(el, t, 800);
    });
  }
  function advJsonTree(obj) {
    function render(val, key) {
      const keyHtml = (key != null)
        ? '<span style="color:var(--info)">' + escapeHtml(String(key)) + "</span>: "
        : "";
      if (val == null) {
        return '<div style="margin-left:14px">' + keyHtml +
          '<span style="color:var(--text-muted)">null</span></div>';
      }
      if (typeof val !== "object") {
        const color = typeof val === "string" ? "var(--success)"
          : (typeof val === "number" ? "var(--warning)" : "var(--info)");
        return '<div style="margin-left:14px">' + keyHtml +
          '<span style="color:' + color + '">' + escapeHtml(JSON.stringify(val)) + "</span></div>";
      }
      const isArr = Array.isArray(val);
      const entries = isArr ? val.map(function (v, i) { return [i, v]; })
        : Object.entries(val);
      const open = (key == null);
      let html = '<details' + (open ? " open" : "") + ' style="margin-left:14px">';
      html += '<summary style="cursor:pointer;color:var(--text-dim);font-size:12px">' + keyHtml +
        (isArr ? "[ ]" : "{ }") + ' <span style="color:var(--text-muted)">(' +
        entries.length + ")</span></summary>";
      entries.forEach(function (entry) {
        const k = isArr ? null : entry[0];
        html += render(entry[1], k);
      });
      html += "</details>";
      return html;
    }
    return '<div class="tot-tree">' + render(obj, null) + "</div>";
  }

  // ════════════════════ 知识图谱 ════════════════════
  function loadKgPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("kgBuildResult", "🏗️", "尚未构建", "点击「构建图谱」从 Vault 抽取实体与关系");
    ensure("kgQueryResult", "🔍", "尚未查询", "输入自然语言问题后点击「查询」");
    ensure("kgSubgraphResult", "🌐", "尚未获取子图", "输入实体名称后点击「获取子图」");
  }

  async function kgBuild() {
    const btn = document.getElementById("kgBuildBtn");
    const out = document.getElementById("kgBuildResult");
    const limitRaw = document.getElementById("kgLimitInput").value.trim();
    const body = {};
    if (limitRaw) {
      const n = parseInt(limitRaw, 10);
      if (!isNaN(n)) body.limit = n;
    }
    advBtnBusy(btn, true, "构建中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/kg/build", { method: "POST", body: body, timeoutMs: 180000 });
      renderKgBuild(r);
      toast("知识图谱构建完成", "success");
    } catch (e) {
      advErrorRetry(out, e.message, kgBuild);
      toast("构建失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderKgBuild(r) {
    const out = document.getElementById("kgBuildResult");
    const cards = [
      { label: "处理文档块", value: r.chunks_processed, target: r.chunks_processed },
      { label: "抽取实体", value: r.entities_extracted, target: r.entities_extracted },
      { label: "抽取关系", value: r.relations_extracted, target: r.relations_extracted },
      { label: "实体总数", value: r.total_entities, target: r.total_entities },
      { label: "关系总数", value: r.total_relations, target: r.total_relations },
    ].map(function (c) {
      return {
        label: c.label,
        value: c.value,
        target: (typeof c.value === "number" && !isNaN(c.value)) ? c.value : null,
      };
    });
    let html = advMetricGrid(cards);
    if (r.requested_limit != null) {
      html += '<p class="hint">请求限制：' + escapeHtml(String(r.requested_limit)) + " 篇文档</p>";
    }
    if (r.message) html += '<p class="hint">' + escapeHtml(r.message) + "</p>";
    out.innerHTML = html;
    advAnimateMetrics(out);
  }

  async function kgQuery() {
    const btn = document.getElementById("kgQueryBtn");
    const out = document.getElementById("kgQueryResult");
    const query = document.getElementById("kgQueryInput").value.trim();
    if (!query) { toast("请输入查询", "error"); return; }
    advBtnBusy(btn, true, "查询中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/kg/query", { method: "POST", body: { query: query, top_k: 5 } });
      renderKgQuery(r);
    } catch (e) {
      advErrorRetry(out, e.message, kgQuery);
      toast("查询失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderKgQuery(r) {
    const out = document.getElementById("kgQueryResult");
    const results = Array.isArray(r.results) ? r.results : [];
    if (!results.length) {
      out.innerHTML = emptyState("🔍", "无匹配结果", "未检索到相关实体或文档");
      return;
    }
    let html = '<div class="section-label">共 ' + results.length + " 条结果</div>";
    results.forEach(function (item, i) {
      const score = typeof item.score === "number" ? item.score : 0;
      const pct = Math.max(0, Math.min(100, Math.round(score * 100)));
      const matched = Array.isArray(item.matched_entities) ? item.matched_entities : [];
      const title = item.chunk_id || item.vault_path || "chunk";
      html += '<div class="kg-result-card" style="animation-delay:' + (i * 50) + 'ms">';
      html += '<div class="finding-head"><span class="finding-rule">' +
        escapeHtml(title) + "</span>";
      html += '<span class="badge info">score ' + score.toFixed(3) + "</span></div>";
      if (item.text) {
        const snippet = item.text.length > 320 ? item.text.slice(0, 320) + "…" : item.text;
        html += '<div class="finding-text">' + escapeHtml(snippet) + "</div>";
      }
      html += '<div class="kg-score-bar"><div class="kg-score-fill" style="width:' +
        pct + '%"></div></div>';
      if (matched.length) {
        html += '<div class="finding-source">命中实体：' +
          matched.map(function (m) { return '<span class="tag">' + escapeHtml(m) + "</span>"; }).join(" ") +
          "</div>";
      }
      html += "</div>";
    });
    out.innerHTML = html;
  }

  async function kgSubgraph() {
    const btn = document.getElementById("kgSubgraphBtn");
    const out = document.getElementById("kgSubgraphResult");
    const name = document.getElementById("kgEntityInput").value.trim();
    if (!name) { toast("请输入实体名称", "error"); return; }
    const depthRaw = document.getElementById("kgDepthInput").value.trim();
    const depth = depthRaw ? parseInt(depthRaw, 10) : 2;
    advBtnBusy(btn, true, "获取中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/kg/subgraph", {
        method: "POST",
        body: { entity_name: name, depth: isNaN(depth) ? 2 : depth },
      });
      renderKgSubgraph(r);
    } catch (e) {
      advErrorRetry(out, e.message, kgSubgraph);
      toast("获取子图失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderKgSubgraph(data) {
    const out = document.getElementById("kgSubgraphResult");
    const entities = Array.isArray(data.entities) ? data.entities : [];
    const relations = Array.isArray(data.relations) ? data.relations : [];
    if (!entities.length) {
      out.innerHTML = emptyState("🌐", "子图为空", "该实体没有关联节点");
      return;
    }
    const palette = ["#60a5fa", "#34d399", "#fbbf24", "#f87171", "#a78bfa",
      "#22d3ee", "#fb923c", "#e879f9", "#4ade80", "#facc15"];
    const types = [];
    entities.forEach(function (e) {
      const t = e.entity_type || "concept";
      if (!types.includes(t)) types.push(t);
    });
    const typeColor = {};
    types.forEach(function (t, i) { typeColor[t] = palette[i % palette.length]; });

    const n = entities.length;
    const W = 640, H = 440, cx = W / 2, cy = H / 2;
    const R = n <= 1 ? 0 : Math.min(W, H) / 2 - 80;
    const pos = {};
    entities.forEach(function (e, i) {
      const name = e.name || e.id || ("entity-" + i);
      if (n <= 1) { pos[name] = { x: cx, y: cy }; }
      else {
        const angle = (i / n) * 2 * Math.PI - Math.PI / 2;
        pos[name] = { x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle) };
      }
    });

    let svg = '<svg viewBox="0 0 ' + W + " " + H +
      '" class="kg-graph-svg" xmlns="http://www.w3.org/2000/svg">';
    svg += '<defs><marker id="kg-arrow" markerWidth="10" markerHeight="10" refX="9" ' +
      'refY="3" orient="auto" markerUnits="strokeWidth">' +
      '<path d="M0,0 L9,3 L0,6 Z" fill="var(--border-strong)"/></marker></defs>';

    let edgesHtml = '<g class="kg-edges">';
    relations.forEach(function (rel) {
      const s = rel.source || rel.from || rel.subject;
      const t = rel.target || rel.to || rel.object;
      const sp = pos[s], tp = pos[t];
      if (!sp || !tp) return;
      const rt = rel.relation_type || rel.type || rel.relation || "";
      const mx = (sp.x + tp.x) / 2, my = (sp.y + tp.y) / 2;
      edgesHtml += '<line class="kg-edge" data-source="' + escapeHtml(s) +
        '" data-target="' + escapeHtml(t) + '" x1="' + sp.x.toFixed(1) + '" y1="' +
        sp.y.toFixed(1) + '" x2="' + tp.x.toFixed(1) + '" y2="' + tp.y.toFixed(1) + '"/>';
      if (rt) {
        edgesHtml += '<text class="kg-edge-label" x="' + mx.toFixed(1) + '" y="' +
          (my - 4).toFixed(1) + '" text-anchor="middle">' + escapeHtml(rt) + "</text>";
      }
    });
    edgesHtml += "</g>";

    let nodesHtml = '<g class="kg-nodes">';
    entities.forEach(function (e) {
      const name = e.name || e.id || "";
      const p = pos[name];
      if (!p) return;
      const color = typeColor[e.entity_type || "concept"];
      const label = name.length > 18 ? name.slice(0, 16) + "…" : name;
      nodesHtml += '<g class="kg-node" data-name="' + escapeHtml(name) +
        '" transform="translate(' + p.x.toFixed(1) + "," + p.y.toFixed(1) + ')">';
      nodesHtml += '<circle r="22" fill="' + color +
        '" fill-opacity="0.85" stroke="' + color + '" stroke-width="2"/>';
      nodesHtml += '<text class="kg-node-label" y="40" text-anchor="middle">' +
        escapeHtml(label) + "</text>";
      if (e.entity_type) {
        nodesHtml += '<text class="kg-node-label" y="54" text-anchor="middle" ' +
          'style="font-size:9px;opacity:0.7">' + escapeHtml(e.entity_type) + "</text>";
      }
      nodesHtml += "<title>" + escapeHtml(name +
        (e.entity_type ? " (" + e.entity_type + ")" : "")) + "</title>";
      nodesHtml += "</g>";
    });
    nodesHtml += "</g>";
    svg += edgesHtml + nodesHtml + "</svg>";

    let legend = '<div class="kg-legend">';
    types.forEach(function (t) {
      legend += '<span class="kg-legend-item"><span class="kg-legend-dot" style="background:' +
        typeColor[t] + '"></span>' + escapeHtml(t) + "</span>";
    });
    legend += "</div>";

    const seedName = data.seed || (entities[0] && entities[0].name) || "";
    const info = '<div class="section-label">种子实体：' + escapeHtml(seedName) + " · " +
      entities.length + " 节点 · " + relations.length + " 关系</div>";

    out.innerHTML = info + '<div class="kg-graph-wrap">' + svg + "</div>" + legend;

    // 节点 hover 高亮：放大节点、突出关联边、淡化无关节点
    out.querySelectorAll(".kg-node").forEach(function (g) {
      g.addEventListener("mouseenter", function () {
        const nm = g.getAttribute("data-name");
        g.style.filter = "brightness(1.3) drop-shadow(0 2px 6px rgba(96,165,250,0.6))";
        out.querySelectorAll(".kg-edge").forEach(function (edge) {
          const hit = edge.getAttribute("data-source") === nm ||
            edge.getAttribute("data-target") === nm;
          edge.style.opacity = hit ? "1" : "0.15";
          edge.style.strokeWidth = hit ? "2.5" : "1.5";
        });
        out.querySelectorAll(".kg-node").forEach(function (node) {
          if (node !== g) node.style.opacity = "0.4";
        });
      });
      g.addEventListener("mouseleave", function () {
        g.style.filter = "";
        out.querySelectorAll(".kg-edge").forEach(function (edge) {
          edge.style.opacity = "";
          edge.style.strokeWidth = "";
        });
        out.querySelectorAll(".kg-node").forEach(function (node) {
          node.style.opacity = "";
        });
      });
    });
  }

  document.getElementById("kgBuildBtn").addEventListener("click", kgBuild);
  document.getElementById("kgQueryBtn").addEventListener("click", kgQuery);
  document.getElementById("kgQueryInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); kgQuery(); }
  });
  document.getElementById("kgSubgraphBtn").addEventListener("click", kgSubgraph);
  document.getElementById("kgEntityInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); kgSubgraph(); }
  });

  // ════════════════════ DAG 工作流 ════════════════════
  function loadDagPage() {
    // 进入 tab 时自动刷新一次调度器状态
    dagSchedulerStatus();
  }

  async function dagSchedulerStatus() {
    const btn = document.getElementById("dagSchedulerBtn");
    const out = document.getElementById("dagSchedulerResult");
    setPill("dagSchedulerStatus", "查询中…", "info");
    if (btn) advBtnBusy(btn, true, "查询中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/scheduler/status");
      renderDagScheduler(r);
    } catch (e) {
      setPill("dagSchedulerStatus", "离线", "fail");
      advErrorRetry(out, e.message, dagSchedulerStatus);
    } finally {
      if (btn) advBtnBusy(btn, false);
    }
  }
  function renderDagScheduler(r) {
    const out = document.getElementById("dagSchedulerResult");
    const q = r.queue || {};
    const m = r.metrics || {};
    const running = q.running || m.running || 0;
    setPill("dagSchedulerStatus", running ? "运行中" : "空闲", running ? "warn" : "ok");
    let html = '<div class="section-label">队列状态</div><div class="kv-grid">';
    html += kvCell("排队中", q.queued != null ? q.queued : "—");
    html += kvCell("运行中", q.running != null ? q.running : "—");
    html += kvCell("已知任务", q.total_known != null ? q.total_known : "—");
    html += kvCell("最大并发", q.max_concurrent != null ? q.max_concurrent : "—");
    html += kvCell("队列上限", q.max_queue_size != null ? q.max_queue_size : "—");
    html += "</div>";
    html += '<div class="section-label">聚合指标</div><div class="kv-grid">';
    html += kvCell("已启动", m.started != null ? m.started : "—");
    html += kvCell("已完成", m.completed != null ? m.completed : "—");
    html += kvCell("失败", m.failed != null ? m.failed : "—");
    html += kvCell("已取消", m.cancelled != null ? m.cancelled : "—");
    html += kvCell("平均等待(秒)", m.avg_wait_seconds != null ? m.avg_wait_seconds : "—");
    html += kvCell("吞吐(/秒)", m.throughput_per_second != null ? m.throughput_per_second : "—");
    html += "</div>";
    if (m.started_at) {
      html += '<p class="hint">调度器启动：' + escapeHtml(relativeTime(m.started_at)) + "</p>";
    }
    out.innerHTML = html;
  }

  async function dagExecute() {
    const btn = document.getElementById("dagExecuteBtn");
    const editor = document.getElementById("dagTasksEditor");
    const out = document.getElementById("dagStatusResult");
    let tasks;
    try { tasks = JSON.parse(editor.value); }
    catch (e) { toast("任务定义 JSON 解析失败：" + e.message, "error"); return; }
    if (!Array.isArray(tasks) || !tasks.length) {
      toast("请提供非空任务数组", "error");
      return;
    }
    advBtnBusy(btn, true, "执行中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/dag/execute", {
        method: "POST",
        body: { tasks: tasks },
        timeoutMs: 120000,
      });
      const dagIdInput = document.getElementById("dagIdInput");
      if (dagIdInput && r.dag_id) dagIdInput.value = r.dag_id;
      renderDagStatus({ dag_id: r.dag_id, found: true, status: r.status });
      toast("DAG 已提交执行（" + r.dag_id + "）", "success");
    } catch (e) {
      advErrorRetry(out, e.message, dagExecute);
      toast("执行失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }

  async function dagStatusQuery() {
    const btn = document.getElementById("dagStatusBtn");
    const out = document.getElementById("dagStatusResult");
    const dagId = document.getElementById("dagIdInput").value.trim();
    if (!dagId) { toast("请输入或先执行获取 DAG ID", "error"); return; }
    advBtnBusy(btn, true, "查询中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/dag/status/" + encodeURIComponent(dagId));
      renderDagStatus(r);
    } catch (e) {
      advErrorRetry(out, e.message, dagStatusQuery);
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function dagStatusClass(s) {
    s = String(s || "").toLowerCase();
    if (s === "success" || s === "completed" || s === "succeeded") return "completed";
    if (s === "failed" || s === "error") return "failed";
    if (s === "running" || s === "in_progress") return "running";
    return "pending";
  }
  function dagDotColor(s) {
    const c = dagStatusClass(s);
    if (c === "completed") return "var(--success)";
    if (c === "failed") return "var(--danger)";
    if (c === "running") return "var(--info)";
    return "var(--text-muted)";
  }
  function renderDagStatus(r) {
    const out = document.getElementById("dagStatusResult");
    const status = r.status || {};
    if (r.found === false) {
      out.innerHTML = emptyState("❓", "未找到 DAG", "DAG ID 无效或已过期");
      return;
    }
    const overall = status.overall || "unknown";
    const tasks = status.tasks || {};
    const counts = status.status_counts || {};
    const taskIds = Object.keys(tasks);
    let html = '<div class="kv-grid">';
    html += kvCell("DAG ID", r.dag_id || "—");
    html += kvCell("总体状态", overall);
    html += kvCell("任务总数", status.total_tasks != null ? status.total_tasks : taskIds.length);
    html += kvCell("已取消", status.cancelled ? "是" : "否");
    if (status.started_at) html += kvCell("开始时间", relativeTime(status.started_at));
    if (status.completed_at) html += kvCell("完成时间", relativeTime(status.completed_at));
    html += "</div>";

    const countKeys = Object.keys(counts);
    if (countKeys.length) {
      html += '<div class="section-label">状态分布</div>';
      html += '<div style="display:flex;flex-wrap:wrap;gap:8px">';
      countKeys.forEach(function (k) {
        html += '<span class="dag-task-status ' + dagStatusClass(k) + '">' +
          escapeHtml(k) + " · " + counts[k] + "</span>";
      });
      html += "</div>";
    }

    if (taskIds.length) {
      html += '<div class="section-label">任务时间线</div><div class="timeline">';
      taskIds.forEach(function (tid) {
        const st = tasks[tid] || "pending";
        html += '<div class="timeline-item">';
        html += '<span class="timeline-dot" style="background:' + dagDotColor(st) + '"></span>';
        html += '<div><div class="timeline-title mono">' + escapeHtml(tid) +
          ' <span class="dag-task-status ' + dagStatusClass(st) + '">' +
          escapeHtml(st) + "</span></div></div></div>";
      });
      html += "</div>";
    } else {
      html += emptyState("📭", "无任务", "该 DAG 没有任务节点");
    }
    out.innerHTML = html;
  }

  document.getElementById("dagSchedulerBtn").addEventListener("click", dagSchedulerStatus);
  document.getElementById("dagExecuteBtn").addEventListener("click", dagExecute);
  document.getElementById("dagFormatBtn").addEventListener("click", function () {
    const editor = document.getElementById("dagTasksEditor");
    try {
      editor.value = JSON.stringify(JSON.parse(editor.value), null, 2);
      toast("已格式化", "info");
    } catch (e) {
      toast("JSON 解析失败：" + e.message, "error");
    }
  });
  document.getElementById("dagStatusBtn").addEventListener("click", dagStatusQuery);
  document.getElementById("dagIdInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); dagStatusQuery(); }
  });

  // ════════════════════ 评估中心 ════════════════════
  function loadEvalPage() {
    const out = document.getElementById("evalResult");
    if (out && !out.innerHTML.trim()) {
      out.innerHTML = '<p class="placeholder">填写输入与输出后点击「运行评估」。</p>';
    }
  }

  async function runEval() {
    const btn = document.getElementById("evalRunBtn");
    const out = document.getElementById("evalResult");
    const input = document.getElementById("evalInput").value.trim();
    const actual = document.getElementById("evalActual").value.trim();
    if (!input || !actual) { toast("请填写 input 与 actual_output", "error"); return; }
    const body = {
      input: input,
      actual_output: actual,
      expected_output: document.getElementById("evalExpected").value.trim() || undefined,
      threshold: parseNum(document.getElementById("evalThreshold").value, 0.5),
    };
    const judgeModel = document.getElementById("evalJudgeModel").value.trim();
    if (judgeModel) body.judge_model = judgeModel;
    const apiKey = document.getElementById("evalApiKey").value.trim();
    if (apiKey) body.api_key = apiKey;
    advBtnBusy(btn, true, "评估中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/evaluate", { method: "POST", body: body, timeoutMs: 120000 });
      renderEvalReport(r);
      toast("评估完成", "success");
    } catch (e) {
      advErrorRetry(out, e.message, runEval);
      toast("评估失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderEvalReport(r) {
    const out = document.getElementById("evalResult");
    const threshold = parseNum(document.getElementById("evalThreshold").value, 0.5);
    const metrics = Array.isArray(r.metrics) ? r.metrics : [];
    const summary = r.summary || {};
    let html = "";

    if (summary && summary.total != null) {
      const passed = (summary.failed || 0) === 0 && (summary.passed || 0) > 0;
      const avg = summary.average_score != null ? Number(summary.average_score) : null;
      html += '<div class="verify-banner ' + (passed ? "ok" : "fail") + '">';
      html += '<span class="verify-icon">' + (passed ? "✅" : "⚠") + "</span>";
      html += '<div><div class="verify-title">评估' + (passed ? "通过" : "未通过") +
        (r.engine ? " · " + escapeHtml(r.engine) : "") + "</div>";
      html += '<div class="verify-sub">通过 ' + (summary.passed || 0) + "/" +
        (summary.total || 0) + " 项" +
        (avg != null ? " · 平均分 " + avg.toFixed(3) : "") + " · 阈值 " + threshold +
        "</div></div></div>";
    }

    if (!metrics.length) {
      html += emptyState("📊", "无评估指标", "后端未返回任何指标数据");
      out.innerHTML = html;
      return;
    }
    html += '<div class="metric-grid">';
    metrics.forEach(function (m) {
      const score = typeof m.score === "number" ? m.score : parseFloat(m.score);
      const passed = m.passed === true ||
        (typeof score === "number" && !isNaN(score) && score >= threshold);
      const cls = passed ? "pass" : "fail";
      html += '<div class="eval-metric-card ' + cls + '">';
      html += '<div class="metric-label">' +
        escapeHtml(m.metric_name || m.name || "metric") + "</div>";
      if (typeof score === "number" && !isNaN(score)) {
        html += '<div class="eval-metric-score" data-target="' + score + '">' +
          score.toFixed(2) + "</div>";
      } else {
        html += '<div class="eval-metric-score">—</div>';
      }
      html += '<div class="hint">阈值 ' +
        escapeHtml(String(m.threshold != null ? m.threshold : threshold)) + "</div>";
      if (m.reason) {
        html += '<div class="finding-text" style="margin-top:6px;font-size:12px">' +
          escapeHtml(m.reason) + "</div>";
      }
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
    advAnimateMetrics(out);
  }

  document.getElementById("evalRunBtn").addEventListener("click", runEval);

  // ════════════════════ 自进化 / ToT ════════════════════
  function loadEvoPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("evoResult", "🧬", "尚未触发自进化", "分析 Agent 轨迹以提取经验教训");
    ensure("evoTrajectoryResult", "🛰️", "尚未查看轨迹", "输入 task_id 后点击「查看轨迹」");
    ensure("totResult", "🌳", "尚未运行 ToT", "输入复杂问题后点击「运行 ToT」");
  }

  async function evoRun() {
    const btn = document.getElementById("evoRunBtn");
    const out = document.getElementById("evoResult");
    const idsRaw = document.getElementById("evoTaskIdsInput").value.trim();
    const body = {};
    if (idsRaw) {
      const ids = idsRaw.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      if (ids.length) body.task_ids = ids;
    }
    advBtnBusy(btn, true, "进化中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/agent/evolve", { method: "POST", body: body, timeoutMs: 180000 });
      renderEvoResult(r);
      toast("自进化完成", "success");
    } catch (e) {
      setPill("evoStatus", "失败", "fail");
      advErrorRetry(out, e.message, evoRun);
      toast("自进化失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderEvoResult(r) {
    const out = document.getElementById("evoResult");
    setPill("evoStatus", "已完成", "ok");
    const lessons = Array.isArray(r.lessons) ? r.lessons : [];
    const cards = [
      { label: "分析轨迹数", value: r.analyzed, target: r.analyzed },
      { label: "经验存储数", value: r.experiences_stored, target: r.experiences_stored },
      { label: "提炼课程数", value: lessons.length, target: lessons.length },
    ].map(function (c) {
      return {
        label: c.label,
        value: c.value,
        target: (typeof c.value === "number" && !isNaN(c.value)) ? c.value : null,
      };
    });
    let html = advMetricGrid(cards);

    if (r.optimized_prompt) {
      html += '<div class="section-label">优化后 Prompt</div>';
      html += '<div class="evo-prompt-block">' + escapeHtml(r.optimized_prompt) + "</div>";
    }

    html += '<div class="section-label">经验课程（' + lessons.length + " 条）</div>";
    if (lessons.length) {
      lessons.forEach(function (l, i) {
        const text = typeof l === "string" ? l
          : (l.lesson || l.text || l.content || JSON.stringify(l));
        const tag = (typeof l === "object" && l && l.category)
          ? '<span class="tag">' + escapeHtml(l.category) + "</span>" : "";
        html += '<div class="evo-lesson-card" style="animation-delay:' + (i * 50) + 'ms">';
        html += '<div class="finding-text">' + escapeHtml(text) + "</div>";
        if (tag) html += '<div class="finding-source" style="margin-top:6px">' + tag + "</div>";
        html += "</div>";
      });
    } else {
      html += emptyState("📝", "未提炼出课程", "尝试提供更多轨迹");
    }
    if (r.message) html += '<p class="hint">' + escapeHtml(r.message) + "</p>";
    out.innerHTML = html;
    advAnimateMetrics(out);
  }

  async function evoTrajectory() {
    const btn = document.getElementById("evoTrajectoryBtn");
    const out = document.getElementById("evoTrajectoryResult");
    const taskId = document.getElementById("evoTrajectoryInput").value.trim();
    if (!taskId) { toast("请输入 task_id", "error"); return; }
    advBtnBusy(btn, true, "加载中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/agent/trajectory/" + encodeURIComponent(taskId));
      renderTrajectory(r);
    } catch (e) {
      advErrorRetry(out, e.message, evoTrajectory);
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function trajStepClass(t) {
    t = String(t || "").toLowerCase();
    if (t.indexOf("thought") >= 0 || t.indexOf("reason") >= 0) return "thought";
    if (t.indexOf("action") >= 0 || t.indexOf("tool") >= 0) return "action";
    if (t.indexOf("observation") >= 0 || t.indexOf("result") >= 0) return "observation";
    return "thought";
  }
  function trajDotColor(t) {
    const c = trajStepClass(t);
    if (c === "action") return "var(--success)";
    if (c === "observation") return "var(--warning)";
    return "var(--info)";
  }
  function renderTrajectory(r) {
    const out = document.getElementById("evoTrajectoryResult");
    if (r.found === false) {
      out.innerHTML = emptyState("❓", "未找到轨迹", "该 task_id 不存在");
      return;
    }
    const steps = Array.isArray(r.steps) ? r.steps : [];
    let html = '<div class="kv-grid">';
    html += kvCell("Task ID", r.task_id || "—");
    html += kvCell("工具调用数", r.total_tool_calls != null ? r.total_tool_calls : "—");
    html += kvCell("耗时", r.total_time_ms != null ? (r.total_time_ms + " ms") : "—");
    html += "</div>";
    if (!steps.length) {
      html += emptyState("👣", "无轨迹步骤", "该任务没有记录步骤");
      out.innerHTML = html;
      return;
    }
    html += '<div class="section-label">执行轨迹（' + steps.length + " 步）</div>";
    html += '<div class="timeline">';
    steps.forEach(function (s, i) {
      const stype = (s.step_type || "").toString();
      const cls = trajStepClass(stype);
      html += '<div class="timeline-item">';
      html += '<span class="timeline-dot" style="background:' + trajDotColor(stype) + '"></span>';
      html += '<div><div class="timeline-title">#' + (i + 1) + ' <span class="traj-step-type ' +
        cls + '">' + escapeHtml(stype || "step") + "</span>";
      if (s.tool_name) {
        html += ' <span class="traj-step-tool">' + escapeHtml(s.tool_name) + "</span>";
      }
      html += "</div>";
      if (s.content) {
        html += '<div class="traj-step-content">' + escapeHtml(s.content) + "</div>";
      }
      html += "</div></div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }

  async function totRun() {
    const btn = document.getElementById("totRunBtn");
    const out = document.getElementById("totResult");
    const query = document.getElementById("totQueryInput").value.trim();
    if (!query) { toast("请输入问题", "error"); return; }
    const maxDepth = parseInt(document.getElementById("totDepthInput").value, 10) || 3;
    const branch = parseInt(document.getElementById("totBranchInput").value, 10) || 3;
    advBtnBusy(btn, true, "推理中…");
    out.innerHTML = advSkeleton(3);
    try {
      const params = new URLSearchParams();
      params.set("query", query);
      params.set("max_depth", String(maxDepth));
      params.set("branching_factor", String(branch));
      const r = await api("/api/v1/agent/tot?" + params.toString(), { timeoutMs: 180000 });
      renderTotResult(r);
      toast("ToT 推理完成", "success");
    } catch (e) {
      advErrorRetry(out, e.message, totRun);
      toast("ToT 推理失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderTotResult(r) {
    const out = document.getElementById("totResult");
    let html = "";
    if (r.answer) {
      html += '<div class="section-label">最终答案</div>';
      html += '<div class="tot-answer">' + advMarkdown(r.answer) + "</div>";
    }
    const path = Array.isArray(r.best_path) ? r.best_path : [];
    if (path.length) {
      html += '<div class="section-label">最佳推理路径（' + path.length + " 步）</div>";
      path.forEach(function (node, i) {
        const thought = node.thought || node.content || node.idea || "";
        const score = node.evaluation_score != null ? node.evaluation_score : node.score;
        const depth = node.depth != null ? node.depth : i;
        html += '<div class="tot-step" style="animation-delay:' + (i * 60) + 'ms">';
        html += '<div class="finding-head"><span class="finding-rule">深度 ' +
          escapeHtml(String(depth)) + "</span>";
        if (typeof score === "number") {
          html += '<span class="badge info">score ' + Number(score).toFixed(3) + "</span>";
        }
        html += "</div>";
        html += '<div class="finding-text">' + escapeHtml(thought) + "</div>";
        html += "</div>";
      });
    }
    if (r.tree) {
      html += '<div class="section-label">完整搜索树</div>';
      html += advJsonTree(r.tree);
    }
    if (!r.answer && !path.length && !r.tree) {
      html += emptyState("🌳", "ToT 未返回结果", "尝试调整问题或参数");
    }
    out.innerHTML = html;
  }

  document.getElementById("evoRunBtn").addEventListener("click", evoRun);
  document.getElementById("evoTrajectoryBtn").addEventListener("click", evoTrajectory);
  document.getElementById("evoTrajectoryInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); evoTrajectory(); }
  });
  document.getElementById("totRunBtn").addEventListener("click", totRun);

  // ════════════════════ 高级 RAG ════════════════════
  function loadRagPage() {
    ragCacheStats();
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("ragRouteResult", "🧭", "尚未路由", "输入查询后点击「路由分析」");
    ensure("ragAgenticResult", "🤖", "尚未运行", "输入查询后点击「运行 Agentic RAG」");
    ensure("ragCorrectResult", "🔧", "尚未运行", "输入查询后点击「运行 Corrective RAG」");
  }

  async function ragRoute() {
    const btn = document.getElementById("ragRouteBtn");
    const out = document.getElementById("ragRouteResult");
    const query = document.getElementById("ragRouteInput").value.trim();
    if (!query) { toast("请输入查询", "error"); return; }
    advBtnBusy(btn, true, "路由中…");
    out.innerHTML = advSkeleton(2);
    try {
      const r = await api("/api/v1/rag/route", { method: "POST", body: { query: query } });
      renderRagRoute(r);
    } catch (e) {
      setPill("ragRouteTag", "失败", "fail");
      advErrorRetry(out, e.message, ragRoute);
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderRagRoute(r) {
    const out = document.getElementById("ragRouteResult");
    setPill("ragRouteTag", r.strategy || r.query_type || "已路由", "info");
    const cfg = r.retrieval_config || {};
    let html = '<div class="kv-grid">';
    html += kvCell("查询", r.query || "—");
    html += kvCell("查询类型", r.query_type || "—");
    html += kvCell("检索策略", r.strategy || "—");
    html += "</div>";
    const cfgKeys = Object.keys(cfg);
    if (cfgKeys.length) {
      html += '<div class="section-label">检索配置</div><div class="kv-grid">';
      cfgKeys.forEach(function (k) { html += kvCell(k, cfg[k]); });
      html += "</div>";
    }
    out.innerHTML = html;
  }

  async function ragAgentic() {
    const btn = document.getElementById("ragAgenticBtn");
    const out = document.getElementById("ragAgenticResult");
    const query = document.getElementById("ragAgenticQuery").value.trim();
    if (!query) { toast("请输入查询", "error"); return; }
    const iter = parseInt(document.getElementById("ragAgenticIter").value, 10) || 5;
    advBtnBusy(btn, true, "检索中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/rag/agentic", {
        method: "POST",
        body: { query: query, max_iterations: iter, top_k: 5 },
        timeoutMs: 180000,
      });
      renderRagAgentic(r);
      toast("Agentic RAG 完成", "success");
    } catch (e) {
      advErrorRetry(out, e.message, ragAgentic);
      toast("Agentic RAG 失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function ragDocCardHtml(d, i) {
    const title = d.title || d.vault_path || d.chunk_id || d.id || ("doc-" + (i + 1));
    const snippet = d.text || d.content || d.snippet || "";
    const score = d.score != null ? d.score : d.relevance_score;
    let html = '<div class="rag-doc-card" style="animation-delay:' + (i * 40) + 'ms">';
    html += '<div class="rag-doc-title">' + escapeHtml(title);
    if (typeof score === "number") {
      html += ' <span class="badge info">score ' + Number(score).toFixed(3) + "</span>";
    }
    if (d.relevant === true) html += ' <span class="status-pill ok">相关</span>';
    else if (d.relevant === false) html += ' <span class="status-pill fail">无关</span>';
    html += "</div>";
    if (snippet) {
      const snip = snippet.length > 280 ? snippet.slice(0, 280) + "…" : snippet;
      html += '<div class="rag-doc-snippet">' + escapeHtml(snip) + "</div>";
    }
    html += "</div>";
    return html;
  }
  function renderRagAgentic(r) {
    const out = document.getElementById("ragAgenticResult");
    let html = '<div class="kv-grid">';
    html += kvCell("迭代次数", r.iterations != null ? r.iterations : "—");
    html += kvCell("检索文档数", Array.isArray(r.documents) ? r.documents.length : 0);
    html += kvCell("行动数", Array.isArray(r.action_history) ? r.action_history.length : 0);
    html += "</div>";
    if (r.answer) {
      html += '<div class="section-label">最终答案</div>';
      html += '<div class="tot-answer">' + advMarkdown(r.answer) + "</div>";
    }
    const docs = Array.isArray(r.documents) ? r.documents : [];
    if (docs.length) {
      html += '<div class="section-label">检索文档（' + docs.length + "）</div>";
      docs.forEach(function (d, i) { html += ragDocCardHtml(d, i); });
    }
    const actions = Array.isArray(r.action_history) ? r.action_history : [];
    if (actions.length) {
      html += '<div class="section-label">行动历史</div><div class="timeline">';
      actions.forEach(function (a, i) {
        const atype = a.action || a.type || a.step_type || "action";
        const desc = a.thought || a.description || a.reasoning || a.query || a.content || "";
        html += '<div class="timeline-item">';
        html += '<span class="timeline-dot" style="background:var(--info)"></span>';
        html += '<div><div class="timeline-title">#' + (i + 1) +
          ' <span class="traj-step-type action">' + escapeHtml(atype) + "</span></div>";
        if (desc) html += '<div class="traj-step-content">' + escapeHtml(desc) + "</div>";
        html += "</div></div>";
      });
      html += "</div>";
    }
    out.innerHTML = html;
  }

  async function ragCorrect() {
    const btn = document.getElementById("ragCorrectBtn");
    const out = document.getElementById("ragCorrectResult");
    const query = document.getElementById("ragCorrectQuery").value.trim();
    if (!query) { toast("请输入查询", "error"); return; }
    const iter = parseInt(document.getElementById("ragCorrectIter").value, 10);
    const body = { query: query, max_iterations: isNaN(iter) ? 2 : iter, top_k: 5 };
    advBtnBusy(btn, true, "纠正中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/rag/correct", { method: "POST", body: body, timeoutMs: 180000 });
      renderRagCorrect(r);
      toast("Corrective RAG 完成", "success");
    } catch (e) {
      advErrorRetry(out, e.message, ragCorrect);
      toast("Corrective RAG 失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderRagCorrect(r) {
    const out = document.getElementById("ragCorrectResult");
    let html = '<div class="verify-banner ' + (r.corrected ? "fail" : "ok") + '">';
    html += '<span class="verify-icon">' + (r.corrected ? "🔧" : "✅") + "</span>";
    html += '<div><div class="verify-title">' +
      (r.corrected ? "已触发查询纠正" : "无需纠正") + "</div>";
    html += '<div class="verify-sub">迭代 ' + (r.iterations != null ? r.iterations : 0) + " 次";
    if (r.original_query && r.original_query !== r.query) {
      html += " · 原始查询：" + escapeHtml(r.original_query);
    }
    html += "</div></div></div>";

    const ev = r.evaluation || {};
    const evKeys = Object.keys(ev);
    if (evKeys.length) {
      html += '<div class="section-label">检索评估</div><div class="metric-grid">';
      evKeys.forEach(function (k) {
        const v = ev[k];
        if (typeof v === "number") {
          html += '<div class="metric-card"><div class="metric-label">' + escapeHtml(k) +
            '</div><div class="metric-value" data-target="' + v + '">' +
            Number(v).toFixed(2) + "</div></div>";
        } else {
          html += '<div class="metric-card"><div class="metric-label">' + escapeHtml(k) +
            '</div><div class="metric-value">' + escapeHtml(String(v)) + "</div></div>";
        }
      });
      html += "</div>";
    }

    const docs = Array.isArray(r.docs) ? r.docs : [];
    if (docs.length) {
      html += '<div class="section-label">检索文档（' + docs.length + "）</div>";
      docs.forEach(function (d, i) { html += ragDocCardHtml(d, i); });
    }

    const trace = Array.isArray(r.trace) ? r.trace : [];
    if (trace.length) {
      html += '<div class="section-label">纠正轨迹</div><div class="timeline">';
      trace.forEach(function (t, i) {
        const assessment = t.assessment || "";
        const ok = assessment.toLowerCase().indexOf("correct") >= 0;
        html += '<div class="timeline-item">';
        html += '<span class="timeline-dot" style="background:' +
          (ok ? "var(--success)" : "var(--warning)") + '"></span>';
        html += '<div><div class="timeline-title">迭代 ' +
          (t.iteration != null ? t.iteration : i) + ' · <span class="badge ' +
          (ok ? "info" : "warning") + '">' + escapeHtml(assessment || "—") +
          "</span></div>";
        html += '<div class="timeline-meta">查询：' + escapeHtml(t.query || "—") +
          " · 文档数 " + (t.doc_count != null ? t.doc_count : "—") + "</div>";
        html += "</div></div>";
      });
      html += "</div>";
    }
    out.innerHTML = html;
    advAnimateMetrics(out);
  }

  async function ragCacheStats() {
    const out = document.getElementById("ragCacheResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/rag/cache/stats");
      renderRagCache(r);
    } catch (e) {
      advErrorRetry(out, e.message, ragCacheStats);
    }
  }
  function renderRagCache(r) {
    const out = document.getElementById("ragCacheResult");
    const hitPct = (typeof r.hit_rate === "number") ? Math.round(r.hit_rate * 100) : 0;
    let html = '<div class="metric-grid">';
    html += '<div class="metric-card"><div class="metric-label">命中率</div>' +
      '<div class="metric-value"><span data-target="' + hitPct + '">0</span>%</div></div>';
    html += '<div class="metric-card"><div class="metric-label">当前大小</div>' +
      '<div class="metric-value" data-target="' + (r.size || 0) + '">' + (r.size || 0) + "</div></div>";
    html += '<div class="metric-card"><div class="metric-label">命中次数</div>' +
      '<div class="metric-value" data-target="' + (r.hits || 0) + '">' + (r.hits || 0) + "</div></div>";
    html += '<div class="metric-card"><div class="metric-label">未命中</div>' +
      '<div class="metric-value" data-target="' + (r.misses || 0) + '">' + (r.misses || 0) + "</div></div>";
    html += '<div class="metric-card"><div class="metric-label">驱逐次数</div>' +
      '<div class="metric-value" data-target="' + (r.evictions || 0) + '">' + (r.evictions || 0) + "</div></div>";
    html += '<div class="metric-card"><div class="metric-label">最大容量</div>' +
      '<div class="metric-value">' + (r.max_size != null ? escapeHtml(String(r.max_size)) : "—") + "</div></div>";
    html += "</div>";
    if (r.ttl_seconds != null) {
      html += '<p class="hint">TTL：' + escapeHtml(String(r.ttl_seconds)) + " 秒</p>";
    }
    out.innerHTML = html;
    advAnimateMetrics(out);
  }

  document.getElementById("ragRouteBtn").addEventListener("click", ragRoute);
  document.getElementById("ragRouteInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); ragRoute(); }
  });
  document.getElementById("ragAgenticBtn").addEventListener("click", ragAgentic);
  document.getElementById("ragCorrectBtn").addEventListener("click", ragCorrect);
  document.getElementById("ragCacheBtn").addEventListener("click", ragCacheStats);
  document.getElementById("ragCacheClearBtn").addEventListener("click", async function () {
    await confirmDialog({
      title: "清空 RAG 缓存",
      message: "确认清空所有 RAG 查询缓存？",
      okText: "清空",
      danger: true,
      icon: "🗑",
      onConfirm: async function () {
        await api("/api/v1/rag/cache", { method: "DELETE" });
        toast("RAG 缓存已清空", "success");
        ragCacheStats();
      },
    });
  });

  // ════════════════════ 智能体高级功能模块（批次2）════════════════════
  // 通用：防止重复绑定（在 load 函数中调用）
  function advBindOnce(id, ev, fn) {
    const el = document.getElementById(id);
    if (el && !el._advBound) { el._advBound = true; el.addEventListener(ev, fn); }
  }
  // 设置计数标签（保留 .tag 样式）
  function advSetCount(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }
  // 对象值安全字符串化
  function advStr(v) {
    if (v == null) return "—";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  // ── 动态按钮事件委托（一次性绑定，处理动态生成的删除/选择按钮）──
  if (!document._advBatch2Delegated) {
    document._advBatch2Delegated = true;
    document.addEventListener("click", function (e) {
      const memDel = e.target.closest(".mem-del-btn");
      if (memDel) { memDeleteFact(memDel.getAttribute("data-id")); return; }
      const hookDel = e.target.closest(".hook-del-btn");
      if (hookDel) {
        const name = hookDel.getAttribute("data-name");
        confirmDialog({
          title: "删除钩子", message: "确认删除钩子「" + name + "」？此操作不可撤销。",
          okText: "删除", danger: true, icon: "🗑",
          onConfirm: async function () { await hookDelete(name); },
        });
        return;
      }
      const expDel = e.target.closest(".exp-del-btn");
      if (expDel) {
        const id = expDel.getAttribute("data-id");
        confirmDialog({
          title: "删除实验", message: "确认删除该 A/B 实验？此操作不可撤销。",
          okText: "删除", danger: true, icon: "🗑",
          onConfirm: async function () { await expDelete(id); },
        });
        return;
      }
      const tplItem = e.target.closest(".prompt-tpl-item");
      if (tplItem) { promptsSelect(tplItem.getAttribute("data-id")); return; }
    });
    document.addEventListener("change", function (e) {
      const tog = e.target.closest(".hook-toggle");
      if (tog) { hookToggle(tog.getAttribute("data-name"), tog.checked); }
    });
  }

  // ═══════ 1. 记忆管理 ═══════
  function loadMemPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("memFactsResult", "🧠", "尚未加载", "点击「刷新」加载长期事实");
    ensure("memRecallResult", "🔍", "尚未召回", "输入查询后点击「召回」");
    ensure("memSessionsResult", "💬", "尚未加载", "进入页面自动加载会话");
    ensure("memEpisodesResult", "🎬", "尚未加载", "进入页面自动加载情景记忆");
    const imp = document.getElementById("memImportance");
    const impVal = document.getElementById("memImportanceVal");
    if (imp && !imp._advBound) {
      imp._advBound = true;
      imp.addEventListener("input", function () { if (impVal) impVal.textContent = imp.value; });
    }
    advBindOnce("memRefreshBtn", "click", memLoadFacts);
    advBindOnce("memAddBtn", "click", memAddFact);
    advBindOnce("memRecallBtn", "click", memRecall);
    advBindOnce("memTypeFilter", "change", memLoadFacts);
    memLoadFacts();
    memLoadSessions();
    memLoadEpisodes();
  }
  async function memLoadFacts() {
    const out = document.getElementById("memFactsResult");
    const type = document.getElementById("memTypeFilter").value;
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/memory/facts?memory_type=" + encodeURIComponent(type) + "&limit=100");
      renderMemFacts(r);
    } catch (e) {
      advErrorRetry(out, e.message, memLoadFacts);
    }
  }
  function renderMemFacts(r) {
    const out = document.getElementById("memFactsResult");
    const items = Array.isArray(r.items) ? r.items : [];
    let html = '<p class="hint">共 ' + (r.total != null ? r.total : items.length) + " 条事实</p>";
    if (!items.length) { html += emptyState("🧠", "暂无事实", "在下方新增一条事实"); out.innerHTML = html; return; }
    html += '<div class="adv-card-list">';
    items.forEach(function (it, i) {
      const imp = typeof it.importance === "number" ? it.importance : 0;
      const pct = Math.max(0, Math.min(100, Math.round(imp * 100)));
      const type = it.memory_type || "semantic";
      const typeCls = type === "episodic" ? "info" : (type === "procedural" ? "warn" : "success");
      html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
      html += '<div class="finding-head"><span class="adv-badge ' + typeCls + '">' + escapeHtml(type) + "</span>";
      html += '<button class="btn btn-sm btn-danger mem-del-btn" data-id="' + escapeHtml(it.memory_id) + '">删除</button></div>';
      html += '<div class="finding-text">' + escapeHtml(it.content || "") + "</div>";
      html += '<div class="adv-progress"><div class="adv-progress-fill" style="width:' + pct + '%"></div></div>';
      html += '<div class="finding-source">重要度 ' + imp.toFixed(2) + " · 访问 " + (it.access_count || 0) + " 次 · 创建 " + escapeHtml(relativeTime(it.created_at)) + "</div>";
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }
  async function memAddFact() {
    const btn = document.getElementById("memAddBtn");
    const content = document.getElementById("memFactContent").value.trim();
    if (!content) { toast("请输入事实内容", "error"); return; }
    const body = {
      content: content,
      memory_type: document.getElementById("memFactType").value,
      importance: parseNum(document.getElementById("memImportance").value, 0.5),
    };
    advBtnBusy(btn, true, "保存中…");
    try {
      await api("/api/v1/memory/facts", { method: "POST", body: body });
      toast("事实已保存", "success");
      document.getElementById("memFactContent").value = "";
      memLoadFacts();
    } catch (e) {
      toast("保存失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  async function memDeleteFact(factId) {
    try {
      await api("/api/v1/memory/facts/" + encodeURIComponent(factId), { method: "DELETE" });
      toast("已删除事实", "success");
      memLoadFacts();
    } catch (e) {
      toast("删除失败：" + e.message, "error");
    }
  }
  async function memRecall() {
    const btn = document.getElementById("memRecallBtn");
    const out = document.getElementById("memRecallResult");
    const query = document.getElementById("memRecallQuery").value.trim();
    if (!query) { toast("请输入查询", "error"); return; }
    const limit = parseInt(document.getElementById("memRecallLimit").value, 10) || 10;
    advBtnBusy(btn, true, "召回中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/memory/recall", { method: "POST", body: { query: query, limit: limit } });
      renderMemRecall(r);
    } catch (e) {
      advErrorRetry(out, e.message, memRecall);
    } finally {
      advBtnBusy(btn, false);
    }
  }
  function renderMemRecall(r) {
    const out = document.getElementById("memRecallResult");
    const items = Array.isArray(r.items) ? r.items : [];
    if (!items.length) { out.innerHTML = emptyState("🔍", "无召回结果", "未检索到相关记忆"); return; }
    let html = '<p class="hint">召回 ' + (r.total != null ? r.total : items.length) + " 条</p>";
    html += '<div class="adv-card-list">';
    items.forEach(function (it, i) {
      const score = typeof it.score === "number" ? it.score : 0;
      html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
      html += '<div class="finding-text">' + escapeHtml(it.content || "") + "</div>";
      html += '<div class="finding-source">score ' + score.toFixed(3) + " · " + escapeHtml(it.memory_type || "") + "</div>";
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }
  async function memLoadSessions() {
    const out = document.getElementById("memSessionsResult");
    out.innerHTML = advSkeleton(2);
    try {
      const r = await api("/api/v1/memory/sessions");
      const items = Array.isArray(r.items) ? r.items : [];
      if (!items.length) { out.innerHTML = emptyState("💬", "暂无会话", "尚未记录会话"); return; }
      let html = '<div class="adv-card-list">';
      items.forEach(function (s, i) {
        html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
        html += '<div class="finding-head"><span class="adv-badge neutral">' + escapeHtml(s.session_id || "session") + "</span>";
        html += '<span class="finding-rule">' + escapeHtml(relativeTime(s.started_at)) + "</span></div>";
        html += '<div class="finding-source">' + escapeHtml(advStr(s).slice(0, 140)) + "</div>";
        html += "</div>";
      });
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      advErrorRetry(out, e.message, memLoadSessions);
    }
  }
  async function memLoadEpisodes() {
    const out = document.getElementById("memEpisodesResult");
    out.innerHTML = advSkeleton(2);
    try {
      const r = await api("/api/v1/memory/episodes");
      const items = Array.isArray(r.items) ? r.items : [];
      if (!items.length) { out.innerHTML = emptyState("🎬", "暂无情景记忆", "尚未记录情景"); return; }
      let html = '<div class="adv-card-list">';
      items.forEach(function (ep, i) {
        html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
        html += '<div class="finding-text"><b>用户：</b>' + escapeHtml(ep.user_message || "") + "</div>";
        html += '<div class="finding-text"><b>助手：</b>' + escapeHtml(ep.assistant_response || "") + "</div>";
        if (ep.timestamp) html += '<div class="finding-source">' + escapeHtml(relativeTime(ep.timestamp)) + "</div>";
        html += "</div>";
      });
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      advErrorRetry(out, e.message, memLoadEpisodes);
    }
  }

  // ═══════ 2. 生命周期钩子 ═══════
  function loadHooksPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("hooksListResult", "🪝", "尚未加载", "点击「刷新钩子列表」");
    ensure("hooksTypesResult", "📜", "尚未加载", "进入页面自动加载钩子类型");
    advBindOnce("hooksRefreshBtn", "click", hooksLoad);
    advBindOnce("hookCreateBtn", "click", hookCreate);
    hooksLoad();
    hooksLoadTypes();
  }
  async function hooksLoad() {
    const out = document.getElementById("hooksListResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/hooks");
      renderHooks(r);
    } catch (e) {
      advErrorRetry(out, e.message, hooksLoad);
    }
  }
  function renderHooks(r) {
    const out = document.getElementById("hooksListResult");
    const hooks = Array.isArray(r.hooks) ? r.hooks : [];
    advSetCount("hooksCount", hooks.length + " 个");
    if (!hooks.length) { out.innerHTML = emptyState("🪝", "暂无钩子", "在下方注册一个新钩子"); return; }
    let html = '<div class="adv-card-list">';
    hooks.forEach(function (h, i) {
      const type = h.hook_type || "unknown";
      html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
      html += '<div class="finding-head"><span class="finding-rule">' + escapeHtml(h.name) + "</span>";
      html += '<span class="adv-badge info">' + escapeHtml(type) + "</span>";
      html += '<label class="adv-toggle"><input type="checkbox" class="hook-toggle" data-name="' + escapeHtml(h.name) + '"' + (h.enabled ? " checked" : "") + ' /><span class="adv-toggle-slider"></span></label>';
      html += '<button class="btn btn-sm btn-danger hook-del-btn" data-name="' + escapeHtml(h.name) + '">删除</button></div>';
      if (h.description) html += '<div class="finding-text">' + escapeHtml(h.description) + "</div>";
      const script = h.script || "";
      const snip = script.length > 160 ? script.slice(0, 160) + "…" : script;
      html += '<div class="finding-source">优先级 ' + (h.priority != null ? h.priority : 0) + ' · 脚本：<code>' + escapeHtml(snip) + "</code></div>";
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }
  async function hookToggle(name, enabled) {
    try {
      await api("/api/v1/hooks/" + encodeURIComponent(name), { method: "PATCH", body: { enabled: enabled } });
      toast(enabled ? "钩子已启用" : "钩子已停用", "success");
    } catch (e) {
      toast("更新失败：" + e.message, "error");
      hooksLoad();
    }
  }
  async function hookDelete(name) {
    try {
      await api("/api/v1/hooks/" + encodeURIComponent(name), { method: "DELETE" });
      toast("已删除钩子", "success");
      hooksLoad();
    } catch (e) {
      toast("删除失败：" + e.message, "error");
    }
  }
  async function hookCreate() {
    const btn = document.getElementById("hookCreateBtn");
    const name = document.getElementById("hookName").value.trim();
    if (!name) { toast("请输入钩子名称", "error"); return; }
    const body = {
      name: name,
      hook_type: document.getElementById("hookType").value,
      priority: parseIntSafe(document.getElementById("hookPriority").value, 0),
      enabled: document.getElementById("hookEnabled").checked,
      description: document.getElementById("hookDesc").value.trim(),
      script: document.getElementById("hookScript").value,
    };
    advBtnBusy(btn, true, "注册中…");
    try {
      await api("/api/v1/hooks", { method: "POST", body: body });
      toast("钩子已注册", "success");
      document.getElementById("hookName").value = "";
      document.getElementById("hookDesc").value = "";
      document.getElementById("hookScript").value = "";
      hooksLoad();
    } catch (e) {
      toast("注册失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  async function hooksLoadTypes() {
    const sel = document.getElementById("hookType");
    const out = document.getElementById("hooksTypesResult");
    try {
      const r = await api("/api/v1/hooks/types");
      const types = Array.isArray(r) ? r : (Array.isArray(r.types) ? r.types : []);
      if (sel) {
        sel.innerHTML = types.length
          ? types.map(function (t) { return '<option value="' + escapeHtml(t) + '">' + escapeHtml(t) + "</option>"; }).join("")
          : '<option value="">（无）</option>';
      }
      if (out) {
        let html = '<div class="adv-section-label">可用钩子类型</div>';
        if (types.length) {
          html += '<div style="display:flex;flex-wrap:wrap;gap:6px">' +
            types.map(function (t) { return '<span class="adv-tag">' + escapeHtml(t) + "</span>"; }).join("") + "</div>";
        } else {
          html += '<p class="hint">暂无类型数据</p>';
        }
        out.innerHTML = html;
      }
    } catch (e) {
      if (out) advErrorRetry(out, e.message, hooksLoadTypes);
    }
  }

  // ═══════ 3. 可观测性 ═══════
  function loadObsPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("obsSnapshotResult", "📊", "尚未加载", "点击「刷新快照」");
    ensure("obsMetricsResult", "📈", "尚未加载", "刷新快照后自动加载");
    ensure("obsTracesResult", "🔗", "尚未加载", "刷新快照后自动加载");
    ensure("obsLogsResult", "📝", "尚未加载", "刷新快照后自动加载");
    advBindOnce("obsRefreshBtn", "click", obsSnapshot);
    obsSnapshot();
  }
  async function obsSnapshot() {
    const btn = document.getElementById("obsRefreshBtn");
    const out = document.getElementById("obsSnapshotResult");
    if (btn) advBtnBusy(btn, true, "加载中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/observability/snapshot");
      renderObsSnapshot(r);
      renderObsMetrics(r.metrics || {});
      renderObsTraces(r.traces || []);
      renderObsLogs(r.recent_logs || r.logs || []);
    } catch (e) {
      advErrorRetry(out, e.message, obsSnapshot);
    } finally {
      if (btn) advBtnBusy(btn, false);
    }
  }
  function renderObsSnapshot(r) {
    const out = document.getElementById("obsSnapshotResult");
    const traces = Array.isArray(r.traces) ? r.traces : [];
    const logs = Array.isArray(r.recent_logs) ? r.recent_logs : (Array.isArray(r.logs) ? r.logs : []);
    const errCount = logs.filter(function (l) {
      const lv = (l && (l.level || "")).toString().toLowerCase();
      return lv === "error";
    }).length;
    const cards = [
      { label: "Traces", value: traces.length, target: traces.length },
      { label: "日志条数", value: logs.length, target: logs.length },
      { label: "错误数", value: errCount, target: errCount, style: "border-color:var(--danger)" },
    ];
    let html = advMetricGrid(cards);
    const health = r.health || {};
    const hKeys = Object.keys(health);
    if (hKeys.length) {
      html += '<div class="adv-section-label">健康状态</div><div class="kv-grid">';
      hKeys.forEach(function (k) { html += kvCell(k, advStr(health[k])); });
      html += "</div>";
    }
    out.innerHTML = html;
    advAnimateMetrics(out);
  }
  async function renderObsMetrics(metrics) {
    const out = document.getElementById("obsMetricsResult");
    if (!metrics || !Object.keys(metrics).length) {
      try {
        const r = await api("/api/v1/observability/metrics");
        metrics = (r && r.metrics) ? r.metrics : (r || {});
      } catch (e) { advErrorRetry(out, e.message, function () { renderObsMetrics({}); }); return; }
    }
    if (!metrics || (typeof metrics === "object" && !Object.keys(metrics).length)) {
      out.innerHTML = emptyState("📈", "暂无指标", "尚无运行时指标数据");
      return;
    }
    out.innerHTML = advJsonTree(metrics);
  }
  function renderObsTraces(traces) {
    const out = document.getElementById("obsTracesResult");
    if (!Array.isArray(traces) || !traces.length) {
      out.innerHTML = emptyState("🔗", "暂无 Traces", "尚无追踪记录");
      return;
    }
    let html = '<div class="adv-card-list">';
    traces.slice(0, 30).forEach(function (t, i) {
      const tid = t.trace_id || t.id || ("trace-" + i);
      const spans = Array.isArray(t.spans) ? t.spans.length : (t.span_count != null ? t.span_count : 0);
      const dur = t.duration_ms != null ? t.duration_ms : (t.duration != null ? t.duration : null);
      const status = t.status || (t.error ? "error" : "ok");
      const sCls = status === "error" ? "danger" : "success";
      html += '<div class="adv-item-card" style="animation-delay:' + (i * 30) + 'ms">';
      html += '<div class="finding-head"><span class="finding-rule">' + escapeHtml(tid) + "</span>";
      html += '<span class="adv-badge ' + sCls + '">' + escapeHtml(status) + "</span></div>";
      html += '<div class="finding-source">spans: ' + spans + (dur != null ? " · 时长 " + dur + "ms" : "") + "</div>";
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }
  function renderObsLogs(logs) {
    const out = document.getElementById("obsLogsResult");
    if (!Array.isArray(logs) || !logs.length) {
      out.innerHTML = emptyState("📝", "暂无日志", "尚无运行日志");
      return;
    }
    let html = '<div class="adv-card-list">';
    logs.slice(0, 50).forEach(function (l, i) {
      const isStr = typeof l === "string";
      const level = isStr ? "info" : ((l.level || "info").toString().toLowerCase());
      const cls = level === "error" ? "danger" : (level === "warn" || level === "warning" ? "warn" : "info");
      const ts = isStr ? "" : (l.timestamp || l.time || l.created_at || "");
      const msg = isStr ? l : (l.message || l.msg || JSON.stringify(l));
      html += '<div class="adv-item-card" style="animation-delay:' + (i * 20) + 'ms">';
      html += '<div class="finding-head"><span class="adv-badge ' + cls + '">' + escapeHtml(level) + "</span>";
      if (ts) html += '<span class="finding-rule">' + escapeHtml(relativeTime(ts)) + "</span>";
      html += "</div>";
      html += '<div class="finding-text">' + escapeHtml(msg) + "</div>";
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }

  // ═══════ 4. 强化学习反馈 ═══════
  function loadRlPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("rlSubmitResult", "📝", "尚未提交", "填写表单后点击「提交反馈」");
    ensure("rlPrefsResult", "📊", "尚未加载", "进入页面自动加载偏好统计");
    ensure("rlPolicyResult", "🎯", "尚未加载", "进入页面自动加载策略状态");
    const grp = document.getElementById("rlRatingGroup");
    if (grp && !grp._advBound) {
      grp._advBound = true;
      grp.addEventListener("click", function (e) {
        const b = e.target.closest(".adv-rating-btn");
        if (!b) return;
        grp.querySelectorAll(".adv-rating-btn").forEach(function (x) { x.classList.remove("active"); });
        b.classList.add("active");
        document.getElementById("rlRating").value = b.getAttribute("data-rating");
      });
    }
    advBindOnce("rlSubmitBtn", "click", rlSubmit);
    rlLoadPrefs();
    rlLoadPolicy();
  }
  async function rlSubmit() {
    const btn = document.getElementById("rlSubmitBtn");
    const out = document.getElementById("rlSubmitResult");
    const taskId = document.getElementById("rlTaskId").value.trim();
    const query = document.getElementById("rlQuery").value.trim();
    const response = document.getElementById("rlResponse").value.trim();
    if (!taskId && !query) { toast("请填写任务 ID 或查询", "error"); return; }
    const body = {
      task_id: taskId || null,
      query: query,
      response: response,
      rating: parseIntSafe(document.getElementById("rlRating").value, 0),
      comment: document.getElementById("rlComment").value.trim(),
      user_id: document.getElementById("rlUserId").value.trim() || null,
    };
    advBtnBusy(btn, true, "提交中…");
    out.innerHTML = advSkeleton(2);
    try {
      const r = await api("/api/v1/rl/feedback", { method: "POST", body: body });
      let html = '<div class="kv-grid">';
      html += kvCell("是否记录", r.recorded ? "是" : "否");
      html += kvCell("反馈 ID", r.feedback_id || "—");
      html += kvCell("奖励值", r.reward != null ? r.reward : "—");
      html += "</div>";
      if (r.message) html += '<p class="hint">' + escapeHtml(r.message) + "</p>";
      out.innerHTML = html;
      toast("反馈已提交", "success");
      rlLoadPrefs();
      rlLoadPolicy();
    } catch (e) {
      advErrorRetry(out, e.message, rlSubmit);
    } finally {
      advBtnBusy(btn, false);
    }
  }
  async function rlLoadPrefs() {
    const out = document.getElementById("rlPrefsResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/rl/preferences");
      const cards = [
        { label: "总数", value: r.total || 0, target: r.total || 0 },
        { label: "正面", value: r.positive || 0, target: r.positive || 0 },
        { label: "中性", value: r.neutral || 0, target: r.neutral || 0 },
        { label: "负面", value: r.negative || 0, target: r.negative || 0 },
        { label: "平均奖励", value: typeof r.average_reward === "number" ? Number(r.average_reward).toFixed(3) : "—" },
      ];
      let html = advMetricGrid(cards);
      const recent = Array.isArray(r.recent) ? r.recent : [];
      if (recent.length) {
        html += '<div class="adv-section-label">最近反馈（' + recent.length + "）</div><div class=\"adv-card-list\">";
        recent.slice(0, 10).forEach(function (it, i) {
          const rating = it.rating != null ? it.rating : "—";
          const rCls = rating === 1 ? "success" : (rating === -1 ? "danger" : "neutral");
          html += '<div class="adv-item-card" style="animation-delay:' + (i * 30) + 'ms">';
          html += '<div class="finding-head"><span class="adv-badge ' + rCls + '">rating ' + escapeHtml(String(rating)) + "</span>";
          if (it.task_id) html += '<span class="finding-rule">' + escapeHtml(it.task_id) + "</span>";
          html += "</div>";
          if (it.comment) html += '<div class="finding-text">' + escapeHtml(it.comment) + "</div>";
          html += "</div>";
        });
        html += "</div>";
      }
      out.innerHTML = html;
      advAnimateMetrics(out);
    } catch (e) {
      advErrorRetry(out, e.message, rlLoadPrefs);
    }
  }
  async function rlLoadPolicy() {
    const out = document.getElementById("rlPolicyResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/rl/policy");
      const cards = [
        { label: "策略版本", value: r.policy_version || "—" },
        { label: "经验总数", value: r.total_experiences || 0, target: r.total_experiences || 0 },
        { label: "反馈总数", value: r.total_feedback || 0, target: r.total_feedback || 0 },
        { label: "平均奖励", value: typeof r.average_reward === "number" ? Number(r.average_reward).toFixed(3) : "—" },
      ];
      let html = advMetricGrid(cards);
      const tools = Array.isArray(r.top_tools) ? r.top_tools : [];
      if (tools.length) {
        html += '<div class="adv-section-label">高频工具</div><div class="adv-card-list">';
        tools.forEach(function (t, i) {
          const name = typeof t === "object" ? (t.name || t.tool || JSON.stringify(t)) : t;
          const cnt = typeof t === "object" ? (t.count || t.uses || "") : "";
          html += '<div class="adv-item-card" style="animation-delay:' + (i * 30) + 'ms">';
          html += '<div class="finding-head"><span class="finding-rule">' + escapeHtml(String(name)) + "</span>";
          if (cnt !== "") html += '<span class="adv-badge neutral">' + escapeHtml(String(cnt)) + "</span>";
          html += "</div></div>";
        });
        html += "</div>";
      }
      const lessons = Array.isArray(r.top_lessons) ? r.top_lessons : [];
      if (lessons.length) {
        html += '<div class="adv-section-label">高频课程</div><div class="adv-card-list">';
        lessons.forEach(function (l, i) {
          const text = typeof l === "object" ? (l.lesson || l.text || JSON.stringify(l)) : l;
          html += '<div class="adv-item-card" style="animation-delay:' + (i * 30) + 'ms">';
          html += '<div class="finding-text">' + escapeHtml(String(text)) + "</div>";
          html += "</div>";
        });
        html += "</div>";
      }
      out.innerHTML = html;
      advAnimateMetrics(out);
    } catch (e) {
      advErrorRetry(out, e.message, rlLoadPolicy);
    }
  }

  // ═══════ 5. 多智能体协作 ═══════
  function loadCollabPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("collabAgentsResult", "🤝", "尚未加载", "点击「刷新智能体」");
    ensure("collabDelegateResult", "📤", "尚未委派", "填写任务后点击「委派任务」");
    ensure("collabMessagesResult", "📨", "尚未加载", "进入页面自动加载消息流");
    advBindOnce("collabRefreshBtn", "click", collabLoadAgents);
    advBindOnce("collabDelegateBtn", "click", collabDelegate);
    collabLoadAgents();
    collabLoadMessages();
  }
  async function collabLoadAgents() {
    const out = document.getElementById("collabAgentsResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/collab/agents");
      const agents = Array.isArray(r.agents) ? r.agents : [];
      advSetCount("collabCount", (r.total != null ? r.total : agents.length) + " 个");
      if (!agents.length) { out.innerHTML = emptyState("🤝", "暂无协作智能体", "尚未注册子智能体"); return; }
      let html = '<div class="adv-card-list">';
      agents.forEach(function (a, i) {
        const name = a.name || ("agent-" + i);
        const role = a.role || "unknown";
        html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
        html += '<div class="finding-head"><span class="finding-rule">' + escapeHtml(name) + "</span>";
        html += '<span class="adv-badge info">' + escapeHtml(role) + "</span></div>";
        if (a.description) html += '<div class="finding-text">' + escapeHtml(a.description) + "</div>";
        html += "</div>";
      });
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      advErrorRetry(out, e.message, collabLoadAgents);
    }
  }
  async function collabDelegate() {
    const btn = document.getElementById("collabDelegateBtn");
    const out = document.getElementById("collabDelegateResult");
    const task = document.getElementById("collabTask").value.trim();
    const role = document.getElementById("collabRole").value.trim();
    if (!task) { toast("请输入任务描述", "error"); return; }
    const body = {
      task: task,
      role: role || null,
      context: document.getElementById("collabContext").value.trim() || null,
      max_rounds: parseInt(document.getElementById("collabMaxRounds").value, 10) || 3,
    };
    advBtnBusy(btn, true, "委派中…");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/collab/delegate", { method: "POST", body: body, timeoutMs: 120000 });
      let html = '<div class="kv-grid">';
      html += kvCell("已委派", r.delegated ? "是" : "否");
      html += kvCell("目标角色", r.role || "—");
      html += kvCell("消息 ID", r.message_id || "—");
      html += kvCell("轮次", r.rounds != null ? r.rounds : "—");
      html += "</div>";
      if (r.response) {
        html += '<div class="adv-section-label">响应</div>';
        html += '<div class="tot-answer">' + advMarkdown(r.response) + "</div>";
      }
      out.innerHTML = html;
      toast("任务委派完成", "success");
      collabLoadMessages();
    } catch (e) {
      advErrorRetry(out, e.message, collabDelegate);
    } finally {
      advBtnBusy(btn, false);
    }
  }
  async function collabLoadMessages() {
    const out = document.getElementById("collabMessagesResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/collab/messages");
      const msgs = Array.isArray(r.messages) ? r.messages : [];
      if (!msgs.length) { out.innerHTML = emptyState("📨", "暂无消息", "尚未产生协作消息"); return; }
      let html = '<div class="timeline">';
      msgs.slice(0, 30).forEach(function (m, i) {
        const sender = m.sender || m.from || m.agent || "unknown";
        const ts = m.timestamp || m.created_at || m.time || "";
        const content = m.content || m.message || m.text || JSON.stringify(m);
        html += '<div class="timeline-item">';
        html += '<span class="timeline-dot" style="background:var(--info)"></span>';
        html += '<div><div class="timeline-title"><span class="adv-badge neutral">' + escapeHtml(sender) + "</span>";
        if (ts) html += ' <span class="finding-rule">' + escapeHtml(relativeTime(ts)) + "</span>";
        html += "</div>";
        html += '<div class="traj-step-content">' + escapeHtml(content) + "</div></div></div>";
      });
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      advErrorRetry(out, e.message, collabLoadMessages);
    }
  }

  // ═══════ 6. 插件管理 ═══════
  function loadPluginsPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("pluginsListResult", "🧩", "尚未加载", "点击「刷新插件」");
    advBindOnce("pluginsRefreshBtn", "click", pluginsLoad);
    pluginsLoad();
  }
  async function pluginsLoad() {
    const out = document.getElementById("pluginsListResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/plugins");
      const plugins = Array.isArray(r.plugins) ? r.plugins : [];
      advSetCount("pluginsCount", (r.total != null ? r.total : plugins.length) + " 个");
      if (!plugins.length) { out.innerHTML = emptyState("🧩", "暂无插件", "尚未注册插件"); return; }
      let html = '<div class="adv-card-list">';
      plugins.forEach(function (p, i) {
        const name = p.name || ("plugin-" + i);
        const ver = p.version || "";
        const enabled = p.enabled !== false;
        html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
        html += '<div class="finding-head"><span class="finding-rule">' + escapeHtml(name) + "</span>";
        if (ver) html += '<span class="adv-badge info">v' + escapeHtml(ver) + "</span>";
        html += '<span class="adv-badge ' + (enabled ? "success" : "neutral") + '">' + (enabled ? "启用" : "停用") + "</span></div>";
        if (p.description) html += '<div class="finding-text">' + escapeHtml(p.description) + "</div>";
        html += "</div>";
      });
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      advErrorRetry(out, e.message, pluginsLoad);
    }
  }

  // ═══════ 7. A/B 实验 ═══════
  function loadExpPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("expListResult", "🧪", "尚未加载", "点击「刷新实验」");
    ensure("expAssignResult", "🎲", "尚未分配", "选择实验后点击「分配变体」");
    advBindOnce("expRefreshBtn", "click", expLoad);
    advBindOnce("expAddVariantBtn", "click", expAddVariantRow);
    advBindOnce("expCreateBtn", "click", expCreate);
    advBindOnce("expAssignBtn", "click", expAssign);
    const vc = document.getElementById("expVariants");
    if (vc && !vc.children.length) { expAddVariantRow(); expAddVariantRow(); }
    expLoad();
  }
  function expAddVariantRow() {
    const vc = document.getElementById("expVariants");
    if (!vc) return;
    const row = document.createElement("div");
    row.className = "adv-variant-row";
    row.innerHTML = '<input type="text" class="exp-var-name" placeholder="变体名" style="flex:2" />' +
      '<input type="number" class="exp-var-weight" placeholder="权重" value="1" min="0" style="flex:1;max-width:100px" />' +
      '<button class="btn btn-sm btn-ghost exp-var-del" type="button">×</button>';
    row.querySelector(".exp-var-del").addEventListener("click", function () {
      if (vc.children.length > 1) vc.removeChild(row);
    });
    vc.appendChild(row);
  }
  async function expLoad() {
    const out = document.getElementById("expListResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/experiments");
      const exps = Array.isArray(r.experiments) ? r.experiments : [];
      renderExpList(r);
      const sel = document.getElementById("expAssignSelect");
      if (sel) {
        sel.innerHTML = exps.length
          ? exps.map(function (e) { return '<option value="' + escapeHtml(e.id) + '">' + escapeHtml(e.name || e.id) + "</option>"; }).join("")
          : '<option value="">暂无实验</option>';
      }
    } catch (e) {
      advErrorRetry(out, e.message, expLoad);
    }
  }
  function renderExpList(r) {
    const out = document.getElementById("expListResult");
    const exps = Array.isArray(r.experiments) ? r.experiments : [];
    if (!exps.length) { out.innerHTML = emptyState("🧪", "暂无实验", "在下方创建一个新实验"); return; }
    let html = '<div class="adv-card-list">';
    exps.forEach(function (e, i) {
      const name = e.name || e.id;
      const status = e.status || "running";
      const sCls = status === "running" ? "success" : (status === "paused" ? "warn" : "info");
      const variants = Array.isArray(e.variants) ? e.variants : [];
      html += '<div class="adv-item-card" style="animation-delay:' + (i * 40) + 'ms">';
      html += '<div class="finding-head"><span class="finding-rule">' + escapeHtml(name) + "</span>";
      html += '<span class="adv-badge ' + sCls + '">' + escapeHtml(status) + "</span>";
      html += '<button class="btn btn-sm btn-danger exp-del-btn" data-id="' + escapeHtml(e.id) + '">删除</button></div>';
      if (e.description) html += '<div class="finding-text">' + escapeHtml(e.description) + "</div>";
      html += '<div class="finding-source">指标：' + escapeHtml(e.metric || "—") + " · 流量 " + (e.traffic_pct != null ? e.traffic_pct : 100) + "%";
      if (e.created_at) html += " · 创建 " + escapeHtml(relativeTime(e.created_at));
      html += "</div>";
      if (variants.length) {
        html += '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">' +
          variants.map(function (v) {
            const vn = typeof v === "object" ? (v.name || JSON.stringify(v)) : v;
            return '<span class="adv-tag">' + escapeHtml(String(vn)) + "</span>";
          }).join("") + "</div>";
      }
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }
  async function expDelete(id) {
    try {
      await api("/api/v1/experiments/" + encodeURIComponent(id), { method: "DELETE" });
      toast("已删除实验", "success");
      expLoad();
    } catch (e) {
      toast("删除失败：" + e.message, "error");
    }
  }
  async function expCreate() {
    const btn = document.getElementById("expCreateBtn");
    const name = document.getElementById("expName").value.trim();
    if (!name) { toast("请输入实验名称", "error"); return; }
    const variants = [];
    document.querySelectorAll("#expVariants .adv-variant-row").forEach(function (row) {
      const n = row.querySelector(".exp-var-name").value.trim();
      const w = parseFloat(row.querySelector(".exp-var-weight").value);
      if (n) variants.push({ name: n, weight: isNaN(w) ? 1 : w });
    });
    if (!variants.length) { toast("请至少添加一个变体", "error"); return; }
    const body = {
      name: name,
      description: document.getElementById("expDesc").value.trim(),
      metric: document.getElementById("expMetric").value.trim() || null,
      traffic_pct: parseInt(document.getElementById("expTraffic").value, 10),
      variants: variants,
    };
    advBtnBusy(btn, true, "创建中…");
    try {
      await api("/api/v1/experiments", { method: "POST", body: body });
      toast("实验已创建", "success");
      document.getElementById("expName").value = "";
      document.getElementById("expDesc").value = "";
      expLoad();
    } catch (e) {
      toast("创建失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  async function expAssign() {
    const btn = document.getElementById("expAssignBtn");
    const out = document.getElementById("expAssignResult");
    const id = document.getElementById("expAssignSelect").value;
    if (!id) { toast("请先选择实验", "error"); return; }
    const userId = document.getElementById("expAssignUser").value.trim();
    const body = userId ? { user_id: userId } : {};
    if (btn) advBtnBusy(btn, true, "分配中…");
    out.innerHTML = advSkeleton(2);
    try {
      const r = await api("/api/v1/experiments/" + encodeURIComponent(id) + "/assign", { method: "POST", body: body });
      let html = '<div class="kv-grid">';
      html += kvCell("分配变体", r.variant || r.variant_name || "—");
      if (r.user_id) html += kvCell("用户 ID", r.user_id);
      if (r.experiment_id) html += kvCell("实验 ID", r.experiment_id);
      html += "</div>";
      out.innerHTML = html;
      toast("已分配变体", "success");
    } catch (e) {
      advErrorRetry(out, e.message, expAssign);
    } finally {
      if (btn) advBtnBusy(btn, false);
    }
  }

  // ═══════ 8. Prompt 模板 ═══════
  let promptsSelectedId = null;
  let promptsCache = [];
  function loadPromptsPage() {
    const ensure = function (id, icon, text, hint) {
      const el = document.getElementById(id);
      if (el && !el.innerHTML.trim()) el.innerHTML = emptyState(icon, text, hint);
    };
    ensure("promptsListResult", "📝", "尚未加载", "点击「刷新模板」");
    ensure("promptTplRenderResult", "🎨", "尚未渲染", "选择模板后填写变量并渲染");
    ensure("promptTplVersionsResult", "📜", "尚未查看", "选择模板后点击「版本历史」");
    advBindOnce("promptsRefreshBtn", "click", promptsLoad);
    advBindOnce("promptsNewBtn", "click", promptsNew);
    advBindOnce("promptTplSaveBtn", "click", promptsSave);
    advBindOnce("promptTplDeleteBtn", "click", promptsDelete);
    advBindOnce("promptTplVersionsBtn", "click", promptsLoadVersions);
    advBindOnce("promptTplRenderBtn", "click", promptsRender);
    promptsLoad();
  }
  async function promptsLoad() {
    const out = document.getElementById("promptsListResult");
    out.innerHTML = advSkeleton(3);
    try {
      const r = await api("/api/v1/prompts");
      promptsCache = Array.isArray(r.templates) ? r.templates : [];
      renderPromptsList();
    } catch (e) {
      advErrorRetry(out, e.message, promptsLoad);
    }
  }
  function renderPromptsList() {
    const out = document.getElementById("promptsListResult");
    if (!promptsCache.length) { out.innerHTML = emptyState("📝", "暂无模板", "点击「新建模板」创建"); return; }
    let html = '<div class="adv-card-list">';
    promptsCache.forEach(function (t, i) {
      const name = t.name || t.id;
      const ver = t.version != null ? t.version : "";
      const tags = Array.isArray(t.tags) ? t.tags : [];
      const sel = t.id === promptsSelectedId ? " selected" : "";
      html += '<div class="adv-item-card prompt-tpl-item' + sel + '" data-id="' + escapeHtml(t.id) + '" style="animation-delay:' + (i * 30) + 'ms;cursor:pointer">';
      html += '<div class="finding-head"><span class="finding-rule">' + escapeHtml(name) + "</span>";
      if (ver !== "") html += '<span class="adv-badge neutral">v' + escapeHtml(String(ver)) + "</span>";
      html += "</div>";
      if (tags.length) {
        html += '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">' +
          tags.map(function (tg) { return '<span class="adv-tag">' + escapeHtml(tg) + "</span>"; }).join("") + "</div>";
      }
      if (t.updated_at) html += '<div class="finding-source">' + escapeHtml(relativeTime(t.updated_at)) + "</div>";
      html += "</div>";
    });
    html += "</div>";
    out.innerHTML = html;
  }
  function promptsSelect(id) {
    const t = promptsCache.find(function (x) { return x.id === id; });
    if (!t) return;
    promptsSelectedId = id;
    document.getElementById("promptTplName").value = t.name || "";
    document.getElementById("promptTplContent").value = t.template || "";
    const vars = Array.isArray(t.variables) ? t.variables : [];
    document.getElementById("promptTplVars").value = vars.join(", ");
    const tags = Array.isArray(t.tags) ? t.tags : [];
    document.getElementById("promptTplTags").value = tags.join(", ");
    document.getElementById("promptTplDesc").value = t.description || "";
    document.getElementById("promptTplDeleteBtn").disabled = false;
    document.getElementById("promptTplVersionsBtn").disabled = false;
    promptsBuildRenderInputs(vars);
    renderPromptsList();
    document.getElementById("promptTplRenderResult").innerHTML = emptyState("🎨", "尚未渲染", "填写变量值后点击「渲染」");
    document.getElementById("promptTplVersionsResult").innerHTML = "";
  }
  function promptsNew() {
    promptsSelectedId = null;
    document.getElementById("promptTplName").value = "";
    document.getElementById("promptTplContent").value = "";
    document.getElementById("promptTplVars").value = "";
    document.getElementById("promptTplTags").value = "";
    document.getElementById("promptTplDesc").value = "";
    document.getElementById("promptTplDeleteBtn").disabled = true;
    document.getElementById("promptTplVersionsBtn").disabled = true;
    document.getElementById("promptTplRenderInputs").innerHTML = '<p class="hint">保存模板后可渲染测试</p>';
    document.getElementById("promptTplRenderResult").innerHTML = emptyState("🎨", "尚未渲染", "保存模板后可渲染测试");
    document.getElementById("promptTplVersionsResult").innerHTML = "";
    renderPromptsList();
    document.getElementById("promptTplName").focus();
  }
  function promptsBuildRenderInputs(vars) {
    const box = document.getElementById("promptTplRenderInputs");
    if (!vars.length) { box.innerHTML = '<p class="hint">该模板无变量</p>'; return; }
    let html = '<div class="adv-section-label">变量值</div>';
    vars.forEach(function (v) {
      html += '<div class="form-row"><label>' + escapeHtml(v) + '</label><input type="text" class="prompt-render-var" data-var="' + escapeHtml(v) + '" placeholder="' + escapeHtml(v) + '" /></div>';
    });
    box.innerHTML = html;
  }
  async function promptsSave() {
    const btn = document.getElementById("promptTplSaveBtn");
    const name = document.getElementById("promptTplName").value.trim();
    const template = document.getElementById("promptTplContent").value;
    if (!name) { toast("请输入模板名称", "error"); return; }
    if (!template) { toast("请输入模板内容", "error"); return; }
    const vars = document.getElementById("promptTplVars").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    const tags = document.getElementById("promptTplTags").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    const body = {
      name: name,
      template: template,
      variables: vars,
      description: document.getElementById("promptTplDesc").value.trim(),
      tags: tags,
    };
    advBtnBusy(btn, true, "保存中…");
    try {
      let r;
      if (promptsSelectedId) {
        r = await api("/api/v1/prompts/" + encodeURIComponent(promptsSelectedId), { method: "PUT", body: body });
        toast("模板已更新", "success");
      } else {
        r = await api("/api/v1/prompts", { method: "POST", body: body });
        toast("模板已创建", "success");
      }
      if (r && r.id) promptsSelectedId = r.id;
      await promptsLoad();
      if (promptsSelectedId) {
        const t = promptsCache.find(function (x) { return x.id === promptsSelectedId; });
        if (t) promptsSelect(t.id);
      }
    } catch (e) {
      toast("保存失败：" + e.message, "error");
    } finally {
      advBtnBusy(btn, false);
    }
  }
  async function promptsDelete() {
    if (!promptsSelectedId) return;
    const id = promptsSelectedId;
    await confirmDialog({
      title: "删除模板", message: "确认删除该 Prompt 模板？此操作不可撤销。",
      okText: "删除", danger: true, icon: "🗑",
      onConfirm: async function () {
        await api("/api/v1/prompts/" + encodeURIComponent(id), { method: "DELETE" });
        toast("已删除模板", "success");
        promptsNew();
        promptsLoad();
      },
    });
  }
  async function promptsRender() {
    const out = document.getElementById("promptTplRenderResult");
    if (!promptsSelectedId) { toast("请先选择或保存模板", "error"); return; }
    const vars = {};
    document.querySelectorAll(".prompt-render-var").forEach(function (inp) {
      vars[inp.getAttribute("data-var")] = inp.value;
    });
    out.innerHTML = advSkeleton(2);
    try {
      const r = await api("/api/v1/prompts/" + encodeURIComponent(promptsSelectedId) + "/render", { method: "POST", body: { variables: vars } });
      let html = "";
      const missing = Array.isArray(r.missing_variables) ? r.missing_variables : [];
      if (missing.length) {
        html += '<div class="verify-banner fail"><span class="verify-icon">⚠</span><div><div class="verify-title">缺少变量</div><div class="verify-sub">' + missing.map(escapeHtml).join(", ") + "</div></div></div>";
      }
      html += '<div class="adv-section-label">渲染结果</div>';
      html += '<div class="tot-answer">' + advMarkdown(r.rendered || "") + "</div>";
      out.innerHTML = html;
    } catch (e) {
      advErrorRetry(out, e.message, promptsRender);
    }
  }
  async function promptsLoadVersions() {
    const out = document.getElementById("promptTplVersionsResult");
    if (!promptsSelectedId) return;
    out.innerHTML = advSkeleton(2);
    try {
      const r = await api("/api/v1/prompts/" + encodeURIComponent(promptsSelectedId) + "/versions");
      const versions = Array.isArray(r) ? r : (Array.isArray(r.versions) ? r.versions : []);
      if (!versions.length) { out.innerHTML = emptyState("📜", "暂无版本历史", "该模板尚无历史版本"); return; }
      let html = '<div class="adv-section-label">版本历史（' + versions.length + "）</div><div class=\"timeline\">";
      versions.forEach(function (v, i) {
        const ver = v.version != null ? v.version : i;
        const ts = v.created_at || v.updated_at || v.timestamp || "";
        html += '<div class="timeline-item"><span class="timeline-dot" style="background:var(--info)"></span>';
        html += '<div><div class="timeline-title">v' + escapeHtml(String(ver)) + "</div>";
        if (ts) html += '<div class="timeline-time">' + escapeHtml(relativeTime(ts)) + "</div>";
        html += "</div></div>";
      });
      html += "</div>";
      out.innerHTML = html;
    } catch (e) {
      advErrorRetry(out, e.message, promptsLoadVersions);
    }
  }

  // ════════════════════ 动画与交互强化（批次3）════════════════════
  // 按钮涟漪效果（事件委托，所有 .btn 自动生效）
  document.addEventListener("click", function (e) {
    const btn = e.target.closest(".btn");
    if (!btn || btn.disabled) return;
    const rect = btn.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.width = ripple.style.height = size + "px";
    ripple.style.left = (e.clientX - rect.left - size / 2) + "px";
    ripple.style.top = (e.clientY - rect.top - size / 2) + "px";
    btn.appendChild(ripple);
    setTimeout(function () { if (ripple.parentNode) ripple.parentNode.removeChild(ripple); }, 650);
  });

  // scroll-reveal：观察 .reveal 元素进入视口时添加 .reveal-visible
  if ("IntersectionObserver" in window) {
    const revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("reveal-visible");
          revealObserver.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -40px 0px", threshold: 0.08 });
    // 暴露给各模块使用：window._observeReveal(el)
    window._observeReveal = function (el) {
      if (el && el.classList) {
        el.classList.add("reveal");
        revealObserver.observe(el);
      }
    };
    // 自动观察所有 panel（延迟以等待视图渲染）
    setTimeout(function () {
      document.querySelectorAll(".panel").forEach(function (p) { revealObserver.observe(p); p.classList.add("reveal"); });
    }, 300);
  } else {
    window._observeReveal = function () { /* no-op */ };
  }

  // 列表交错入场助手：给容器添加 .stagger 类触发子元素依次淡入
  window._staggerIn = function (container) {
    if (!container) return;
    container.classList.remove("stagger");
    // 触发重排以重启动画
    void container.offsetWidth;
    container.classList.add("stagger");
  };

  // ════════════════════ 动画工具函数 (Animation Utilities) ════════════════════
  // 彩带碎片：在 (x, y) 视口坐标发射 count 个彩带，随机颜色与角度，1.2s 后自动清理
  window.confetti = function (x, y, count) {
    count = Math.max(0, parseInt(count, 10) || 0);
    if (count === 0) return;
    const colors = ["#ef4444", "#f59e0b", "#22c55e", "#3b82f6", "#a855f7", "#ec4899", "#14b8a6", "#f97316"];
    const frag = document.createDocumentFragment();
    const pieces = [];
    for (let i = 0; i < count; i++) {
      const piece = document.createElement("div");
      piece.className = "confetti-piece";
      piece.style.animation = "none"; // 自驱动动画以支持随机角度
      piece.style.left = x + "px";
      piece.style.top = y + "px";
      piece.style.background = colors[Math.floor(Math.random() * colors.length)];
      const angle = Math.random() * Math.PI * 2;
      const speed = 80 + Math.random() * 140;
      pieces.push({
        el: piece,
        x: 0, y: 0,
        vx: Math.cos(angle) * speed,
        vy: Math.sin(angle) * speed - 60,
        rot: Math.random() * 360,
        vrot: (Math.random() - 0.5) * 900,
      });
      frag.appendChild(piece);
    }
    document.body.appendChild(frag);
    const startT = performance.now();
    let lastT = startT;
    const duration = 1200;
    function tick(now) {
      const elapsed = now - startT;
      let dt = (now - lastT) / 1000;
      lastT = now;
      if (dt > 0.05) dt = 0.05; // 钳制大间隔，避免跳变
      if (elapsed >= duration) {
        pieces.forEach(function (p) { if (p.el.parentNode) p.el.parentNode.removeChild(p.el); });
        return;
      }
      const gravity = 600;
      pieces.forEach(function (p) {
        p.vy += gravity * dt;
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        p.rot += p.vrot * dt;
        const opacity = Math.max(0, 1 - elapsed / duration);
        p.el.style.transform = "translate(" + p.x.toFixed(1) + "px," + p.y.toFixed(1) + "px) rotate(" + p.rot.toFixed(1) + "deg)";
        p.el.style.opacity = opacity;
      });
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  };

  // 抖动效果：常用于错误提示
  window.shakeEl = function (el) {
    if (!el) return;
    el.classList.remove("shake");
    void el.offsetWidth; // 强制重绘以重启动画
    el.classList.add("shake");
    setTimeout(function () { el.classList.remove("shake"); }, 400);
  };

  // 成功打勾：在 el 内插入打勾标记，1.5s 后移除
  window.successCheck = function (el) {
    if (!el) return;
    const span = document.createElement("span");
    span.className = "success-check";
    el.appendChild(span);
    setTimeout(function () { if (span.parentNode) span.parentNode.removeChild(span); }, 1500);
  };

  // 心跳加载器 HTML 字符串
  window.heartbeatLoader = function () {
    return '<div class="heartbeat-loader"><span class="beat"></span><span class="beat"></span><span class="beat"></span><span class="beat"></span></div>';
  };

  // 微光进度条 HTML 字符串
  window.shimmerBar = function () {
    return '<div class="shimmer-bar"></div>';
  };

  // 弹跳入场：给元素加 .bounce-in 触发弹跳动画
  window.bounceIn = function (el) {
    if (!el) return;
    el.classList.remove("bounce-in");
    void el.offsetWidth;
    el.classList.add("bounce-in");
  };

  // ════════════════════ 功能介绍 Tooltip 系统 ════════════════════
  // 任何元素加 data-tip="标题|内容" 即可 hover 显示气泡
  let tipTimer = null;
  let tipTarget = null;

  document.addEventListener("mouseover", function (e) {
    const el = e.target.closest("[data-tip]");
    if (!el) return;
    tipTarget = el;
    clearTimeout(tipTimer);
    tipTimer = setTimeout(function () { showTip(el); }, 300);
  });
  document.addEventListener("mouseout", function (e) {
    const el = e.target.closest("[data-tip]");
    if (!el) return;
    clearTimeout(tipTimer);
    hideTip();
  });

  function showTip(el) {
    const raw = el.getAttribute("data-tip") || "";
    const parts = raw.split("|");
    const title = parts[0] || "";
    const content = parts.slice(1).join("|") || "";
    const popover = document.getElementById("tipPopover");
    const contentEl = document.getElementById("tipPopoverContent");
    if (!popover || !contentEl) return;
    contentEl.innerHTML = (title ? "<strong>" + escapeHtml(title) + "</strong>" : "") + escapeHtml(content);
    popover.classList.remove("hidden");
    popover.setAttribute("aria-hidden", "false");
    // 智能定位：优先 bottom，视口空间不足则 top；左右越界则夹紧
    const rect = el.getBoundingClientRect();
    const popRect = popover.getBoundingClientRect();
    let placement = "bottom";
    let top = rect.bottom + 8;
    let left = rect.left + rect.width / 2 - popRect.width / 2;
    if (top + popRect.height > window.innerHeight - 8) {
      placement = "top";
      top = rect.top - popRect.height - 8;
    }
    if (left < 8) left = 8;
    if (left + popRect.width > window.innerWidth - 8) left = window.innerWidth - popRect.width - 8;
    if (top < 8) top = 8;
    popover.setAttribute("data-placement", placement);
    popover.style.left = left + "px";
    popover.style.top = top + "px";
  }
  function hideTip() {
    const popover = document.getElementById("tipPopover");
    if (popover) {
      popover.classList.add("hidden");
      popover.setAttribute("aria-hidden", "true");
    }
    tipTarget = null;
  }

  // tipHTML：返回一个可嵌入文档的 ℹ 图标 span，hover 显示解释
  function tipHTML(title, content) {
    const attr = escapeHtml(title + "|" + content);
    return '<span class="tip-trigger" data-tip="' + attr + '" tabindex="0" role="button" aria-label="' + escapeHtml(title) + '">i</span>';
  }
  // addTipToSelector：在指定容器内查找包含 term 的文本节点，并在首次出现处插入 ℹ 图标
  function addTipToSelector(selector, term, title, content) {
    if (!term) return;
    const containers = document.querySelectorAll(selector);
    containers.forEach(function (container) {
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (node.nodeValue && node.nodeValue.indexOf(term) !== -1) return NodeFilter.FILTER_ACCEPT;
          return NodeFilter.FILTER_REJECT;
        },
      });
      const targets = [];
      let n;
      while ((n = walker.nextNode())) targets.push(n);
      targets.forEach(function (node) {
        const text = node.nodeValue;
        const idx = text.indexOf(term);
        if (idx === -1) return;
        const before = text.slice(0, idx);
        const after = text.slice(idx + term.length);
        const parent = node.parentNode;
        if (!parent) return;
        const frag = document.createDocumentFragment();
        frag.appendChild(document.createTextNode(before));
        const termEl = document.createElement("span");
        termEl.textContent = term;
        frag.appendChild(termEl);
        const tip = document.createElement("span");
        tip.className = "tip-trigger";
        tip.setAttribute("data-tip", title + "|" + content);
        tip.setAttribute("tabindex", "0");
        tip.textContent = "i";
        frag.appendChild(tip);
        frag.appendChild(document.createTextNode(after));
        parent.replaceChild(frag, node);
      });
    });
  }
  // addTipTriggers：对指定容器批量注入常见专业术语的 ℹ 图标（默认不匹配任何容器，需显式传选择器）
  window.tipHTML = tipHTML;
  window.addTipToSelector = addTipToSelector;
  window.addTipTriggers = function (selector) {
    if (!selector) return;
    const dict = [
      ["FHIR", "FHIR R4", "医疗数据交换国际标准，由 HL7 组织维护，规范了患者、检验、用药等资源的结构与交互。"],
      ["PHI", "PHI", "受试者健康信息（Protected Health Information），任何可识别个体身份的健康相关数据。"],
      ["LangGraph", "LangGraph", "声明式有向无环图（DAG）编排框架，用于定义智能体任务的执行流程与节点依赖。"],
      ["embedding", "Embedding", "向量嵌入：把文本转换为数字向量，用于计算语义相似度。"],
      ["RLHF", "RLHF", "人类反馈强化学习：用人类对输出的偏好作为奖励信号，优化模型策略。"],
      ["OpenTelemetry", "OpenTelemetry", "开源可观测性标准，统一 traces（追踪）、metrics（指标）、logs（日志）的采集与导出。"],
    ];
    dict.forEach(function (row) { addTipToSelector(selector, row[0], row[1], row[2]); });
  };

  // ════════════════════ 使用文档中心 (Help Center) ════════════════════
  const HELP_DOCS = [
    {
      id: "quickstart", icon: "🚀", label: "快速入门", group: "入门",
      html: '<h2>🚀 快速入门</h2>' +
        '<p>DoctorAgent 控制台是面向医疗场景的智能体工作台，三步即可上手：</p>' +
        '<ol>' +
        '<li><strong>配置 API Token</strong>：在右上角输入框填入 API Token，用于接口鉴权。本地开发环境可留空。</li>' +
        '<li><strong>选择功能页面</strong>：通过顶部导航栏切换到所需模块，例如「智能对话」「临床工作台」。</li>' +
        '<li><strong>开始使用</strong>：在对应页面输入内容并提交，智能体将开始处理并返回结果。</li>' +
        '</ol>' +
        '<h3>首次使用提示</h3>' +
        '<p>随时按 <kbd>Ctrl</kbd>+<kbd>K</kbd> 打开命令面板，快速跳转到任意页面或执行常用动作。</p>' +
        '<h3>健康状态说明</h3>' +
        '<p>右上角的圆点指示后端连接状态：</p>' +
        '<ul>' +
        '<li><span style="color:#22c55e">● 绿色</span>：在线，后端正常响应。</li>' +
        '<li><span style="color:#ef4444">● 红色</span>：离线，后端不可达或异常。</li>' +
        '<li><span style="color:#9ca3af">● 灰色</span>：未检测，尚未发起健康检查。</li>' +
        '</ul>',
    },
    {
      id: "chat", icon: "💬", label: "智能对话", group: "核心功能",
      html: '<h2>💬 智能对话</h2>' +
        '<p>与医疗智能体进行多轮对话，支持上下文记忆、文件上传与联网搜索。</p>' +
        '<h3>发起对话与上传文件</h3>' +
        '<ul>' +
        '<li>在输入框输入问题，按 <kbd>Enter</kbd> 发送，<kbd>Shift</kbd>+<kbd>Enter</kbd> 换行。</li>' +
        '<li>支持上传文本与二进制文件（如 PDF、图片），单文件上限 20MB。</li>' +
        '<li>开启联网搜索后，智能体会先检索最新资料再作答。</li>' +
        '</ul>' +
        '<h3>消息操作</h3>' +
        '<p>悬停消息可执行：复制、删除、重新生成、编辑后重发。</p>' +
        '<h3>系统提示词</h3>' +
        '<p>在「设置中心」配置系统提示词，用于引导 Agent 的角色与行为边界。</p>' +
        '<h3>历史记录</h3>' +
        '<p>对话历史展示在左侧列表，自动保存到浏览器本地，可随时切换或删除会话。</p>',
    },
    {
      id: "clinical", icon: "🩺", label: "临床工作台", group: "核心功能",
      html: '<h2>🩺 临床工作台</h2>' +
        '<p>以结构化患者上下文驱动智能体的临床决策支持。</p>' +
        '<h3>患者上下文</h3>' +
        '<p>使用 JSON 编辑器维护患者基本信息、生命体征、检验结果、用药记录等结构化数据，作为推理输入。</p>' +
        '<h3>临床决策支持</h3>' +
        '<p>智能体基于上下文给出鉴别诊断、检查建议与风险提示，供医师参考复核。</p>' +
        '<h3>可视化</h3>' +
        '<ul>' +
        '<li>生命体征图表：趋势化展示心率、血压、体温等。</li>' +
        '<li>实验室检验可视化：异常值高亮与参考区间对比。</li>' +
        '</ul>' +
        '<h3>数据标准</h3>' +
        '<p>提及 <span class="tip-trigger" data-tip="FHIR R4|医疗数据交换国际标准，由 HL7 组织维护，规范了患者、检验、用药等资源的结构与交互。" tabindex="0">i</span> FHIR R4 标准，确保数据结构可与其他医疗系统互通。</p>',
    },
    {
      id: "phi", icon: "🔒", label: "PHI 脱敏", group: "核心功能",
      html: '<h2>🔒 PHI 脱敏</h2>' +
        '<p>对文本中的受试者健康信息（<span class="tip-trigger" data-tip="PHI|受试者健康信息（Protected Health Information），任何可识别个体身份的健康相关数据。" tabindex="0">i</span> PHI）进行自动识别与脱敏处理。</p>' +
        '<h3>三种脱敏策略</h3>' +
        '<ul>' +
        '<li><strong>redact</strong>：擦除，将敏感内容直接删除或替换为占位符。</li>' +
        '<li><strong>pseudonymize</strong>：假名替换，用一致性映射的虚拟标识替换原值，保留关联关系。</li>' +
        '<li><strong>mask</strong>：掩码，保留部分字符（如 138****5678）以便辨识格式。</li>' +
        '</ul>' +
        '<h3>支持的实体类型</h3>' +
        '<p>手机号、邮箱、身份证号、姓名、地址等可识别个体身份的字段。</p>',
    },
    {
      id: "rules", icon: "🛡️", label: "安全规则引擎", group: "智能体高级",
      html: '<h2>🛡️ 安全规则引擎</h2>' +
        '<p>独立于 LLM 的确定性规则层，提供不可逾越的安全底线。</p>' +
        '<h3>确定性规则</h3>' +
        '<p>规则引擎与模型无关，保证关键校验结果稳定可审计，不受模型幻觉影响。</p>' +
        '<h3>覆盖范围</h3>' +
        '<ul>' +
        '<li>生命体征危急值阈值告警。</li>' +
        '<li>实验室检验异常项识别。</li>' +
        '<li>药物相互作用检测。</li>' +
        '<li>过敏交叉反应提示。</li>' +
        '</ul>' +
        '<h3>严重程度分级</h3>' +
        '<ul>' +
        '<li><strong>critical</strong>：阻断性，必须人工介入。</li>' +
        '<li><strong>warning</strong>：警示，需关注但不阻断。</li>' +
        '<li><strong>info</strong>：提示性参考信息。</li>' +
        '</ul>',
    },
    {
      id: "orchestration", icon: "🤖", label: "智能体编排", group: "智能体高级",
      html: '<h2>🤖 智能体编排</h2>' +
        '<p>基于 <span class="tip-trigger" data-tip="LangGraph|声明式有向无环图（DAG）编排框架，用于定义智能体任务的执行流程与节点依赖。" tabindex="0">i</span> LangGraph 的声明式 DAG（有向无环图，定义任务执行流程与节点依赖）编排。</p>' +
        '<h3>执行流程</h3>' +
        '<ol>' +
        '<li><strong>确定性规则</strong>：先跑安全底线校验。</li>' +
        '<li><strong>三路并行专家</strong>：多个专家节点并发推理。</li>' +
        '<li><strong>聚合</strong>：汇总各专家结论。</li>' +
        '<li><strong>文书</strong>：生成结构化输出文书。</li>' +
        '<li><strong>护栏</strong>：终检与合规过滤。</li>' +
        '</ol>' +
        '<h3>不可变与可审计</h3>' +
        '<p>图编译后不可变，每次执行的节点路径与中间状态均可追溯，满足审计要求。</p>',
    },
    {
      id: "kg", icon: "🌐", label: "知识图谱", group: "智能体高级",
      html: '<h2>🌐 知识图谱</h2>' +
        '<p>从文档中抽取实体与关系，构建可查询的知识网络。</p>' +
        '<h3>实体与关系抽取</h3>' +
        '<p>自动识别疾病、药物、症状、检查等实体，及其作用、并发、禁忌等关系。</p>' +
        '<h3>语义查询与可视化</h3>' +
        '<p>支持自然语言语义查询，并以子图形式可视化结果路径。</p>' +
        '<h3>向量检索</h3>' +
        '<p>借助 <span class="tip-trigger" data-tip="Embedding|向量嵌入：把文本转换为数字向量，用于计算语义相似度。" tabindex="0">i</span> embedding（向量嵌入：把文本转为数字向量用于相似度计算）实现语义级别的实体匹配。</p>',
    },
    {
      id: "memory", icon: "🧠", label: "记忆管理", group: "智能体高级",
      html: '<h2>🧠 记忆管理</h2>' +
        '<p>智能体维护四层记忆体系，模拟人类认知结构。</p>' +
        '<h3>四层记忆</h3>' +
        '<ul>' +
        '<li><strong>短期记忆</strong>：当前会话的即时上下文。</li>' +
        '<li><strong>工作记忆</strong>：当前任务进行中的中间状态。</li>' +
        '<li><strong>情景记忆</strong>：过往交互的具体事件。</li>' +
        '<li><strong>长期记忆</strong>：持久化的事实与知识。</li>' +
        '</ul>' +
        '<h3>记忆类型区别</h3>' +
        '<ul>' +
        '<li><strong>semantic</strong>：语义事实，如「青霉素是抗生素」。</li>' +
        '<li><strong>episodic</strong>：情景记忆，如「上次与某患者的对话」。</li>' +
        '<li><strong>procedural</strong>：程序性知识，如「如何完成某项操作流程」。</li>' +
        '</ul>' +
        '<h3>权重与统计</h3>' +
        '<p>每条记忆带重要性权重与访问统计，用于召回排序与遗忘策略。</p>',
    },
    {
      id: "rlhf", icon: "📈", label: "强化学习反馈", group: "智能体高级",
      html: '<h2>📈 强化学习反馈</h2>' +
        '<p>用户的点赞/点踩作为奖励信号，持续优化智能体行为。</p>' +
        '<h3>反馈信号</h3>' +
        '<ul>' +
        '<li>👍：<strong>+1</strong>，正向奖励。</li>' +
        '<li>😐：<strong>0</strong>，中性，不调整。</li>' +
        '<li>👎：<strong>-1</strong>，负向惩罚。</li>' +
        '</ul>' +
        '<h3>RLHF 简释</h3>' +
        '<p><span class="tip-trigger" data-tip="RLHF|人类反馈强化学习：用人类对输出的偏好作为奖励信号，优化模型策略。" tabindex="0">i</span> RLHF（人类反馈强化学习）：用人类偏好作为奖励信号，优化模型行为。</p>' +
        '<h3>策略进化</h3>' +
        '<p>policy（策略）随累积反馈不断进化，使更受认可的输出模式被强化。</p>',
    },
    {
      id: "obs", icon: "📊", label: "可观测性", group: "运维监控",
      html: '<h2>📊 可观测性</h2>' +
        '<p>基于 <span class="tip-trigger" data-tip="OpenTelemetry|开源可观测性标准，统一 traces（追踪）、metrics（指标）、logs（日志）的采集与导出。" tabindex="0">i</span> OpenTelemetry 的全链路可观测体系。</p>' +
        '<h3>三大支柱</h3>' +
        '<ul>' +
        '<li><strong>traces</strong>：请求追踪，记录单次请求在节点间的调用链与耗时。</li>' +
        '<li><strong>metrics</strong>：指标，聚合后的计数、延迟、吞吐等量化数据。</li>' +
        '<li><strong>logs</strong>：日志，离散事件的结构化记录。</li>' +
        '</ul>' +
        '<h3>健康快照与错误监控</h3>' +
        '<p>提供系统健康快照与错误集中监控，便于快速定位异常。</p>',
    },
    {
      id: "config", icon: "⚙️", label: "配置管理", group: "运维监控",
      html: '<h2>⚙️ 配置管理</h2>' +
        '<p>通过 settings.json 集中管理运行配置。</p>' +
        '<h3>配置文件结构</h3>' +
        '<p>settings.json 采用分层结构组织各模块参数，可在「设置中心」可视化编辑。</p>' +
        '<h3>敏感字段脱敏</h3>' +
        '<p>密钥类字段在界面显示为 <code>***</code>。提交时若字段仍为 <code>***</code>，则保留原值不修改，避免误清空。</p>' +
        '<h3>主密钥提供者</h3>' +
        '<ul>' +
        '<li><strong>FilePassword</strong>：基于文件口令派生。</li>' +
        '<li><strong>env</strong>：从环境变量读取。</li>' +
        '<li><strong>KMS</strong>：对接密钥管理服务托管解密。</li>' +
        '</ul>',
    },
    {
      id: "shortcuts", icon: "⌨️", label: "键盘快捷键", group: "参考",
      html: '<h2>⌨️ 键盘快捷键</h2>' +
        '<div class="help-kbd-row"><kbd>Ctrl</kbd>+<kbd>K</kbd><span>打开命令面板</span></div>' +
        '<div class="help-kbd-row"><kbd>Alt</kbd>+<kbd>1</kbd>…<kbd>9</kbd><span>切换前 9 个功能页面</span></div>' +
        '<div class="help-kbd-row"><kbd>Ctrl</kbd>+<kbd>Enter</kbd><span>在对话页发送消息</span></div>' +
        '<div class="help-kbd-row"><kbd>Esc</kbd><span>关闭当前弹窗 / 取消焦点</span></div>' +
        '<div class="help-kbd-row"><kbd>?</kbd><span>打开使用文档中心</span></div>',
    },
    {
      id: "faq", icon: "❓", label: "常见问题 FAQ", group: "参考",
      html: '<h2>❓ 常见问题 FAQ</h2>' +
        '<h3>Q：Token 是什么？</h3><p>A：用于接口鉴权的 API 密钥。本地开发可留空。</p>' +
        '<h3>Q：数据存在哪里？</h3><p>A：敏感配置由 Vault 加密存储；对话历史保存在浏览器本地。</p>' +
        '<h3>Q：如何切换主题？</h3><p>A：点击右上角月亮图标，或在命令面板搜索「主题」。</p>' +
        '<h3>Q：文件上传失败？</h3><p>A：检查文件大小（需小于 20MB）以及 Token 是否有效。</p>' +
        '<h3>Q：流式回复中断？</h3><p>A：可能是 LLM 后端连接失败，请检查右上角健康状态。</p>',
    },
  ];

  let helpActiveId = HELP_DOCS[0].id;

  function renderHelpNav() {
    const nav = document.getElementById("helpNav");
    if (!nav) return;
    const groups = [];
    const map = {};
    HELP_DOCS.forEach(function (d) {
      if (!map[d.group]) { map[d.group] = []; groups.push(d.group); }
      map[d.group].push(d);
    });
    let html = "";
    groups.forEach(function (g) {
      html += '<div class="help-nav-group-label">' + escapeHtml(g) + "</div>";
      map[g].forEach(function (d) {
        html += '<button class="help-nav-item' + (d.id === helpActiveId ? " active" : "") +
          '" data-doc="' + escapeHtml(d.id) + '" role="tab" aria-selected="' + (d.id === helpActiveId ? "true" : "false") + '">' +
          '<span class="help-nav-icon" aria-hidden="true">' + d.icon + "</span>" +
          "<span>" + escapeHtml(d.label) + "</span></button>";
      });
    });
    nav.innerHTML = html;
  }

  function renderHelpDoc(id) {
    const doc = HELP_DOCS.find(function (d) { return d.id === id; }) || HELP_DOCS[0];
    helpActiveId = doc.id;
    const content = document.getElementById("helpContent");
    if (!content) return;
    content.innerHTML = doc.html;
    // 触发 helpContentIn 动画：移除再添加（通过重置 animation 属性 + 强制重绘）
    content.style.animation = "none";
    void content.offsetWidth;
    content.style.animation = "";
    // 同步侧边栏 active 状态
    document.querySelectorAll(".help-nav-item").forEach(function (b) {
      const on = b.dataset.doc === doc.id;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function openHelpCenter() {
    if (!helpModal) return;
    helpModal.classList.remove("hidden");
    renderHelpNav();
    renderHelpDoc(helpActiveId);
  }
  function closeHelpCenter() {
    if (!helpModal) return;
    helpModal.classList.add("hidden");
  }

  if (helpBtn) {
    helpBtn.addEventListener("click", openHelpCenter);
  }
  const helpCloseBtn = document.getElementById("helpCloseBtn");
  if (helpCloseBtn) {
    helpCloseBtn.addEventListener("click", closeHelpCenter);
  }
  if (helpModal) {
    helpModal.addEventListener("click", function (e) {
      if (e.target === helpModal) closeHelpCenter();
    });
  }
  const helpNavEl = document.getElementById("helpNav");
  if (helpNavEl) {
    helpNavEl.addEventListener("click", function (e) {
      const btn = e.target.closest(".help-nav-item");
      if (!btn) return;
      renderHelpDoc(btn.dataset.doc);
    });
  }

  // ════════════════════ 首次使用引导 (Onboarding Tour) ════════════════════
  const ONBOARD_KEY = "doctoragent_onboarded";
  let onboardStep = 0;
  const ONBOARD_STEPS = [
    { icon: "👋", title: "欢迎使用 DoctorAgent", text: "欢迎使用 DoctorAgent，医疗智能体控制台。30 秒了解核心功能。", target: null },
    { icon: "📝", title: "配置 API Token", text: "在这里配置 API Token 用于鉴权。本地开发可留空。", target: "#tokenInput" },
    { icon: "🧭", title: "侧边栏", text: "左侧导航栏切换功能页面。共 20+ 个模块。", target: ".sidebar" },
    { icon: "⌘", title: "命令面板", text: "按 Ctrl+K 快速跳转任意页面或执行命令。", target: "#cmdPaletteBtn" },
    { icon: "📖", title: "帮助文档", text: "随时点击 ? 查看完整使用文档。开始探索吧！", target: "#helpBtn" },
  ];
  let onboardResizeHandler = null;

  function renderOnboardDots() {
    const dots = document.getElementById("onboardDots");
    if (!dots) return;
    let html = "";
    for (let i = 0; i < ONBOARD_STEPS.length; i++) {
      html += '<span class="onboard-dot' + (i === onboardStep ? " active" : "") + '" data-step="' + i + '"></span>';
    }
    dots.innerHTML = html;
  }

  function positionOnboard() {
    const step = ONBOARD_STEPS[onboardStep];
    const spotlight = document.getElementById("onboardSpotlight");
    const card = document.getElementById("onboardCard");
    if (!spotlight || !card) return;
    if (step.target) {
      const targetEl = document.querySelector(step.target);
      if (targetEl) {
        const r = targetEl.getBoundingClientRect();
        const pad = 4;
        spotlight.style.left = (r.left - pad) + "px";
        spotlight.style.top = (r.top - pad) + "px";
        spotlight.style.width = (r.width + pad * 2) + "px";
        spotlight.style.height = (r.height + pad * 2) + "px";
        spotlight.style.boxShadow = "0 0 0 9999px rgba(0,0,0,0.65)";
        // 卡片优先置于目标下方，空间不足则上方
        const cardRect = card.getBoundingClientRect();
        let top = r.bottom + 12;
        if (top + cardRect.height > window.innerHeight - 12) {
          top = r.top - cardRect.height - 12;
          if (top < 12) top = 12;
        }
        let left = r.left + r.width / 2 - cardRect.width / 2;
        if (left < 12) left = 12;
        if (left + cardRect.width > window.innerWidth - 12) left = window.innerWidth - cardRect.width - 12;
        card.style.left = left + "px";
        card.style.top = top + "px";
        card.style.transform = "none";
        return;
      }
    }
    // 无目标元素或目标不存在：全屏变暗，卡片居中
    spotlight.style.left = "-100px";
    spotlight.style.top = "-100px";
    spotlight.style.width = "0px";
    spotlight.style.height = "0px";
    spotlight.style.boxShadow = "0 0 0 9999px rgba(0,0,0,0.65)";
    card.style.left = "50%";
    card.style.top = "50%";
    card.style.transform = "translate(-50%, -50%)";
  }

  function renderOnboardStep() {
    const step = ONBOARD_STEPS[onboardStep];
    const stepEl = document.getElementById("onboardStep");
    const iconEl = document.getElementById("onboardIcon");
    const titleEl = document.getElementById("onboardTitle");
    const textEl = document.getElementById("onboardText");
    const prevBtn = document.getElementById("onboardPrevBtn");
    const nextBtn = document.getElementById("onboardNextBtn");
    if (stepEl) stepEl.textContent = (onboardStep + 1) + " / " + ONBOARD_STEPS.length;
    if (iconEl) iconEl.textContent = step.icon;
    if (titleEl) titleEl.textContent = step.title;
    if (textEl) textEl.textContent = step.text;
    if (prevBtn) prevBtn.disabled = (onboardStep === 0);
    if (nextBtn) nextBtn.textContent = (onboardStep === ONBOARD_STEPS.length - 1) ? "完成" : "下一步";
    renderOnboardDots();
    // 双重 rAF：确保 DOM 更新后再读取卡片尺寸定位
    requestAnimationFrame(function () { requestAnimationFrame(positionOnboard); });
  }

  function startOnboarding() {
    onboardStep = 0;
    const overlay = document.getElementById("onboardOverlay");
    if (!overlay) return;
    overlay.classList.remove("hidden");
    renderOnboardStep();
    if (!onboardResizeHandler) {
      onboardResizeHandler = function () {
        if (overlay.classList.contains("hidden")) return;
        positionOnboard();
      };
      window.addEventListener("resize", onboardResizeHandler);
      window.addEventListener("scroll", onboardResizeHandler, true);
    }
  }
  window.startOnboarding = startOnboarding;

  function finishOnboarding() {
    const overlay = document.getElementById("onboardOverlay");
    if (overlay) overlay.classList.add("hidden");
    try { localStorage.setItem(ONBOARD_KEY, "1"); } catch (e) { /* ignore */ }
    if (onboardResizeHandler) {
      window.removeEventListener("resize", onboardResizeHandler);
      window.removeEventListener("scroll", onboardResizeHandler, true);
      onboardResizeHandler = null;
    }
  }

  function nextOnboard() {
    if (onboardStep < ONBOARD_STEPS.length - 1) {
      onboardStep++;
      renderOnboardStep();
    } else {
      finishOnboarding();
    }
  }
  function prevOnboard() {
    if (onboardStep > 0) {
      onboardStep--;
      renderOnboardStep();
    }
  }

  const onboardPrevBtnEl = document.getElementById("onboardPrevBtn");
  const onboardNextBtnEl = document.getElementById("onboardNextBtn");
  const onboardSkipBtnEl = document.getElementById("onboardSkipBtn");
  const onboardDotsEl = document.getElementById("onboardDots");
  if (onboardPrevBtnEl) onboardPrevBtnEl.addEventListener("click", prevOnboard);
  if (onboardNextBtnEl) onboardNextBtnEl.addEventListener("click", nextOnboard);
  if (onboardSkipBtnEl) onboardSkipBtnEl.addEventListener("click", finishOnboarding);
  if (onboardDotsEl) {
    onboardDotsEl.addEventListener("click", function (e) {
      const dot = e.target.closest(".onboard-dot");
      if (!dot) return;
      const idx = parseInt(dot.dataset.step, 10);
      if (!isNaN(idx) && idx >= 0 && idx < ONBOARD_STEPS.length) {
        onboardStep = idx;
        renderOnboardStep();
      }
    });
  }

  // 页面加载后自动启动引导（仅首次）
  setTimeout(function () {
    let onboarded = false;
    try { onboarded = localStorage.getItem(ONBOARD_KEY) === "1"; } catch (e) { /* ignore */ }
    if (!onboarded) {
      startOnboarding();
    }
  }, 800);

  // ════════════════════ 命令面板 (Command Palette) + 键盘快捷键 ════════════════════
  const cmdPalette = document.getElementById("cmdPalette");
  const cmdInput = document.getElementById("cmdInput");
  const cmdList = document.getElementById("cmdList");
  let cmdItems = [];          // 当前过滤后的命令列表
  let cmdSelected = 0;        // 当前选中索引
  let cmdLastFocus = null;    // 打开前的焦点元素，关闭后恢复

  // 构建命令清单：导航 + 动作
  function buildCommands() {
    const cmds = [];
    // —— 页面导航（从 tab 元素动态构建） ——
    document.querySelectorAll(".sidebar-item[data-view]").forEach(function (tab) {
      const view = tab.dataset.view;
      const label = tab.textContent.trim();
      cmds.push({
        id: "goto:" + view,
        label: "前往 " + label,
        hint: "页面导航",
        icon: "↗",
        keywords: label + " " + view + " page navigate goto " + view,
        run: function () { switchTab(view); },
      });
    });
    // —— 动作命令 ——
    cmds.push({ id: "theme-toggle", label: "切换主题（亮/暗）", hint: "动作", icon: "🌗", keywords: "theme dark light 主题 切换", run: function () { document.getElementById("themeToggle").click(); } });
    cmds.push({ id: "focus-token", label: "聚焦 API Token 输入", hint: "动作", icon: "🔑", keywords: "token auth api key 鉴权 登录", run: function () { const t = document.getElementById("tokenInput"); if (t) { t.focus(); t.select(); } } });
    cmds.push({ id: "refresh-health", label: "刷新健康状态", hint: "动作", icon: "🔄", keywords: "health refresh 健康 在线 status", run: function () { if (typeof checkHealth === "function") checkHealth(); } });
    cmds.push({ id: "open-chat", label: "新建对话", hint: "动作", icon: "💬", keywords: "chat new conversation 对话 新建", run: function () { switchTab("chat"); const nb = document.getElementById("newChatBtn"); if (nb) nb.click(); else if (typeof createChatSession === "function") createChatSession(); } });
    cmds.push({ id: "open-settings", label: "打开设置中心", hint: "动作", icon: "⚙️", keywords: "settings config 设置 配置", run: function () { switchTab("settings"); } });
    cmds.push({ id: "open-memory", label: "记忆管理", hint: "动作", icon: "🧠", keywords: "memory 记忆 facts recall", run: function () { switchTab("mem"); } });
    cmds.push({ id: "open-hooks", label: "生命周期钩子", hint: "动作", icon: "🪝", keywords: "hooks 生命周期 钩子 hook", run: function () { switchTab("hooks"); } });
    cmds.push({ id: "open-obs", label: "可观测性", hint: "动作", icon: "📊", keywords: "observability traces logs metrics 可观测性", run: function () { switchTab("obs"); } });
    cmds.push({ id: "open-kg", label: "知识图谱", hint: "动作", icon: "🌐", keywords: "knowledge graph 知识图谱 kg", run: function () { switchTab("kg"); } });
    return cmds;
  }

  function openCmdPalette() {
    if (!cmdPalette) return;
    cmdLastFocus = document.activeElement;
    cmdPalette.classList.remove("hidden");
    cmdPalette.classList.add("cmd-show");
    cmdInput.value = "";
    renderCmdList("");
    // 延迟聚焦以触发过渡动画
    requestAnimationFrame(function () { cmdInput.focus(); });
  }
  function closeCmdPalette() {
    if (!cmdPalette || cmdPalette.classList.contains("hidden")) return;
    cmdPalette.classList.add("cmd-leaving");
    setTimeout(function () {
      cmdPalette.classList.add("hidden");
      cmdPalette.classList.remove("cmd-show", "cmd-leaving");
      if (cmdLastFocus && typeof cmdLastFocus.focus === "function") {
        try { cmdLastFocus.focus(); } catch (e) { /* ignore */ }
      }
    }, 160);
  }
  function renderCmdList(query) {
    const all = buildCommands();
    const q = (query || "").trim().toLowerCase();
    let filtered;
    if (!q) {
      filtered = all;
    } else {
      filtered = all.filter(function (c) {
        return c.keywords.toLowerCase().indexOf(q) !== -1 || c.label.toLowerCase().indexOf(q) !== -1;
      });
    }
    // 简单评分排序：label 完全包含 > 开头匹配 > 关键词包含
    if (q) {
      filtered.sort(function (a, b) {
        const sa = scoreCmd(a, q);
        const sb = scoreCmd(b, q);
        return sb - sa;
      });
    }
    cmdItems = filtered.slice(0, 30);
    cmdSelected = 0;
    let html = "";
    if (cmdItems.length === 0) {
      html = '<li class="cmd-empty" role="option" aria-disabled="true">无匹配命令</li>';
    } else {
      cmdItems.forEach(function (c, i) {
        html += '<li class="cmd-item' + (i === 0 ? " active" : "") + '" role="option" data-idx="' + i + '" aria-selected="' + (i === 0 ? "true" : "false") + '">' +
          '<span class="cmd-item-icon" aria-hidden="true">' + c.icon + "</span>" +
          '<span class="cmd-item-label">' + escapeHtml(c.label) + "</span>" +
          '<span class="cmd-item-hint">' + escapeHtml(c.hint) + "</span>" +
          "</li>";
      });
    }
    cmdList.innerHTML = html;
  }
  function scoreCmd(c, q) {
    const label = c.label.toLowerCase();
    if (label === q) return 100;
    if (label.indexOf(q) === 0) return 80;
    if (label.indexOf(q) !== -1) return 60;
    if (c.keywords.toLowerCase().indexOf(q) !== -1) return 40;
    return 0;
  }
  function moveCmdSelection(delta) {
    if (cmdItems.length === 0) return;
    cmdSelected = (cmdSelected + delta + cmdItems.length) % cmdItems.length;
    const items = cmdList.querySelectorAll(".cmd-item");
    items.forEach(function (el, i) {
      const active = i === cmdSelected;
      el.classList.toggle("active", active);
      el.setAttribute("aria-selected", active ? "true" : "false");
    });
    // 滚动可见
    const activeEl = items[cmdSelected];
    if (activeEl && activeEl.scrollIntoView) {
      activeEl.scrollIntoView({ block: "nearest" });
    }
  }
  function executeCmd() {
    if (cmdItems.length === 0) return;
    const c = cmdItems[cmdSelected];
    closeCmdPalette();
    if (c && typeof c.run === "function") {
      try { c.run(); } catch (e) { toast("命令执行失败：" + e.message, "error"); }
    }
  }

  // 绑定命令面板事件
  if (cmdPaletteBtn) {
    cmdPaletteBtn.addEventListener("click", openCmdPalette);
  }
  if (cmdInput) {
    cmdInput.addEventListener("input", function () { renderCmdList(cmdInput.value); });
    cmdInput.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveCmdSelection(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveCmdSelection(-1); }
      else if (e.key === "Enter") { e.preventDefault(); executeCmd(); }
      else if (e.key === "Escape") { e.preventDefault(); closeCmdPalette(); }
    });
  }
  // 点击列表项执行
  if (cmdList) {
    cmdList.addEventListener("mouseover", function (e) {
      const li = e.target.closest(".cmd-item");
      if (!li) return;
      const idx = parseInt(li.dataset.idx, 10);
      if (!isNaN(idx)) {
        cmdSelected = idx;
        cmdList.querySelectorAll(".cmd-item").forEach(function (el, i) {
          el.classList.toggle("active", i === idx);
          el.setAttribute("aria-selected", i === idx ? "true" : "false");
        });
      }
    });
    cmdList.addEventListener("click", function (e) {
      const li = e.target.closest(".cmd-item");
      if (!li) return;
      const idx = parseInt(li.dataset.idx, 10);
      if (!isNaN(idx)) { cmdSelected = idx; executeCmd(); }
    });
  }
  // 点击遮罩关闭
  if (cmdPalette) {
    cmdPalette.addEventListener("click", function (e) {
      if (e.target === cmdPalette) closeCmdPalette();
    });
  }

  // ── 全局键盘快捷键 ──
  document.addEventListener("keydown", function (e) {
    // Ctrl/Cmd + K → 命令面板
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (cmdPalette && cmdPalette.classList.contains("hidden")) openCmdPalette();
      else closeCmdPalette();
      return;
    }
    // 命令面板打开时，不处理其他快捷键
    if (cmdPalette && !cmdPalette.classList.contains("hidden")) return;
    // 在输入框/textarea 中时不触发导航快捷键（除非含 Ctrl/Cmd）
    const tag = (e.target.tagName || "").toUpperCase();
    const isTyping = tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable;
    if (isTyping && !e.ctrlKey && !e.metaKey && !e.altKey) return;

    // Esc → 关闭任何打开的弹窗 / 取消焦点
    if (e.key === "Escape") {
      const openModal = document.querySelector(".modal-mask:not(.hidden)");
      if (openModal) {
        const cancelBtn = openModal.querySelector('[id$="Cancel"], .modal-actions .btn-ghost');
        if (cancelBtn) cancelBtn.click();
        return;
      }
    }
    // ? → 打开帮助中心（非输入框聚焦时；Shift+/ 产生 ?）
    if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (helpModal && helpModal.classList.contains("hidden") && typeof openHelpCenter === "function") {
        e.preventDefault();
        openHelpCenter();
      }
      return;
    }
    // Ctrl/Cmd + / → 命令面板（备用）或打开帮助
    if ((e.ctrlKey || e.metaKey) && e.key === "/") {
      e.preventDefault();
      openCmdPalette();
      return;
    }
    // Alt + 数字 1..9 → 快速切换前 9 个 tab
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      const n = parseInt(e.key, 10);
      if (!isNaN(n) && n >= 1 && n <= 9) {
        const tabs = document.querySelectorAll(".sidebar-item[data-view]");
        if (tabs[n - 1]) {
          e.preventDefault();
          switchTab(tabs[n - 1].dataset.view);
        }
      }
    }
    // Ctrl/Cmd + Enter → 在对话页发送消息
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      if (typeof sendChatMessage === "function" && document.getElementById("view-chat").classList.contains("active")) {
        e.preventDefault();
        sendChatMessage();
      }
    }
  });

  // ════════════════════ 合规管理（首次使用弹窗 / 状态面板 / 教程查看器） ════════════════════
  let complianceData = null;

  async function loadComplianceData() {
    try {
      const res = await fetch("/api/v1/compliance/warning");
      const data = await res.json();
      complianceData = data;
      return data;
    } catch (e) {
      console.error("加载合规数据失败:", e);
      return null;
    }
  }

  function showComplianceWarningModal(data) {
    if (!data || !data.should_show) return;
    // 检查是否已经显示过（sessionStorage 避免每次刷新都弹）
    if (sessionStorage.getItem("compliance_warning_shown")) return;

    const blockingItems = data.blocking_items || [];
    if (blockingItems.length === 0) return;

    // 构建弹窗内容
    const itemsHtml = blockingItems.map(function (item) {
      return '<div class="compliance-warning-item">' +
        '<div class="compliance-warning-icon">⚠️</div>' +
        '<div class="compliance-warning-content">' +
          '<h4>' + item.name + '</h4>' +
          '<p>' + item.description + '</p>' +
          '<div class="compliance-warning-meta">' +
            '<span class="meta-tag">📅 ' + item.estimated_duration + '</span>' +
            '<span class="meta-tag">🏛️ ' + item.authority + '</span>' +
          '</div>' +
          '<div class="compliance-warning-risk">' + item.risk_if_missing + '</div>' +
          '<button class="btn-tutorial" data-item-id="' + item.id + '">查看教程 →</button>' +
        '</div>' +
      '</div>';
    }).join("");

    const modalHtml =
      '<div class="modal-overlay compliance-modal-overlay" id="complianceModal">' +
        '<div class="modal compliance-modal">' +
          '<div class="modal-header">' +
            '<h2>🔒 合规提示</h2>' +
            '<button class="modal-close" onclick="closeComplianceModal()">✕</button>' +
          '</div>' +
          '<div class="modal-body">' +
            '<div class="compliance-alert">' +
              '<p>您的系统尚有 <strong>' + blockingItems.length + ' 项</strong>必须完成的合规资质未办理。</p>' +
              '<p>在取得相应资质前，本产品不作为医疗器械销售或使用。</p>' +
              '<p>请按照以下教程完成合规注册：</p>' +
            '</div>' +
            itemsHtml +
          '</div>' +
          '<div class="modal-footer">' +
            '<label class="compliance-dont-show">' +
              '<input type="checkbox" id="dontShowCompliance"> 本次会话不再提醒' +
            '</label>' +
            '<button class="btn-primary" onclick="closeComplianceModal()">我知道了</button>' +
          '</div>' +
        '</div>' +
      '</div>';

    // 移除已有弹窗
    const existing = document.getElementById("complianceModal");
    if (existing) existing.remove();

    // 添加弹窗
    const div = document.createElement("div");
    div.innerHTML = modalHtml;
    document.body.appendChild(div.firstElementChild);

    // 绑定教程按钮
    document.querySelectorAll("#complianceModal .btn-tutorial").forEach(function (btn) {
      btn.addEventListener("click", function () {
        openComplianceTutorial(this.dataset.itemId);
      });
    });

    // 不再提醒
    const dontShowChk = document.getElementById("dontShowCompliance");
    if (dontShowChk) {
      dontShowChk.addEventListener("change", function () {
        if (this.checked) {
          sessionStorage.setItem("compliance_warning_shown", "1");
        } else {
          sessionStorage.removeItem("compliance_warning_shown");
        }
      });
    }
  }

  function closeComplianceModal() {
    const modal = document.getElementById("complianceModal");
    if (modal) modal.remove();
    sessionStorage.setItem("compliance_warning_shown", "1");
  }

  async function openComplianceTutorial(itemId) {
    // 先关闭合规弹窗
    closeComplianceModal();

    try {
      const res = await fetch("/api/v1/compliance/tutorial/" + itemId);
      const data = await res.json();

      // 构建教程查看器弹窗
      const item = data.item;
      const content = data.content;

      // 将 markdown 转为简单的 HTML（不需要完整 markdown 解析器，做基本转换即可）
      const htmlContent = simpleMarkdownToHtml(content);

      const modalHtml =
        '<div class="modal-overlay" id="tutorialModal">' +
          '<div class="modal tutorial-modal">' +
            '<div class="modal-header">' +
              '<h2>' + item.name + ' — 办理教程</h2>' +
              '<button class="modal-close" onclick="closeTutorialModal()">✕</button>' +
            '</div>' +
            '<div class="modal-body tutorial-body">' +
              '<div class="tutorial-info-bar">' +
                '<div class="info-item"><span class="info-label">法律依据</span><span class="info-value">' + item.legal_basis + '</span></div>' +
                '<div class="info-item"><span class="info-label">办理机构</span><span class="info-value">' + item.authority + '</span></div>' +
                '<div class="info-item"><span class="info-label">预计周期</span><span class="info-value">' + item.estimated_duration + '</span></div>' +
              '</div>' +
              '<div class="tutorial-risk-box">' +
                '<strong>⚠️ 不办理的风险：</strong>' + item.risk_if_missing +
              '</div>' +
              '<div class="tutorial-content">' + htmlContent + '</div>' +
            '</div>' +
            '<div class="modal-footer">' +
              '<button class="btn-secondary" onclick="closeTutorialModal()">关闭</button>' +
              '<button class="btn-primary" onclick="markComplianceStarted(\'' + itemId + '\')">标记为办理中</button>' +
            '</div>' +
          '</div>' +
        '</div>';

      const div = document.createElement("div");
      div.innerHTML = modalHtml;
      document.body.appendChild(div.firstElementChild);
    } catch (e) {
      console.error("加载教程失败:", e);
      alert("加载教程失败，请稍后重试");
    }
  }

  function closeTutorialModal() {
    const modal = document.getElementById("tutorialModal");
    if (modal) modal.remove();
  }

  async function markComplianceStarted(itemId) {
    try {
      await fetch("/api/v1/compliance/" + itemId + "/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "in_progress", notes: "已开始办理" }),
      });
      closeTutorialModal();
      loadCompliancePanel(); // 刷新面板
      alert("已标记为\"办理中\"，请在完成后更新状态");
    } catch (e) {
      alert("更新状态失败");
    }
  }

  // 简单的 Markdown 转 HTML（不需要完整的 markdown 解析器）
  function simpleMarkdownToHtml(md) {
    if (!md) return "";
    let html = md
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/^- \[ \] (.+)$/gm, '<div class="checkbox-item">☐ $1</div>')
      .replace(/^- \[x\] (.+)$/gmi, '<div class="checkbox-item done">☑ $1</div>')
      .replace(/^- (.+)$/gm, '<li>$1</li>')
      .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
      .replace(/```[\s\S]*?```/g, function (m) { return '<pre>' + m.replace(/```/g, '') + '</pre>'; })
      .replace(/\n\n/g, '</p><p>')
      .replace(/\n/g, '<br>');
    return '<p>' + html + '</p>';
  }

  // 加载合规面板数据
  async function loadCompliancePanel() {
    try {
      const res = await fetch("/api/v1/compliance/status");
      const data = await res.json();

      const items = data.items || [];
      const summary = data.summary || {};

      // 更新概览
      const elMustTotal = document.getElementById("complianceMustTotal");
      const elMustMissing = document.getElementById("complianceMustMissing");
      const elCompleted = document.getElementById("complianceCompleted");
      const elRate = document.getElementById("complianceRate");
      if (elMustTotal) elMustTotal.textContent = summary.must_total || 0;
      if (elMustMissing) elMustMissing.textContent = summary.must_missing || 0;
      if (elCompleted) elCompleted.textContent = summary.completed || 0;
      if (elRate) elRate.textContent = (summary.completion_rate || 0) + "%";

      // 渲染合规项列表
      const listHtml = items.map(function (item) {
        const statusClass = item.status === "completed" ? "status-success" :
          item.status === "in_progress" ? "status-warning" :
          item.status === "not_required" ? "status-muted" : "status-danger";
        const statusText = {
          "not_started": "未开始",
          "in_progress": "办理中",
          "completed": "已完成",
          "not_required": "不适用",
        }[item.status] || item.status;
        const categoryClass = item.category === "must" ? "cat-must" :
          item.category === "conditional" ? "cat-conditional" : "cat-recommended";
        const categoryText = { must: "必须", conditional: "条件性", recommended: "建议" }[item.category] || item.category;

        return '<div class="compliance-card ' + statusClass + '">' +
          '<div class="card-header">' +
            '<h3>' + item.name + '</h3>' +
            '<span class="badge ' + categoryClass + '">' + categoryText + '</span>' +
          '</div>' +
          '<p class="card-desc">' + item.description + '</p>' +
          '<div class="card-meta">' +
            '<span>📅 ' + item.estimated_duration + '</span>' +
            '<span>🏛️ ' + item.authority + '</span>' +
          '</div>' +
          '<div class="card-status">' +
            '<span class="status-badge ' + statusClass + '">' + statusText + '</span>' +
          '</div>' +
          '<div class="card-actions">' +
            '<button class="btn-tutorial" onclick="openComplianceTutorial(\'' + item.id + '\')">📖 查看教程</button>' +
            '<select class="status-select" onchange="updateComplianceStatus(\'' + item.id + '\', this.value)" title="更新状态">' +
              '<option value="not_started"' + (item.status === "not_started" ? " selected" : "") + '>未开始</option>' +
              '<option value="in_progress"' + (item.status === "in_progress" ? " selected" : "") + '>办理中</option>' +
              '<option value="completed"' + (item.status === "completed" ? " selected" : "") + '>已完成</option>' +
              '<option value="not_required"' + (item.status === "not_required" ? " selected" : "") + '>不适用</option>' +
            '</select>' +
          '</div>' +
        '</div>';
      }).join("");

      const listEl = document.getElementById("complianceList");
      if (listEl) listEl.innerHTML = listHtml;
    } catch (e) {
      console.error("加载合规面板失败:", e);
    }
  }

  async function updateComplianceStatus(itemId, status) {
    try {
      await fetch("/api/v1/compliance/" + itemId + "/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: status, notes: "" }),
      });
      loadCompliancePanel(); // 刷新
    } catch (e) {
      alert("更新失败");
    }
  }

  // 暴露给 inline onclick/onchange 的函数
  window.closeComplianceModal = closeComplianceModal;
  window.openComplianceTutorial = openComplianceTutorial;
  window.closeTutorialModal = closeTutorialModal;
  window.markComplianceStarted = markComplianceStarted;
  window.updateComplianceStatus = updateComplianceStatus;

  // 页面加载后检查合规状态（首次使用弹窗）
  function initComplianceWarningCheck() {
    // 延迟检查，避免与页面初始化冲突
    setTimeout(async function () {
      const data = await loadComplianceData();
      if (data) {
        showComplianceWarningModal(data);
      }
    }, 2000);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initComplianceWarningCheck);
  } else {
    initComplianceWarningCheck();
  }

  // ════════════════════ 启动收尾 ════════════════════
  // 所有 let/const 声明已完成，现在安全地初始化默认视图。
  // 先初始化对话（chat 是默认激活视图），再切换到 URL hash 指定的视图。
  initChat();
  if (initialView && initialView !== "chat" && document.querySelector('.sidebar-item[data-view="' + initialView + '"]')) {
    switchTab(initialView, false);
  }

  // ── 返回顶部按钮 ──
  (function () {
    const backToTopBtn = document.getElementById("backToTopBtn");
    if (!backToTopBtn) return;
    window.addEventListener("scroll", debounce(function () {
      if (window.scrollY > 300) backToTopBtn.classList.remove("hidden");
      else backToTopBtn.classList.add("hidden");
    }, 100));
    backToTopBtn.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  })();

  // ── 侧边栏搜索过滤 ──
  (function initSidebarSearch() {
    var input = document.getElementById("sidebarSearch");
    if (!input) return;
    input.addEventListener("input", function () {
      var q = this.value.toLowerCase().trim();
      var items = document.querySelectorAll(".sidebar-item");
      var sections = document.querySelectorAll(".sidebar-section");
      items.forEach(function (item) {
        var text = (item.textContent || "").toLowerCase();
        item.classList.toggle("filtered", q && text.indexOf(q) === -1);
      });
      sections.forEach(function (sec) {
        // 隐藏没有任何可见 items 的分组标题
        var next = sec.nextElementSibling;
        var allHidden = true;
        while (next && next.classList.contains("sidebar-item")) {
          if (!next.classList.contains("filtered")) { allHidden = false; break; }
          next = next.nextElementSibling;
        }
        sec.classList.toggle("filtered", q && allHidden);
      });
    });
  })();

  // ── 侧边栏折叠切换 ──
  (function initSidebarToggle() {
    var sidebar = document.querySelector(".sidebar");
    var toggleBtn = document.getElementById("sidebarToggleBtn");
    if (!sidebar || !toggleBtn) return;
    try {
      if (localStorage.getItem("doctoragent_sidebar_collapsed") === "1") {
        sidebar.classList.add("collapsed");
        toggleBtn.textContent = "▶";
      }
    } catch (e) {}
    toggleBtn.addEventListener("click", function () {
      sidebar.classList.toggle("collapsed");
      var isCollapsed = sidebar.classList.contains("collapsed");
      toggleBtn.textContent = isCollapsed ? "▶" : "☰";
      try { localStorage.setItem("doctoragent_sidebar_collapsed", isCollapsed ? "1" : "0"); } catch (e) {}
    });
  })();

  // ── 企业平台 (M14) ─────────────────────────────────────────────
  const ENT_PREFIX = "/api/v1/enterprise";
  async function entApi(path, opts) {
    return await api(ENT_PREFIX + path, opts || {});
  }

  window.enterpriseLoad = async function () {
    try {
      const s = await entApi("/status");
      document.getElementById("entOrgCount").textContent = s.orgs;
      document.getElementById("entUserCount").textContent = s.users;
      document.getElementById("entAnnCount").textContent = s.announcements;
      document.getElementById("entMaintenance").textContent = s.maintenance ? "是" : "否";
    } catch (e) { console.error("enterprise status", e); }
    try {
      const orgs = await entApi("/orgs");
      const list = document.getElementById("entOrgList");
      list.innerHTML = (orgs.items || []).map(function (o) {
        return '<div class="ent-row"><b>' + esc(o.name) + '</b> <span>' + o.id + '</span> <em>' + o.status + '</em></div>';
      }).join("");
    } catch (e) {}
  };

  window.enterpriseCreateOrg = async function () {
    const name = document.getElementById("entOrgName").value.trim();
    if (!name) { alert("请输入组织名称"); return; }
    try {
      const org = await entApi("/orgs", { method: "POST", body: { name: name } });
      alert("创建成功: " + org.id);
      window.enterpriseLoad();
    } catch (e) { alert("创建失败: " + e.message); }
  };

  window.enterpriseCreateUser = async function () {
    const org = document.getElementById("entUserOrg").value.trim();
    const email = document.getElementById("entUserEmail").value.trim();
    const pwd = document.getElementById("entUserPwd").value;
    const name = document.getElementById("entUserName").value.trim();
    if (!org || !email || !pwd) { alert("组织ID/邮箱/密码必填"); return; }
    try {
      await entApi("/orgs/" + org + "/users", { method: "POST", body: { email: email, password: pwd, display_name: name } });
      alert("用户创建成功");
    } catch (e) { alert("创建失败: " + e.message); }
  };

  window.enterpriseSetBudget = async function () {
    const scope = document.getElementById("entBudgetScope").value || "org";
    const scopeId = document.getElementById("entBudgetScopeId").value.trim();
    const amt = parseFloat(document.getElementById("entBudgetAmt").value);
    if (!scopeId || !(amt > 0)) { alert("scope_id 与预算金额必填"); return; }
    try {
      await entApi("/governance/budget", { method: "PUT", body: { scope: scope, scope_id: scopeId, amount_usd: amt, hard_limit: true } });
      alert("预算已设置");
    } catch (e) { alert("设置失败: " + e.message); }
  };

  window.enterpriseSetMaintenance = async function (enabled) {
    const msg = document.getElementById("entMaintMsg").value || "系统维护中";
    try {
      await entApi("/maintenance", { method: "PUT", body: { enabled: enabled, message: msg, readonly: enabled } });
      window.enterpriseLoad();
    } catch (e) { alert("操作失败: " + e.message); }
  };

  window.enterpriseCreateAnn = async function () {
    const title = document.getElementById("entAnnTitle").value.trim();
    const content = document.getElementById("entAnnContent").value.trim();
    if (!title) { alert("标题必填"); return; }
    try {
      await entApi("/announcements", { method: "POST", body: { title: title, content: content } });
      alert("公告已发布");
      window.enterpriseLoad();
    } catch (e) { alert("发布失败: " + e.message); }
  };

  // 挂载企业面板初始化：切换视图时刷新
  document.addEventListener("click", function (ev) {
    if (ev.target.closest && ev.target.closest('.sidebar-item[data-view="enterprise"]')) {
      setTimeout(window.enterpriseLoad, 150);
    }
  });
  // 页面加载时若为企业视图则初始化
  document.addEventListener("DOMContentLoaded", function () {
    if (document.getElementById("view-enterprise") && document.getElementById("view-enterprise").classList.contains("active")) {
      window.enterpriseLoad();
    }
  });

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  // ── 引导界面（登录 / 注册 / 游客）──────────────────────────────
  const landing = document.getElementById("landing");
  const landingModes = document.getElementById("landingModes");
  const landingForm = document.getElementById("landingForm");
  const landingFormTitle = document.getElementById("landingFormTitle");
  const landingHint = document.getElementById("landingHint");
  const landingToken = document.getElementById("landingToken");
  const guestHint = document.getElementById("guestHint");
  let landingMode = "";
  const LANDING_PREF = "doctoragent_landing_pref";

  // 若无引导元素（或已记住偏好/已有令牌），直接进入
  function landingEnter() {
    if (!landing) return;
    landing.classList.add("hidden");
    document.body.classList.add("landing-done");
    // 聚焦聊天输入框
    const inp = document.getElementById("chatInput");
    if (inp) setTimeout(function () { inp.focus(); }, 260);
  }

  window.enterAsGuest = function () {
    landingMode = "guest";
    try { localStorage.setItem(LANDING_PREF, "guest"); } catch (e) {}
    if (guestHint) guestHint.classList.remove("hidden");
    if (landingForm) landingForm.classList.add("hidden");
    if (landingModes) landingModes.classList.remove("hidden");
    setTimeout(landingEnter, 260);
  };

  // 记住偏好/已有令牌 → 跳过引导直接进入
  function maybeSkipLanding() {
    if (!landing) return;
    let pref = "";
    try { pref = localStorage.getItem(LANDING_PREF) || ""; } catch (e) {}
    const hasToken = !!getToken();
    if (pref === "guest" || hasToken) {
      landing.classList.add("hidden");
      document.body.classList.add("landing-done");
    }
  }

  function showLandingForm(mode) {
    landingMode = mode;
    const isRegister = mode === "register";
    if (landingModes) landingModes.classList.add("hidden");
    if (guestHint) guestHint.classList.add("hidden");
    if (landingForm) {
      landingForm.classList.remove("hidden");
      landingFormTitle.textContent = isRegister ? "注册 · 配置访问令牌" : "登录";
      landingHint.innerHTML = isRegister
        ? "企业 / 多端同步：请输入由管理员在服务端配置的访问令牌（<code>DOCTORAGENT_API_TOKEN</code>）。"
        : "输入你的访问令牌后进入控制台；本地访问可留空。";
      landingToken.value = getToken() || "";
      setTimeout(function () { landingToken.focus(); }, 120);
    }
  }

  function submitLandingForm() {
    const token = (landingToken.value || "").trim();
    if (token) { try { setToken(token); } catch (e) {} }
    try { localStorage.removeItem(LANDING_PREF); } catch (e) {}
    // 冒烟：验证令牌连通性（可忽略失败，本地无令牌也可用）
    try {
      api("/api/v1/enterprise/status").catch(function () {});
    } catch (e) {}
    landingEnter();
  }

  if (landing) {
    // 模式按钮
    landingModes && landingModes.querySelectorAll(".landing-mode").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const mode = btn.dataset.mode;
        if (mode === "guest") window.enterAsGuest();
        else showLandingForm(mode);
      });
    });
    // 返回
    const backBtn = document.getElementById("landingBack");
    if (backBtn) backBtn.addEventListener("click", function () {
      if (landingForm) landingForm.classList.add("hidden");
      if (landingModes) landingModes.classList.remove("hidden");
    });
    // 表单提交
    landingForm && landingForm.addEventListener("submit", function (e) {
      e.preventDefault();
      submitLandingForm();
    });
    // 回车
    landingToken && landingToken.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); submitLandingForm(); }
    });
  }
  maybeSkipLanding();

  // ── 临床科室角色切换器 ───────────────────────────────────────
  const roleSelect = document.getElementById("clinicalRoleSelect");
  if (roleSelect) {
    function loadRoles() {
      try {
        api("/api/v1/clinical/roles").then(function (d) {
          const items = d && d.items ? d.items : [];
          if (!items.length) return;
          const current = (d.current || "");
          roleSelect.innerHTML = items.map(function (r) {
            return '<option value="' + esc(r.code) + '">' + esc(r.name) + "</option>";
          }).join("");
          // 尽量保持当前角色
          const active = (localStorage.getItem("doctoragent_clinical_role") || "").trim();
          if (active && items.some(function (r) { return r.code === active; })) roleSelect.value = active;
        }).catch(function () {});
      } catch (e) {}
    }
    roleSelect.addEventListener("change", function () {
      const code = roleSelect.value;
      try {
        api("/api/v1/clinical/roles/" + code + "/activate", { method: "POST", body: {} }).then(function (r) {
          try { localStorage.setItem("doctoragent_clinical_role", code); } catch (e2) {}
          toast("已切换为「" + (r && r.name ? r.name : code) + "」", "success");
        }).catch(function (e) { toast("切换失败：" + e.message, "error"); });
      } catch (e) {}
    });
    // 页面加载后异步填充
    setTimeout(loadRoles, 600);
  }

})();



