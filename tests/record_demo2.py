#!/usr/bin/env python3
"""Record a rich DoctorAgent console demo video (robust version)."""
import json
import os
import pathlib

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = pathlib.Path("/tmp/qwenwork_demo2")
OUT.mkdir(parents=True, exist_ok=True)
VIDEO_DIR = OUT / "video"

SESSIONS = [
    {"id": "chat-1", "title": "华法林用药安全评估",
     "createdAt": "2026-08-11T08:00:00.000Z",
     "messages": [
         {"role": "user", "content": "华法林能和布洛芬一起吃吗？", "ts": "2026-08-11T08:00:00Z"},
         {"role": "assistant", "content": "**不建议联用**：会增加消化道出血风险。建议用对乙酰氨基酚止痛，并加强 INR 监测。", "ts": "2026-08-11T08:01:00Z"},
         {"role": "user", "content": "他有肝硬化，剂量怎么调整？", "ts": "2026-08-11T08:02:00Z"},
         {"role": "assistant", "content": "肝硬化患者对乙酰氨基酚需减量并短疗程（≤2g/日，≤3 天），并监测肝功能。", "ts": "2026-08-11T08:03:00Z"},
     ]},
]

MODULES = [("clinical", "临床工作台"), ("safety", "安全规则"), ("deidentify", "PHI 脱敏"),
           ("vault", "文档 Vault"), ("agents", "智能体编排"), ("system", "系统状态"),
           ("enterprise", "企业平台"), ("mem", "记忆管理"), ("eval", "评估中心")]


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
        pg.evaluate("window.enterAsGuest ? window.enterAsGuest() : null")
        pg.wait_for_timeout(800)
        # 关闭新手引导弹层，避免遮挡交互
        pg.evaluate("() => { const o=document.getElementById('onboardOverlay'); if(o) o.remove(); "
                    "const m=document.querySelector('.onboard-mask'); if(m) m.remove(); "
                    "document.body.classList.remove('onboarding-active'); }")

        # 打开会话展示多轮医疗对话
        pg.evaluate("() => { const c=document.querySelector('.chat-session'); if(c) c.click(); }")
        pg.wait_for_timeout(900)
        pg.screenshot(path=str(OUT / "01_chat.png"))

        # 输入框打字动画
        inp = pg.locator("#chatInput")
        q = "把角色切换成心内科医生，然后查看系统状态"
        inp.click()
        for ch in q:
            inp.type(ch)
            pg.wait_for_timeout(28)
        pg.wait_for_timeout(500)

        # 模块巡览：悬停 + 切换 + 卡片悬停
        for view, label in MODULES:
            try:
                pg.hover(f'.sidebar-item[data-view="{view}"]')
                pg.wait_for_timeout(200)
                pg.evaluate("v => { const b=document.querySelector('.sidebar-item[data-view=\"'+v+'\"]'); if(b) b.click(); }", view)
                pg.wait_for_timeout(650)
                pg.mouse.move(340, 320)
                pg.wait_for_timeout(350)
            except Exception:  # noqa: BLE001
                pass
        pg.screenshot(path=str(OUT / "02_modules.png"))

        # 主题切换 暗→亮→暗
        pg.evaluate("() => { const t=document.getElementById('themeToggle'); if(t) t.click(); }")
        pg.wait_for_timeout(800)
        pg.screenshot(path=str(OUT / "03_light.png"))
        pg.evaluate("() => { const t=document.getElementById('themeToggle'); if(t) t.click(); }")
        pg.wait_for_timeout(800)
        pg.evaluate("() => { const b=document.querySelector('.sidebar-item[data-view=\"chat\"]'); if(b) b.click(); }")
        pg.wait_for_timeout(700)

        pg.wait_for_timeout(1000)
        browser.close()

    vids = list(VIDEO_DIR.glob("*.webm"))
    if vids:
        vids.sort(key=lambda p: p.stat().st_mtime)
        os.replace(vids[-1], OUT / "doctoragent_demo.webm")
        print("VIDEO:", OUT / "doctoragent_demo.webm", flush=True)


if __name__ == "__main__":
    main()
