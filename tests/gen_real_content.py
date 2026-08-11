#!/usr/bin/env python3
"""Generate real multi-turn clinical content via the live gateway (step-3.5-flash)."""
import asyncio, json, os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from doctoragent.connections.models import AuthMethod, Connection, PlatformType
from doctoragent.model.provider import OpenAICompatibleProvider
KEY = os.environ.get("K2")
QUESTIONS = [
    "华法林和布洛芬能一起吃吗？请给用药建议。",
    "他有肝硬化，止痛药怎么选、剂量怎么调？",
    "请用 Python 计算：体重70kg、每日1.5mg/kg 的总剂量，并给出用药注意。",
]
async def main():
    conn = Connection(name="h", platform_type=PlatformType.OPENAI_COMPATIBLE, base_url="https://api.hcnsec.cn",
                      model_name="step-3.5-flash", api_key=KEY, auth_method=AuthMethod.API_KEY,
                      is_local=True, is_cloud_authorized=True, timeout=60)
    p = OpenAICompatibleProvider(conn, temperature=0.3)
    msgs = [{"role": "system", "content": "你是临床药师，回答专业、有依据、分点清晰。"}]
    out = []
    for q in QUESTIONS:
        msgs.append({"role": "user", "content": q})
        a = await p.chat_completion(messages=msgs)
        out.append({"q": q, "a": a})
        msgs.append({"role": "assistant", "content": a})
    json.dump(out, open("/tmp/real_chat.json", "w"), ensure_ascii=False)
    print("saved", len(out), "turns")
    await p.close()
asyncio.run(main())
