#!/usr/bin/env node
// DoctorAgent 全功能录屏 v5 — 段3 补录重型视图与核心交互
// 覆盖：agents 图谱、eval 评估、rl 强化学习、collab 协作、kg 知识图谱、
//       dag 工作流、evo 自进化、ops 运维、hooks 钩子、exp 实验 + 关键交互
// 说明：重型视图的加载按钮会触发真实 LLM/图谱调用（agents 26s+），
//       本段专门处理它们，点击后等待足够时长再截图。
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

// 重型视图的激活动作（点击后等 waitMs 再截图）
const HEAVY_ACTIONS = [
  { view: 'agents',  click: '#agents-load-btn',   waitMs: 30000, label: 'agents 拓扑' },
  { view: 'eval',    click: '#evalRunBtn',        waitMs: 8000,  label: '评估运行' },
  { view: 'rl',      click: '#rlSubmitBtn',       waitMs: 8000,  label: 'RL 反馈' },
  { view: 'collab',  click: '#collabRefreshBtn',  waitMs: 6000,  label: '协作列表' },
  { view: 'kg',      click: '#kgQueryBtn',        waitMs: 10000, label: '知识图谱' },
  { view: 'dag',     click: '#dagStatusBtn',      waitMs: 6000,  label: 'DAG 状态' },
  { view: 'evo',     click: '#evoTrajectoryBtn',  waitMs: 8000,  label: '自进化轨迹' },
  { view: 'ops',     click: '#syncStatusBtn',     waitMs: 5000,  label: '同步状态' },
  { view: 'hooks',   click: '#hooksRefreshBtn',   waitMs: 5000,  label: '钩子列表' },
  { view: 'exp',     click: '#expRefreshBtn',     waitMs: 5000,  label: '实验列表' },
];

// 关键交互（填输入 + 点按钮 + 等待）
const INTERACTIONS = [
  {
    label: 'RAG 路由问答',
    view: 'rag',
    fill: { sel: '#ragRouteInput', text: '二甲双胍的作用机制是什么？' },
    click: '#ragRouteBtn', waitMs: 8000,
  },
  {
    label: 'Vault 检索',
    view: 'vault',
    fill: { sel: '#searchQuery', text: '糖尿病' },
    click: '#searchBtn', waitMs: 6000,
  },
  {
    label: '记忆召回',
    view: 'mem',
    fill: { sel: '#memRecallLimit', text: '5' },
    click: '#memRecallBtn', waitMs: 6000,
  },
  {
    label: 'Prompt 渲染',
    view: 'prompts',
    click: '#promptsRefreshBtn', waitMs: 5000,
  },
  {
    label: '连接列表',
    view: 'connections',
    click: '#connLoadBtn', waitMs: 5000,
  },
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
  page.setDefaultTimeout(20000);

  try {
    log('打开控制台:', BASE + '/console/');
    await page.goto(BASE + '/console/', { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(2500);
    await page.evaluate((t) => localStorage.setItem('doctoragent_api_token', t), TOKEN).catch(() => {});
    // 关闭引导
    const skipBtn = page.locator('#onboardSkipBtn');
    if (await skipBtn.count() && await skipBtn.isVisible().catch(() => false)) {
      await skipBtn.click().catch(() => {});
      await page.waitForTimeout(500);
    }

    // 1) 重型视图真实加载
    log('=== 重型视图真实加载 ===');
    for (const act of HEAVY_ACTIONS) {
      const btn = page.locator(`.sidebar-item[data-view="${act.view}"]`);
      if (!(await btn.count())) { log('跳过:', act.view); continue; }
      await btn.first().click().catch(() => {});
      await page.waitForTimeout(1200);
      const loadBtn = page.locator(act.click);
      if (await loadBtn.count()) {
        await loadBtn.first().click().catch(() => {});
        log(`已触发 ${act.label}，等待 ${act.waitMs / 1000}s`);
      }
      await page.waitForTimeout(act.waitMs);
      await shot(page, 'heavy-' + act.view + '-' + act.label);
      log(`${act.label} 完成`);
    }

    // 2) 核心交互
    log('=== 核心交互 ===');
    for (const act of INTERACTIONS) {
      const btn = page.locator(`.sidebar-item[data-view="${act.view}"]`);
      if (!(await btn.count())) { log('跳过交互:', act.label); continue; }
      await btn.first().click().catch(() => {});
      await page.waitForTimeout(1000);
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

    // 3) 收尾
    log('段 3 全部完成');
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
      const dst = path.join(RUN_DIR, 'doctoragent-console-seg3.webm');
      if (src !== dst) renameSync(src, dst);
      log('视频已保存:', dst, (statSync(dst).size / 1024 / 1024).toFixed(1) + 'MB');
    }
  } catch (e) {
    log('视频重命名失败:', e.message);
  }
  log('截图数量:', shotCount.n, '目录:', SHOT_DIR);
  writeFileSync(path.join(RUN_DIR, 'summary.txt'), `segment=3\nshots=${shotCount.n}\n`);
})();
