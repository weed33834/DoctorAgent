#!/usr/bin/env node
// DoctorAgent 全功能测试录屏脚本 v4（分段运行）
// 段1（医生视图）：前 14 个视图 + 核心交互 → 保存视频 1
// 段2（管理视图）：管理视图切换 + 后 13 个视图 → 保存视频 2
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
const SEGMENT = process.env.SEGMENT || '1';  // '1' = 医生段，'2' = 管理段
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

const VIEW_ACTIONS = {
  chat:       { click: '#chatRefreshBtn', fill: null },
  clinical:   { click: '#runClinicalBtn', fill: { sel: '#ctx-patient-id', text: 'P001' } },
  deidentify: { click: '#deid-example-btn', fill: null },
  safety:     { click: '#safety-rules-btn', fill: null },
  agents:     { click: null, fill: null },  // 跳过：26s LLM 请求阻塞
  vault:      { click: '#searchBtn', fill: { sel: '#searchQuery', text: '糖尿病' } },
  rag:        { click: '#ragRouteBtn', fill: { sel: '#ragRouteInput', text: '二甲双胍的作用机制' } },
  kg:         { click: '#kgQueryBtn', fill: { sel: '#kgQueryInput', text: '糖尿病' } },
  mem:        { click: '#memRefreshBtn', fill: null },
  prompts:    { click: '#promptsRefreshBtn', fill: null },
  dag:        { click: '#dagStatusBtn', fill: null },
  eval:       { click: null, fill: null },
  evo:        { click: '#evoTrajectoryBtn', fill: null },
  rl:         { click: null, fill: null },
  collab:     { click: '#collabRefreshBtn', fill: null },
  config:     { click: '#configLoadBtn', fill: null },
  connections:{ click: '#connLoadBtn', fill: null },
  tenants:    { click: '#tenantLoadBtn', fill: null },
  system:     { click: '#refreshSystemBtn', fill: null },
  audit:      { click: '#auditQueryBtn', fill: null },
  compliance: { click: null, fill: null },
  ops:        { click: '#syncStatusBtn', fill: null },
  settings:   { click: '#advConfigLoadBtn', fill: null },
  hooks:      { click: '#hooksRefreshBtn', fill: null },
  obs:        { click: '#obsRefreshBtn', fill: null },
  plugins:    { click: '#pluginsRefreshBtn', fill: null },
  exp:        { click: '#expRefreshBtn', fill: null },
};

const ALL_VIEWS = [
  'chat', 'clinical', 'deidentify', 'safety', 'agents',
  'vault', 'rag', 'kg', 'mem', 'prompts',
  'dag', 'eval', 'evo', 'rl', 'collab',
  'config', 'connections', 'tenants', 'system', 'audit',
  'compliance', 'ops', 'settings', 'hooks', 'obs', 'plugins', 'exp',
];

