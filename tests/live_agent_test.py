#!/usr/bin/env python3
"""Full ReAct Agent loop test against a live model: a complex multi-tool task
(code execution + memory) verifying the orchestrator loop, tool dispatch,
budget guard and reflection. Usage: TEST_KEY=sk-... python tests/live_agent_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = "https://zhiyunapi.cc"
KEY = os.environ.get("TEST_KEY", "")
MODEL = os.environ.get("AGENT_MODEL", "deepseek-v4-flash")


async def main() -> None:
    if not KEY:
        print("TEST_KEY env required"); sys.exit(1)
    from tempfile import TemporaryDirectory

    from doctoragent.connections.models import AuthMethod, Connection, PlatformType
    from doctoragent.model.agent import AgentConfig, create_agent
    from doctoragent.model.provider import OpenAICompatibleProvider
    from doctoragent.tools.code_exec_tool import register_code_exec_tool

    conn = Connection(name="live", platform_type=PlatformType.OPENAI_COMPATIBLE,
                      base_url=API, model_name=MODEL, api_key=KEY,
                      auth_method=AuthMethod.API_KEY, timeout=60.0)
    provider = OpenAICompatibleProvider(conn, temperature=0.2)

    with TemporaryDirectory() as d:
        from doctoragent.model.rag import MemorySystem

        memory = MemorySystem(Path(d) / "mem.db")
        agent = create_agent(
            llm_provider=provider,
            memory_system=memory,
            config=AgentConfig(max_iterations=8, max_tool_calls=4,
                               enable_planning=True, enable_reflection=True),
        )
        register_code_exec_tool(agent.tools)
        names = {t.name for t in agent.tools.list_tools()}
        print("tools:", sorted(names))

        print("\n--- TASK 1: 多工具(代码执行) ---")
        r1 = await agent.run("用 Python 计算 123*456，然后说结果，并简要说明这是怎么算的。")
        print("RESULT1:", r1[:300].replace("\n", " "))

        print("\n--- TASK 2: 记忆 + 引用 ---")
        r2 = await agent.run("记住：我的收缩压目标是130。然后问我一个问题，验证我是否记住了。")
        print("RESULT2:", r2[:300].replace("\n", " "))

        print("\n--- TASK 3: 反射/纠错（触发工具错误后重试） ---")
        r3 = await agent.run("用 Python 运行一段会先报错、然后你修复并成功打印 2 的代码（提示：NameError 改为定义变量）。")
        print("RESULT3:", r3[:300].replace("\n", " "))

        # memory persistence check
        facts = memory.recall_facts("收缩压", limit=3)
        print("\nmemory facts:", [(f.content, f.memory_type) for f in facts])

    await provider.close()
    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(main())
