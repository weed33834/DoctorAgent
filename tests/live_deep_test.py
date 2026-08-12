#!/usr/bin/env python3
"""Deeper live tests: streaming (SSE), long multi-turn context, concurrent
requests, and malicious-code containment. Usage: TEST_KEY=sk-... python tests/live_deep_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

API = "https://zhiyunapi.cc"
KEY = os.environ.get("TEST_KEY", "")
MODEL = os.environ.get("DEEP_MODEL", "deepseek-v4-flash")

# Live tests against an external API — excluded from the default suite and
# skipped unless TEST_KEY is provided.
pytestmark = pytest.mark.integration
if not KEY:
    pytest.skip("TEST_KEY not set; this is a live test against an external API", allow_module_level=True)

results: list[tuple[str, bool, str]] = []


def rep(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def conn():
    from doctoragent.connections.models import AuthMethod, Connection, PlatformType

    return Connection(name="deep", platform_type=PlatformType.OPENAI_COMPATIBLE,
                      base_url=API, model_name=MODEL, api_key=KEY,
                      auth_method=AuthMethod.API_KEY, timeout=60.0)


async def test_streaming() -> None:
    from doctoragent.model.provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(conn(), temperature=0.3)
    try:
        chunks: list[str] = []
        async for delta in provider.chat_completion_stream(
            messages=[{"role": "user", "content": "用一句话介绍你自己"}]
        ):
            if delta:
                chunks.append(delta)
        full = "".join(chunks)
        ok = len(chunks) > 1 and len(full) > 10
        rep("流式输出(SSE)", ok, f"{len(chunks)} chunks / {len(full)} chars")
    except Exception as e:
        rep("流式输出(SSE)", False, str(e)[:90])
    finally:
        await provider.close()


async def test_long_multiturn() -> None:
    """Many turns to exercise context handling (no truncation crash, continuity)."""
    from doctoragent.model.provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(conn(), temperature=0.2)
    history = [{"role": "system", "content": "你是助手，简洁回答。"}]
    try:
        turns = 8
        for i in range(turns):
            history.append({"role": "user", "content": f"这是第{i+1}轮：请回复数字 {i+1}，并说'继续'。"})
            r = await provider.chat_completion(messages=history)
            history.append({"role": "assistant", "content": r})
        last = history[-1]["content"]
        ok = f"{turns}" in last or "继续" in last or str(turns) in last
        rep("长上下文多轮(8轮)", ok, f"final={last[:40]}")
    except Exception as e:
        rep("长上下文多轮(8轮)", False, str(e)[:90])
    finally:
        await provider.close()


async def test_concurrent() -> None:
    """Concurrent requests to the same provider (connection reuse + race check)."""
    from doctoragent.model.provider import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(conn(), temperature=0.3)

    async def one(i: int) -> bool:
        try:
            r = await provider.chat_completion(messages=[{"role": "user", "content": f"返回数字 {i}"}])
            return str(i) in r or r.strip() != ""
        except Exception:
            return False

    t0 = time.time()
    res = await asyncio.gather(*[one(i) for i in range(6)])
    dt = time.time() - t0
    ok = all(res)
    rep("并发请求(6并行)", ok, f"{sum(res)}/6 ok in {dt:.1f}s")
    await provider.close()


async def test_malicious_code_contained() -> None:
    """Sandbox must contain malicious code (no host impact, returns error)."""
    from doctoragent.tools.code_exec_tool import CodeExecTool

    t = CodeExecTool()
    # attempt to read /etc/passwd or write outside workdir
    r = await t.execute(code=(
        "import pathlib\n"
        "try:\n"
        "  data = pathlib.Path('/etc/passwd').read_text()\n"
        "  print('LEAK', data[:20])\n"
        "except Exception as e:\n"
        "  print('BLOCKED', type(e).__name__)\n"
    ))
    out = (r.data or {}).get("stdout", "")
    ok = "LEAK" not in out
    rep("恶意代码隔离(/etc/passwd)", ok, out.strip()[:50])


async def test_tool_loop_guard() -> None:
    """The full agent must not loop forever calling tools (budget guard)."""
    from tempfile import TemporaryDirectory

    from doctoragent.model.agent import AgentConfig, create_agent
    from doctoragent.model.provider import OpenAICompatibleProvider
    from doctoragent.model.rag import MemorySystem
    from doctoragent.tools.code_exec_tool import register_code_exec_tool

    provider = OpenAICompatibleProvider(conn(), temperature=0.2)
    with TemporaryDirectory() as d:
        agent = create_agent(llm_provider=provider, memory_system=MemorySystem(Path(d) / "m.db"),
                             config=AgentConfig(max_iterations=4, max_tool_calls=3,
                                                enable_planning=False, enable_reflection=False))
        register_code_exec_tool(agent.tools)
        t0 = time.time()
        r = await agent.run("请连续调用多次 code_exec 计算 1+1，直到你说停。")
        dt = time.time() - t0
        calls = agent.get_tool_calls() if hasattr(agent, "get_tool_calls") else []
        ok = dt < 90  # budget guard prevents infinite loop
        rep("工具调用预算护栏", ok, f"完成于{dt:.0f}s")
    await provider.close()


async def main() -> None:
    await test_streaming()
    await test_long_multiturn()
    await test_concurrent()
    await test_malicious_code_contained()
    await test_tool_loop_guard()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== DEEP SUMMARY: {passed}/{len(results)} passed ===")
    for n, ok, d in results:
        if not ok:
            print(f"  FAIL: {n} -> {d}")
    sys.exit(0 if passed == len(results) else 2)


if __name__ == "__main__":
    asyncio.run(main())
