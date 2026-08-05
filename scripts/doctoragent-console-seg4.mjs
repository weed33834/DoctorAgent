#!/usr/bin/env node
// DoctorAgent 录屏 v5 — 段4 补剩余 4 个交互（Vault/记忆/Prompt/连接）
// 修复策略：每步交互后强制 reload 页面，避免前一个 LLM 请求阻塞事件循环
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { mkdirSync, writeFileSync, readdirSync, renameSync, statSync } from 'node:fs';
import path from 'node:path';

const _require = createRequire(import.meta.url);
const playwrightPath = process.env.PLAYWRIGHT_PKG ||
  'C:/Users/Administrator/WorkBuddy/2026-08-03-22-49-14/nova/node_modules/playwright';
const { chromium } = _require(playwrightPath);

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';
const TOKEN = process.env.API_TOKEN || 'dev-local-audit-token-2026';
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(__dirname, 'assets', 'console-walkthrough');
const RUN_ID = 'run-' + Date.now();
const RUN_DIR = path.join(OUT, RUN_ID);
const SHOT_DIR = path.join(RUN_DIR, 'shots');
mkdirSync(SHOT_DIR, { recursive: true });

const log = (...a) => console.log('[walkthrough]', ...a);
const shotCount = { n: 0 };
async function shot(page, name) {
  shotCount.n += 1;
  const file = path.join(SHOT_DIR, String(shotCount.n).padStart(2, '0') + '-' + name + '.png');
  await page.screenshot({ path: file });
  log('截图:', path.basename(file));
}

// 4 个交互：视图 → 填输入 → 点按钮 → 等待 → 截图 → reload
const ACTIONS = [
  { view: 'vault',       label: 'Vault 检索', fill: { sel: '#searchQuery', text: '糖尿病' }, click: '#searchBtn', waitMs: 6000 },
  { view: 'mem',         label: '记忆召回',   fill: { sel: '#memRecallLimit', text: '5' },  click: '#memRecallBtn', waitMs: 6000 },
  { view: 'prompts',     label: 'Prompt 列表', fill: null, click: '#promptsRefreshBtn', waitMs: 5000 },
  { view: 'connections', label: '连接列表',   fill: null, click: '#connLoadBtn', waitMs: 5000 },
];

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-dev-shm-usage', '--disable-gpu', '--no-sandbox', '--disable-extensions'],
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    recordVideo: { dir: RUN_DIR, size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);

  try {
    log('打开控制台');
    await page.goto(BASE + '/console/', { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(2500);
    await page.evaluate((t) => localStorage.setItem('doctoragent_api_token', t), TOKEN).catch(() => {});
    const skipBtn = page.locator('#onboardSkipBtn');
    if (await skipBtn.count() && await skipBtn.isVisible().catch(() => false)) {
      await skipBtn.click().catch(() => {});
      await page.waitForTimeout(500);
    }

    for (const act of ACTIONS) {
      // 每次操作前 reload，隔离上一交互的请求
      await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
      await page.waitForTimeout(1800);
      const btn = page.locator(`.sidebar-item[data-view="${act.view}"]`);
      if (!(await btn.count())) { log('跳过:', act.label); continue; }
      await btn.first().click().catch(() => {});
      await page.waitForTimeout(1200);
      if (act.fill) {
        const inp = page.locator(act.fill.sel);
        if (await inp.count()) await inp.fill(act.fill.text).catch(() => {});
      }
      const runBtn = page.locator(act.click);
      if (await runBtn.count()) await runBtn.first().click().catch(() => {});
      await page.waitForTimeout(act.waitMs);
      await shot(page, 'interact-' + act.label);
      log(`${act.label} 完成`);
    }
    log('段 4 全部完成');
  } catch (err) {
    log('流程异常:', err.message);
    await shot(page, 'error-state').catch(() => {});
  } finally {
    await context.close();
    await browser.close();
  }

  try {
    const files = readdirSync(RUN_DIR).filter((f) => f.endsWith('.webm'));
    if (files.length) {
      const src = path.join(RUN_DIR, files[0]);
      const dst = path.join(RUN_DIR, 'doctoragent-console-seg4.webm');
      if (src !== dst) renameSync(src, dst);
      log('视频已保存:', dst, (statSync(dst).size / 1024 / 1024).toFixed(1) + 'MB');
    }
  } catch (e) {
    log('视频重命名失败:', e.message);
  }
  log('截图数量:', shotCount.n, '目录:', SHOT_DIR);
  writeFileSync(path.join(RUN_DIR, 'summary.txt'), `segment=4\nshots=${shotCount.n}\n`);
})();
