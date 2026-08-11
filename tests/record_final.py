#!/usr/bin/env python3
"""Final promotional recording: real model content + full module tour."""
import json
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
OUT = pathlib.Path("/tmp/qwenwork_final")
OUT.mkdir(parents=True, exist_ok=True)
VIDEO_DIR = OUT / "video"

# 读取真实模型内容
try:
    real = json.load(open("/tmp/real_chat.json", encoding="utf-8"))
except Exception:  # noqa: BLE001
    real = []

msgs = []
for i, item in enumerate(real):
    msgs.append({"role": "user", "content": item["q"], "ts": f"2026-08-11T{9+i:02d}:00:00Z"})
    msgs.append({"role": "assistant", "content": item["a"], "ts": f"2026-08-11T{9+i:02d}:01:00Z"})

SESSIONS = [{
    "id": "chat-live", "title": "华法林用药安全评估（临床药师）",
    "createdAt": "2026-08-11T09:00:00.000Z", "messages": msgs,
}]

MODULES = [("chat", "智能对话"), ("clinical", "临床工作台"), ("safety", "安全规则"),
           ("deidentify", "PHI 脱敏"), ("vault", "文档 Vault"), ("rag", "高级 RAG"),
           ("agents", "智能体编排"), ("system", "系统状态"), ("enterprise", "企业平台"),
           ("mem", "记忆管理"), ("eval", "评估中心")]

SAMPLE = {
    "system": "const s=document.getElementById('sysHeroSub'); if(s) s.textContent='服务正常 · 模型 step-3.5-flash · v0.5.x';",
    "enterprise": "const a=document.getElementById('entOrgCount'); if(a) a.textContent='3';"
                  "const u=document.getElementById('entUserCount'); if(u) u.textContent='27';",
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
        pg.evaluate("window.enterAsGuest ? window.enterAsGuest() : null")
        pg.wait_for_timeout(600)
        pg.evaluate("() => { const o=document.getElementById('onboardOverlay'); if(o) o.remove(); "
                    "const m=document.querySelector('.onboard-mask'); if(m) m.remove(); "
                    "const b=document.querySelector('.view-tab[data-view=\"admin\"]'); if(b) b.click(); }")
        pg.wait_for_timeout(500)
        # 打开真实对话
        pg.evaluate("() => { const c=document.querySelector('.chat-session'); if(c) c.click(); }")
        pg.wait_for_timeout(900)
        pg.screenshot(path=str(OUT / "01_chat.png"))

        for i, (view, label) in enumerate(MODULES, start=2):
            try:
                pg.hover(f'.sidebar-item[data-view="{view}"]', timeout=4000)
                pg.wait_for_timeout(160)
                pg.evaluate("v => { const b=document.querySelector('.sidebar-item[data-view=\"'+v+'\"]'); if(b) b.click(); }", view)
                pg.wait_for_timeout(550)
                sm = SAMPLE.get(view)
                if sm:
                    try:
                        pg.evaluate(sm)
                        pg.wait_for_timeout(250)
                    except Exception:  # noqa: BLE001
                        pass
                pg.mouse.move(360, 320)
                pg.wait_for_timeout(250)
                pg.screenshot(path=str(OUT / f"{i:02d}_{view}.png"))
            except Exception as e:  # noqa: BLE001
                print("[warn]", view, str(e)[:30])

        pg.evaluate("() => { const t=document.getElementById('themeToggle'); if(t) t.click(); }")
        pg.wait_for_timeout(700)
        pg.screenshot(path=str(OUT / "99_light.png"))
        pg.evaluate("() => { const t=document.getElementById('themeToggle'); if(t) t.click(); }")
        pg.wait_for_timeout(600)
        pg.wait_for_timeout(1000)
        browser.close()

    vids = list(VIDEO_DIR.glob("*.webm"))
    if vids:
        vids.sort(key=lambda p: p.stat().st_mtime)
        os.replace(vids[-1], OUT / "doctoragent_final.webm")
        print("VIDEO:", OUT / "doctoragent_final.webm", flush=True)


if __name__ == "__main__":
    main()
