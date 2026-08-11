# mypy: ignore-errors
"""Deep audit: concurrency + edge-case tests to expose latent bugs in the
platform modules (semantic cache, workspace, enterprise, a2a, docgen, governance)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest


# ── semantic cache concurrency ────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_cache_concurrent_put_get(tmp_path: Path) -> None:
    from doctoragent.model.semantic_cache import SemanticCache

    cache = SemanticCache(persist_path=tmp_path / "sc.db")

    async def worker(i: int) -> None:
        for k in range(10):
            cache.put(f"q{i}-{k}", f"a{i}-{k}")

    await asyncio.gather(*[worker(i) for i in range(8)])
    # no crash + entries persisted
    assert cache.stats()["entries"] > 0


def test_semantic_cache_thread_safe(tmp_path: Path) -> None:
    """Non-async threads share the cache; must not raise 'database is locked'."""
    from doctoragent.model.semantic_cache import SemanticCache

    cache = SemanticCache(persist_path=tmp_path / "sc.db")
    errors: list[str] = []

    def worker(n: int) -> None:
        try:
            for k in range(50):
                cache.put(f"t{n}-{k}", f"r{n}-{k}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{n}:{exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent write failures: {errors[:3]}"


# ── workspace upsert id stability + concurrency ───────────────────────


def test_workspace_prompt_upsert_keeps_id(tmp_path: Path) -> None:
    from doctoragent.workspace_config import WorkspaceConfig

    ws = WorkspaceConfig(tmp_path / "ws.db")
    ws.upsert_prompt("x", "v1")
    first = ws.get_prompt("x")["id"]
    ws.upsert_prompt("x", "v2")
    assert ws.get_prompt("x")["id"] == first  # ON CONFLICT keeps id
    assert ws.get_prompt("x")["template"] == "v2"


@pytest.mark.asyncio
async def test_workspace_concurrent_writes(tmp_path: Path) -> None:
    from doctoragent.workspace_config import WorkspaceConfig

    ws = WorkspaceConfig(tmp_path / "ws.db")

    async def worker(i: int) -> None:
        for k in range(10):
            ws.upsert_prompt(f"p{i}-{k}", f"t{k}")
            ws.register_skill(f"s{i}-{k}", "d")
            ws.create_expert(f"e{i}-{k}", "t", "p")

    await asyncio.gather(*[worker(i) for i in range(6)])
    assert len(ws.list_prompts()) + len(ws.list_skills()) + len(ws.list_experts()) > 0


# ── a2a concurrent tasks ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a2a_concurrent_tasks() -> None:
    from doctoragent.a2a import A2AServer

    async def handler(m, meta):
        await asyncio.sleep(0.01)
        return "ok"

    server = A2AServer(name="d", description="d", handler=handler)
    ids = []
    for i in range(20):
        r = await server.handle_rpc({"jsonrpc": "2.0", "method": "task/send",
                                     "params": {"message": {"parts": [{"type": "text", "text": str(i)}]}},
                                     "id": i})
        ids.append(r["result"]["id"])
    await asyncio.sleep(0.2)
    assert server.task_count() == 20
    for tid in ids[:5]:
        g = await server.handle_rpc({"jsonrpc": "2.0", "method": "task/get", "params": {"id": tid}, "id": 1})
        assert g["result"]["status"] in ("working", "completed")
    await server.close()


# ── docgen unicode / long text ────────────────────────────────────────


@pytest.mark.parametrize("fmt,prefix", [
    ("md", b"# "), ("pdf", b"%PDF"), ("docx", b"PK"),
])
def test_docgen_unicode_long(fmt: str, prefix: bytes, tmp_path: Path) -> None:
    from doctoragent.docgen import export_messages

    long = "华法林" * 800 + "\n" + "<b>&</b> &amp;" * 50
    msgs = [{"role": "user", "content": long}, {"role": "assistant", "content": "监测 INR：\n- 目标 2-3\n- 定期复查"}]
    out = tmp_path / f"u.{fmt}"
    export_messages(msgs, fmt, out)
    assert out.read_bytes()[: len(prefix)] == prefix


# ── enterprise edge ───────────────────────────────────────────────────


def test_enterprise_authenticate_mfa_flow(tmp_path: Path) -> None:
    from doctoragent.enterprise import EnterpriseService, EnterpriseStore
    from doctoragent.enterprise.security import totp_code

    svc = EnterpriseService(EnterpriseStore(tmp_path / "e.db"))
    org = svc.create_org("o")
    u = svc.create_user(org.id, "a@x.com", "Password123")
    e = svc.mfa_enroll(u.id)
    assert svc.mfa_verify_enroll(u.id, totp_code(e["secret"])) is True
    # after enrolled, login requires mfa
    _, res = svc.authenticate(org.id, "a@x.com", "Password123")
    assert res == "mfa_required"
    # wrong mfa fails
    assert svc.mfa_verify_login(u.id, "000000") is False
    assert svc.mfa_verify_login(u.id, totp_code(e["secret"])) is True


def test_enterprise_bulk_import_mixed(tmp_path: Path) -> None:
    from doctoragent.enterprise import EnterpriseService, EnterpriseStore

    svc = EnterpriseService(EnterpriseStore(tmp_path / "e.db"))
    org = svc.create_org("o")
    res = svc.bulk_import_users(org.id, [
        {"email": "ok1@x.com", "password": "Password123"},
        {"email": "ok2@x.com", "password": "Password123", "role": "admin"},
        {"email": "bad", "password": "Password123"},
        {},  # empty row
    ])
    assert res["created"] == 2
    assert res["failed"] == 2


# ── governance / interop edge ─────────────────────────────────────────


def test_governance_sensitivity_rule_match(tmp_path: Path) -> None:
    from doctoragent.governance import AssetType, DataSensitivity, GovernanceService, GovernanceStore

    svc = GovernanceService(GovernanceStore(tmp_path / "g.db"))
    svc.add_classification_rule("phi", DataSensitivity.PHI, ["身份证号"])
    a = svc.register_asset("病历", AssetType.DOCUMENT, content="患者身份证号110101...")
    assert a.sensitivity == DataSensitivity.PHI


def test_interop_trust_ranking(tmp_path: Path) -> None:
    from doctoragent.interop import InteropService, InteropStore, TrustLevel

    svc = InteropService(InteropStore(tmp_path / "i.db"))
    svc.add_policy("p", require_trust=TrustLevel.TRUSTED)
    assert svc.check_access("a", "act", TrustLevel.UNTRUSTED)["allowed"] is False
    assert svc.check_access("a", "act", TrustLevel.HIGH)["allowed"] is True
