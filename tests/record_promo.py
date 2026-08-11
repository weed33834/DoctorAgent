#!/usr/bin/env python3
"""Record a complete promotional walkthrough (real-user simulation).

Pre-seeds realistic clinical conversation (incl. real gpt-oss output) and
sample data into each module, then drives the console like a real user:
landing → guest → multi-turn chat → role switch → module tour → theme toggle.
Outputs a .webm + per-module screenshots.
"""
import json
import os
import pathlib
import time

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = pathlib.Path("/tmp/qwenwork_promo")
OUT.mkdir(parents=True, exist_ok=True)
VIDEO_DIR = OUT / "video"

SESSIONS = [
    {"id": "chat-1", "title": "华法林用药安全评估（心内）",
     "createdAt": "2026-08-11T08:00:00.000Z",
     "messages": [
         {"role": "user", "content": "华法林和布洛芬能一起吃吗？", "ts": "2026-08-11T08:00:00Z"},
         {"role": "assistant", "content": "不建议同时使用。华法林与布洛芬等 NSAIDs 合用会显著增加胃肠道出血风险。建议优先选择对乙酰氨基酚，并加强 INR 监测。", "ts": "2026-08-11T08:01:00Z"},
         {"role": "user", "content": "那止痛药应该选什么？他有肝硬化。", "ts": "2026-08-11T08:02:00Z"},
         {"role": "assistant", "content": "患者：心内科（华法林抗凝）+ 肝硬化。止痛优先对乙酰氨基酚，但需减量（≤2g/日）并短疗程，避免 NSAIDs；监测肝功能与 INR。", "ts": "2026-08-11T08:03:00Z"},
         {"role": "user", "content": "用 Python 算一下：70kg、每日 1.5mg/kg，总剂量多少？", "ts": "2026-08-11T08:04:00Z"},
         {"role": "assistant", "content": "70 × 1.5 = **105 mg/日**。\n\n```python\nweight=70; dose=1.5\nprint(weight*dose)  # 105.0\n```\n\n已用代码沙箱复核，结果为 105 mg。", "ts": "2026-08-11T08:05:00Z"},
     ]},
]

MODULES = [("chat", "智能对话"), ("clinical", "临床工作台"), ("safety", "安全规则"),
           ("deidentify", "PHI 脱敏"), ("vault", "文档 Vault"), ("rag", "高级 RAG"),
           ("agents", "智能体编排"), ("system", "系统状态"), ("enterprise", "企业平台"),
           ("mem", "记忆管理"), ("eval", "评估中心")]

# 每个模块注入的示例数据（让界面看起来真实）
SAMPLE = {
    "clinical": "document.getElementById('ctx-patient-id').value='synthetic-001';"
                "try{document.getElementById('ctx-patient-id').dispatchEvent(new Event('change'))}catch(e){}",
    "system": "const s=document.getElementById('sysHeroSub'); if(s) s.textContent='服务运行正常 · 版本 0.5.x · 模型 gpt-oss';"
              "const c=document.querySelectorAll('.metric-value'); if(c[0]) c[0].textContent='128'; if(c[1]) c[1].textContent='45ms';",
    "enterprise": "const c=document.getElementById('entOrgCount'); if(c) c.textContent='3';"
                  "const u=document.getElementById('entUserCount'); if(u) u.textContent='27';"
                  "const a=document.getElementById('entAnnCount'); if(a) a.textContent='2';"
                  "const m=document.getElementById('entMaintenance'); if(m) m.textContent='否';",
}


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  record_video_dir=str(VIDEO_DIR),
                                  record_video_size={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.goto("http://127.0.0.1:3000/console/index.html", wait_until="domcontentloaded", timeout=30000)
        pg.evaluate("d => { try { localStorage.setItem('doctoragent_chats', d); "
                    "localStorage.setItem('doctoragent_landing_pref','guest'); } catch(e){} }",
                    json.dumps(SESSIONS, ensure_ascii=False))
        pg.reload(wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1000)
        pg.screenshot(path=str(OUT / "00_landing.png"))
        # 关闭引导弹层并进入
        pg.evaluate("window.enterAsGuest ? window.enterAsGuest() : null")
        pg.wait_for_timeout(500)
        pg.evaluate("() => { const o=document.getElementById('onboardOverlay'); if(o) o.remove(); "
                    "const m=document.querySelector('.onboard-mask'); if(m) m.remove(); }")
        # 切换到管理视图（显示全部模块）
        pg.evaluate("() => { const b=document.querySelector('.view-tab[data-view=\"admin\"]'); if(b) b.click(); }")
        pg.wait_for_timeout(500)

        # 打开会话展示多轮对话
        pg.evaluate("() => { const c=document.querySelector('.chat-session'); if(c) c.click(); }")
        pg.wait_for_timeout(900)
        pg.screenshot(path=str(OUT / "01_chat.png"))

        # 输入框打字（模拟用户提问）
        inp = pg.locator("#chatInput")
        q = "把角色切换成心内科医生"
        inp.click()
        for ch in q:
            inp.type(ch)
            pg.wait_for_timeout(24)

        # 模块巡览 + 悬停 + 注入示例数据
        for i, (view, label) in enumerate(MODULES, start=2):
            try:
                pg.hover(f'.sidebar-item[data-view="{view}"]', timeout=5000)
                pg.wait_for_timeout(160)
                pg.evaluate("v => { const b=document.querySelector('.sidebar-item[data-view=\"'+v+'\"]'); if(b) b.click(); }", view)
                pg.wait_for_timeout(550)
                sample = SAMPLE.get(view)
                if sample:
                    try:
                        pg.evaluate(sample)
                        pg.wait_for_timeout(250)
                    except Exception:  # noqa: BLE001
                        pass
                pg.mouse.move(360, 320)
                pg.wait_for_timeout(260)
                pg.screenshot(path=str(OUT / f"{i:02d}_{view}.png"))
            except Exception as e:  # noqa: BLE001
                print("[warn]", view, str(e)[:40])

        # 主题切换
        pg.evaluate("() => { const t=document.getElementById('themeToggle'); if(t) t.click(); }")
        pg.wait_for_timeout(700)
        pg.screenshot(path=str(OUT / "99_light.png"))
        pg.evaluate("() => { const t=document.getElementById('themeToggle'); if(t) t.click(); }")
        pg.wait_for_timeout(700)
        # 回到对话
        pg.evaluate("() => { const b=document.querySelector('.sidebar-item[data-view=\"chat\"]'); if(b) b.click(); }")
        pg.wait_for_timeout(700)
        pg.screenshot(path=str(OUT / "98_back_chat.png"))

        pg.wait_for_timeout(1200)
        browser.close()

    vids = list(VIDEO_DIR.glob("*.webm"))
    if vids:
        vids.sort(key=lambda p: p.stat().st_mtime)
        os.replace(vids[-1], OUT / "doctoragent_promo.webm")
        print("VIDEO:", OUT / "doctoragent_promo.webm", flush=True)


if __name__ == "__main__":
    main()
