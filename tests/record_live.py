#!/usr/bin/env python3
"""Record a REAL live session against the running server on :3000.

Sends real multi-turn clinical questions through the console (which calls the
live LLM via /vault/agent/stream), waits for the real streaming reply, then
tours modules. Outputs video + screenshots.
"""
import os
import pathlib
import time

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = pathlib.Path("/tmp/qwenwork_live")
OUT.mkdir(parents=True, exist_ok=True)
VIDEO_DIR = OUT / "video"

QUESTIONS = [
    "华法林和布洛芬能一起吃吗？请给用药建议。",
    "他有肝硬化，止痛药应该怎么选？剂量如何调整？",
]
MODULES = [("clinical", "临床工作台"), ("safety", "安全规则"), ("deidentify", "PHI 脱敏"),
           ("vault", "文档 Vault"), ("agents", "智能体编排"), ("system", "系统状态"),
           ("enterprise", "企业平台")]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  record_video_dir=str(VIDEO_DIR),
                                  record_video_size={"width": 1440, "height": 900})
        pg = ctx.new_page()
        # 预置访问令牌（与启动脚本的 DOCTORAGENT_API_TOKEN=demo-token 对应）
        pg.goto("http://127.0.0.1:3000/console/index.html", wait_until="domcontentloaded", timeout=30000)
        pg.evaluate("() => { try { localStorage.setItem('doctoragent_api_token','demo-token'); "
                    "localStorage.setItem('doctoragent_landing_pref','guest'); } catch(e){} }")
        pg.reload(wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1200)
        pg.screenshot(path=str(OUT / "00_landing.png"))
        pg.evaluate("window.enterAsGuest ? window.enterAsGuest() : null")
        pg.wait_for_timeout(800)
        pg.evaluate("() => { const o=document.getElementById('onboardOverlay'); if(o) o.remove(); "
                    "const m=document.querySelector('.onboard-mask'); if(m) m.remove(); "
                    "const b=document.querySelector('.view-tab[data-view=\"admin\"]'); if(b) b.click(); }")
        pg.wait_for_timeout(500)

        # 真实多轮对话：逐题输入并等待真实流式回复
        for qi, q in enumerate(QUESTIONS):
            inp = pg.locator("#chatInput")
            inp.click()
            for ch in q:
                inp.type(ch)
                pg.wait_for_timeout(20)
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(500)
            # 等待回复文本出现（真实模型可能较慢）
            waited = 0
            replied = False
            while waited < 60:
                try:
                    replied = pg.evaluate(
                        "() => { const els=document.querySelectorAll('.assistant-msg .md-body, .assistant-msg'); "
                        "for(const e of els){ if(e.textContent && e.textContent.length>10) return true; } return false; }"
                    )
                    if replied:
                        break
                except Exception:  # noqa: BLE001
                    pass
                pg.wait_for_timeout(1500)
                waited += 1.5
            pg.wait_for_timeout(2500)
            pg.screenshot(path=str(OUT / f"{qi+1:02d}_live_chat.png"))
            print(f"[turn{qi+1}] replied={replied} waited={waited}s", flush=True)

        # 模块巡览
        for i, (view, label) in enumerate(MODULES, start=10):
            try:
                pg.hover(f'.sidebar-item[data-view="{view}"]', timeout=4000)
                pg.evaluate("v => { const b=document.querySelector('.sidebar-item[data-view=\"'+v+'\"]'); if(b) b.click(); }", view)
                pg.wait_for_timeout(700)
                pg.screenshot(path=str(OUT / f"{i:02d}_{view}.png"))
            except Exception:  # noqa: BLE001
                pass
        pg.wait_for_timeout(1000)
        browser.close()

    vids = list(VIDEO_DIR.glob("*.webm"))
    if vids:
        vids.sort(key=lambda p: p.stat().st_mtime)
        os.replace(vids[-1], OUT / "doctoragent_live.webm")
        print("VIDEO:", OUT / "doctoragent_live.webm", flush=True)


if __name__ == "__main__":
    main()
