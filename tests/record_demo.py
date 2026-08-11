#!/usr/bin/env python3
"""Record a full DoctorAgent console walkthrough video + screenshots.

Records: landing → guest enter → rich multi-turn clinical chat → role switch →
module tour → back to chat. Outputs a .webm video and per-module PNGs.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = pathlib.Path("/tmp/qwenwork_demo")
OUT.mkdir(parents=True, exist_ok=True)
VIDEO_DIR = OUT / "video"

# 注入 3 个真实感医疗多轮会话（代表 Agent 会产生的回复）
sessions = [
    {
        "id": "chat-1",
        "title": "华法林用药安全评估",
        "createdAt": "2026-08-11T08:00:00.000Z",
        "messages": [
            {"role": "user", "content": "我父亲在吃华法林，最近又开了布洛芬，能一起吃吗？", "ts": "2026-08-11T08:00:00Z"},
            {"role": "assistant", "content": "**有出血风险，不建议联用。**\n\n华法林与布洛芬等 NSAIDs 联用会显著增加消化道出血风险。建议：\n- 止痛优先选用**对乙酰氨基酚**（扑热息痛）\n- 若必须用 NSAID，需与心内科/药师评估并加强 INR 监测\n\n> 依据：药物相互作用速查（内置知识库）", "ts": "2026-08-11T08:01:00Z"},
            {"role": "user", "content": "那对乙酰氨基酚的剂量怎么给？他有肝硬化病史。", "ts": "2026-08-11T08:02:00Z"},
            {"role": "assistant", "content": "肝硬化患者对乙酰氨基酚需**减量并短疗程**（建议 ≤2g/日，疗程 ≤3 天），并监测肝功能。若疼痛持续，建议就诊消化内科评估。", "ts": "2026-08-11T08:03:00Z"},
        ],
    },
    {
        "id": "chat-2",
        "title": "心衰患者 SGLT2i 评估",
        "createdAt": "2026-08-11T09:00:00.000Z",
        "messages": [
            {"role": "user", "content": "用 Python 帮我算一下：心衰患者 eGFR 45，能不能用 SGLT2i？按体重算个示例剂量。", "ts": "2026-08-11T09:00:00Z"},
            {"role": "assistant", "content": "eGFR ≥20 时 SGLT2i（如达格列净）可用，起始 10mg 每日一次。下面用 Python 演示按体重校准的估算：\n\n```python\nweight=70; dose_mg_per_kg=0.14\nprint(weight*dose_mg_per_kg)  # 9.8 → 取 10mg\n```\n\n**建议剂量：10mg/日**，起始后 2 周复查肾功能。", "ts": "2026-08-11T09:01:00Z"},
            {"role": "user", "content": "把他 eGFR 45 记住，方便下次复查对比。", "ts": "2026-08-11T09:02:00Z"},
            {"role": "assistant", "content": "已记住：**该患者基线 eGFR 45**（长期记忆）。下次复查可让我对比肾功能变化并评估 SGLT2i 是否继续。", "ts": "2026-08-11T09:03:00Z"},
        ],
    },
    {
        "id": "chat-3",
        "title": "术前抗凝评估（外科）",
        "createdAt": "2026-08-11T10:00:00.000Z",
        "messages": [
            {"role": "user", "content": "患者长期用华法林，下周做胆囊切除，术前怎么停药？", "ts": "2026-08-11T10:00:00Z"},
            {"role": "assistant", "content": "**低出血风险手术 + 华法林**：术前 5 天停药，待 INR 正常化后手术；高血栓风险需桥接低分子肝素（LMWH）。术后 24h 评估重启。\n\n> 依据：围手术期抗凝停药管理（内置知识库）", "ts": "2026-08-11T10:01:00Z"},
            {"role": "user", "content": "把他切成外科医生视角再回答一次，并列出术后 VTE 预防要点。", "ts": "2026-08-11T10:02:00Z"},
            {"role": "assistant", "content": "已切换为**外科医生**视角。术后 VTE 预防：低危—早期活动；中/高危—LMWH 或物理预防；有出血风险者优先物理预防。抗凝重启需权衡出血与血栓。", "ts": "2026-08-11T10:03:00Z"},
        ],
    },
]

MODULES = [
    ("chat", "智能对话"),
    ("clinical", "临床工作台"),
    ("safety", "安全规则"),
    ("deidentify", "PHI 脱敏"),
    ("vault", "文档 Vault"),
    ("agents", "智能体编排"),
    ("system", "系统状态"),
    ("enterprise", "企业平台"),
]


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(VIDEO_DIR),
            record_video_size={"width": 1440, "height": 900},
        )
        pg = ctx.new_page()
        # 加载后设置聊天会话与游客偏好，再刷新（避免 add_init_script 的 patchright 依赖）
        pg.goto("http://127.0.0.1:3000/console/index.html", wait_until="domcontentloaded", timeout=30000)
        pg.evaluate(
            "data => { try { localStorage.setItem('doctoragent_chats', data); "
            "localStorage.setItem('doctoragent_landing_pref','guest'); } catch(e){} }",
            json.dumps(sessions, ensure_ascii=False),
        )
        pg.reload(wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(1800)
        pg.screenshot(path=str(OUT / "00_landing.png"))

        # 进入（游客偏好应已跳过引导；若仍在则点游客）
        pg.evaluate("window.enterAsGuest ? window.enterAsGuest() : null")
        pg.wait_for_timeout(900)
        pg.screenshot(path=str(OUT / "01_chat.png"))

        # 逐个模块导航 + 截图
        for idx, (view, label) in enumerate(MODULES, start=2):
            try:
                pg.evaluate(
                    "v => { const b=document.querySelector('.sidebar-item[data-view=\"'+v+'\"]'); if(b) b.click(); }",
                    view,
                )
                pg.wait_for_timeout(900)
                pg.screenshot(path=str(OUT / f"{idx:02d}_{view}.png"))
                print(f"[shot] {idx:02d}_{view}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {view}: {e}", flush=True)

        # 回到对话，展示多轮会话
        pg.evaluate("() => { const b=document.querySelector('.sidebar-item[data-view=\"chat\"]'); if(b) b.click(); }")
        pg.wait_for_timeout(1000)
        pg.screenshot(path=str(OUT / "20_chat_sessions.png"))
        # 打开第一个会话
        pg.evaluate("() => { const s=document.querySelector('.chat-session'); if(s) s.click(); }")
        pg.wait_for_timeout(1000)
        pg.screenshot(path=str(OUT / "21_chat_conversation.png"))

        pg.wait_for_timeout(1500)
        browser.close()

    # 找到录制的视频并重命名
    vids = list(VIDEO_DIR.glob("*.webm"))
    if vids:
        vids.sort(key=lambda p: p.stat().st_mtime)
        final = OUT / "doctoragent_demo.webm"
        os.replace(vids[-1], final)
        print("VIDEO:", final, final.stat().st_size // 1024, "KB", flush=True)


if __name__ == "__main__":
    main()
