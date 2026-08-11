# mypy: ignore-errors
"""Tests for conversation capabilities: sandbox code-exec, doc export, workspace config, manage tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from doctoragent.docgen import export_messages, messages_to_markdown
from doctoragent.tools.code_exec_tool import CodeExecTool
from doctoragent.workspace_config import WorkspaceConfig, _extract_vars


# ── workspace config (prompts / skills / experts) ─────────────────────


@pytest.fixture
def ws(tmp_path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(tmp_path / "ws.db")


def test_prompt_upsert_and_variables(ws: WorkspaceConfig) -> None:
    ws.upsert_prompt("临床助手", "你是{role}，请{task}。", description="通用")
    p = ws.list_prompts()[0]
    assert p["name"] == "临床助手"
    assert set(p["variables"]) == {"role", "task"}
    # upsert same name updates
    ws.upsert_prompt("临床助手", "新模板{role}")
    assert ws.get_prompt("临床助手")["template"] == "新模板{role}"


def test_skill_and_expert(ws: WorkspaceConfig) -> None:
    ws.register_skill("文献检索", "检索医学文献", triggers=["search", "检索"])
    ws.create_expert("药师", "临床药师", "你是资深临床药师")
    assert len(ws.list_skills()) == 1
    assert ws.list_skills()[0]["triggers"] == ["search", "检索"]
    assert len(ws.list_experts()) == 1
    assert ws.summary() == {"prompts": 0, "skills": 1, "experts": 1}


def test_extract_vars() -> None:
    assert set(_extract_vars("你好{name}，{task}")) == {"name", "task"}
    assert _extract_vars("无变量") == []


# ── doc export ────────────────────────────────────────────────────────


def test_messages_to_markdown() -> None:
    md = messages_to_markdown([{"role": "user", "content": "Q"},
                               {"role": "assistant", "content": "A"}])
    assert "Q" in md and "A" in md and "USER" in md


def test_export_md(tmp_path: Path) -> None:
    msgs = [{"role": "user", "content": "华法林？"}, {"role": "assistant", "content": "监测 INR。"}]
    out = tmp_path / "chat.md"
    export_messages(msgs, "md", out)
    assert out.read_text(encoding="utf-8").startswith("# ")


def test_export_pdf(tmp_path: Path) -> None:
    msgs = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}]
    out = tmp_path / "chat.pdf"
    export_messages(msgs, "pdf", out)
    assert out.stat().st_size > 500
    assert out.read_bytes()[:4] == b"%PDF"


def test_export_docx(tmp_path: Path) -> None:
    msgs = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}]
    out = tmp_path / "chat.docx"
    export_messages(msgs, "docx", out)
    assert out.stat().st_size > 500
    assert out.read_bytes()[:2] == b"PK"


def test_export_unsupported(tmp_path: Path) -> None:
    from doctoragent.docgen import DocExportError

    with pytest.raises(DocExportError):
        export_messages([], "xyz", tmp_path / "x.xyz")


# ── sandbox code execution ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_exec_calc() -> None:
    t = CodeExecTool()
    r = await t.execute(code="print(6*7)")
    assert r.success is True
    assert "42" in (r.data or {}).get("stdout", "")


@pytest.mark.asyncio
async def test_code_exec_chart_image() -> None:
    t = CodeExecTool()
    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.bar(['A','B'],[3,7])\n"
        "plt.savefig('chart.png')\n"
        "print('ok')\n"
    )
    r = await t.execute(code=code)
    assert r.success is True
    assert (r.data or {}).get("image", "").startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_code_exec_error_surface() -> None:
    t = CodeExecTool()
    r = await t.execute(code="raise ValueError('boom')")
    assert r.success is False
    assert "boom" in (r.error or "")


# ── manage tools (chat → management) ──────────────────────────────────


@pytest.fixture
def registry() -> object:
    from doctoragent.model.tools import ToolRegistry

    return ToolRegistry()


def test_manage_tools_registered_and_callable(ws: WorkspaceConfig, registry: object) -> None:
    from doctoragent.tools.manage_tools import register_workspace_tools

    names = register_workspace_tools(registry, ws)
    assert "create_prompt" in names
    assert "create_expert" in names
    assert "register_skill" in names

    create_prompt = registry.get("create_prompt")
    create_expert = registry.get("create_expert")

    async def run():
        r1 = await create_prompt.execute(name="专家提示", template="你是{domain}专家")
        r2 = await create_expert.execute(name="药师", title="临床药师", system_prompt="你是资深药师")
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.success and r2.success
    # The change is visible in the shared store (management UI reads this).
    assert ws.get_prompt("专家提示")["template"] == "你是{domain}专家"
    assert ws.list_experts()[0]["name"] == "药师"


# ── sandbox security (malicious code must be contained) ───────────────


@pytest.mark.asyncio
async def test_sandbox_blocks_host_secrets() -> None:
    """Malicious code must not read host secrets when OS isolation is effective."""
    from doctoragent.security.sandbox import SandboxManager
    from doctoragent.tools.code_exec_tool import CodeExecTool

    if not SandboxManager.isolation_effective():
        # No real isolation available → code_exec must REFUSE (fail-closed).
        t = CodeExecTool()
        r = await t.execute(code="print('x')")
        assert r.success is False
        assert "refused" in (r.error or "").lower() or "sandbox" in (r.error or "").lower()
        return
    t = CodeExecTool()
    r = await t.execute(code=(
        "import pathlib\n"
        "try:\n"
        "  leaked = pathlib.Path('/etc/passwd').read_text()[:20]\n"
        "  print('LEAK', leaked)\n"
        "except Exception:\n"
        "  print('BLOCKED')\n"
    ))
    out = (r.data or {}).get("stdout", "")
    assert "LEAK" not in out  # host secret must never leak
    assert "BLOCKED" in out   # the read was refused