async function activateView(page, view) {
  const action = VIEW_ACTIONS[view];
  if (!action) return;
  if (action.fill) {
    const inp = page.locator(action.fill.sel);
    if (await inp.count()) await inp.fill(action.fill.text).catch(() => {});
  }
  if (action.click) {
    const btn = page.locator(action.click);
    if (await btn.count()) await btn.first().click().catch(() => {});
  }
}

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
    log(`=== 段 ${SEGMENT} ===`);
    log('打开控制台:', BASE + '/console/');
    await page.goto(BASE + '/console/', { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(2500);
    await shot(page, '00-console-home');

    // 注入 token
    try {
      const tokenInput = page.locator('#tokenInput');
      if (await tokenInput.count()) await tokenInput.fill(TOKEN);
    } catch {}
    await page.evaluate((t) => localStorage.setItem('doctoragent_api_token', t), TOKEN).catch(() => {});
    await page.waitForTimeout(600);

    // 关闭引导遮罩
    const skipBtn = page.locator('#onboardSkipBtn');
    if (await skipBtn.count() && await skipBtn.isVisible().catch(() => false)) {
      await skipBtn.click().catch(() => {});
      await page.waitForTimeout(500);
      log('已关闭引导遮罩');
    }
    await shot(page, '01-after-onboard-skip');

    // 段1：医生视图（前 15 个）+ 核心交互
    if (SEGMENT === '1') {
      const views1 = ALL_VIEWS.slice(0, 15);  // chat..collab
      for (const view of views1) {
        const btn = page.locator(`.sidebar-item[data-view="${view}"]`);
        if (!(await btn.count())) continue;
        await btn.first().click().catch(() => {});
        await page.waitForTimeout(1000);
        await activateView(page, view);
        await page.waitForTimeout(2500);
        await shot(page, 'view-' + view);
        log(`视图 ${view} 完成`);
      }
      // 核心交互：对话、脱敏、临床
      await page.locator('.sidebar-item[data-view="chat"]').first().click().catch(() => {});
      await page.waitForTimeout(1200);
      const chatInput = page.locator('#chatInput');
      if (await chatInput.count()) {
        await chatInput.fill('你好，请介绍你自己');
        await page.keyboard.press('Enter').catch(() => {});
        await page.waitForTimeout(5000);
        await shot(page, 'interact-chat');
      }
      await page.locator('.sidebar-item[data-view="deidentify"]').first().click().catch(() => {});
      await page.waitForTimeout(1200);
      const deidInput = page.locator('#deid-input');
      if (await deidInput.count()) {
        await deidInput.fill('患者张三，电话13800138000，邮箱zhangsan@test.com');
        await page.locator('#deid-run-btn').first().click().catch(() => {});
        await page.waitForTimeout(3000);
        await shot(page, 'interact-deidentify');
      }
      log('段 1 完成');
    }

    // 段2：管理视图切换 + 后 12 个视图
    if (SEGMENT === '2') {
      // 先切到管理视图
      await page.locator('.view-tab[data-view="admin"]').first().click().catch(() => {});
      await page.waitForTimeout(1500);
      await shot(page, '02-admin-view');
      const views2 = ALL_VIEWS.slice(15);  // config..exp
      for (const view of views2) {
        const btn = page.locator(`.sidebar-item[data-view="${view}"]`);
        if (!(await btn.count())) continue;
        await btn.first().click().catch(() => {});
        await page.waitForTimeout(1000);
        await activateView(page, view);
        await page.waitForTimeout(2500);
        await shot(page, 'view-' + view);
        log(`视图 ${view} 完成`);
      }
      // 关键管理视图交互：审计查询、系统状态、配置加载
      await page.locator('.sidebar-item[data-view="audit"]').first().click().catch(() => {});
      await page.waitForTimeout(1000);
      await page.locator('#auditQueryBtn').first().click().catch(() => {});
      await page.waitForTimeout(3000);
      await shot(page, 'interact-audit');
      await page.locator('.sidebar-item[data-view="system"]').first().click().catch(() => {});
      await page.waitForTimeout(1000);
      await page.locator('#refreshSystemBtn').first().click().catch(() => {});
      await page.waitForTimeout(3000);
      await shot(page, 'interact-system');
      log('段 2 完成');
    }

    log('全部完成');
  } catch (err) {
    log('流程异常:', err.message);
    await shot(page, 'error-state').catch(() => {});
  } finally {
    await context.close();
    await browser.close();
  }

  // 重命名视频
  try {
    const files = readdirSync(RUN_DIR).filter((f) => f.endsWith('.webm'));
    if (files.length) {
      const src = path.join(RUN_DIR, files[0]);
      const dst = path.join(RUN_DIR, `doctoragent-console-seg${SEGMENT}.webm`);
      if (src !== dst) renameSync(src, dst);
      log('视频已保存:', dst, (statSync(dst).size / 1024 / 1024).toFixed(1) + 'MB');
    }
  } catch (e) {
    log('视频重命名失败:', e.message);
  }
  log('截图数量:', shotCount.n, '目录:', SHOT_DIR);
  writeFileSync(path.join(RUN_DIR, 'summary.txt'), `segment=${SEGMENT}\nshots=${shotCount.n}\n`);
})();