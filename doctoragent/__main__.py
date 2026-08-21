"""CLI entry point for DoctorAgent."""

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import click

from doctoragent.config import AegisConfig
from doctoragent.execution.vault import VaultManager
from doctoragent.orchestration.agent import AegisAgent
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.security import windows_hello
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.master_key import MasterKeyProvider, create_master_key_provider
from doctoragent.security.windows_hello import WindowsHelloError

logger = logging.getLogger(__name__)


def _master_key_storage_path(config: AegisConfig) -> Path:
    return config.paths.connections.parent / "master_key.bin"


def _build_audit_logger(config: AegisConfig, provider: MasterKeyProvider | None) -> AuditLogger:
    """Build the audit logger with a master-key-derived HMAC key when possible.

    Prefer HKDF(master_key) over the legacy ``<logs>/.audit.key`` file so the
    audit chain cannot be re-signed by anyone who only has disk access. Two
    guardrails:

    * an existing ``.audit.key`` keeps being used (switching keys mid-chain
      would make every historical record fail verification);
    * any failure to obtain the master key falls back to legacy behaviour
      with a warning instead of blocking startup.
    """
    legacy_key_path = config.paths.logs / ".audit.key"
    if provider is not None and not legacy_key_path.exists():
        try:
            from doctoragent.security.keytree import derive_audit_key

            return AuditLogger(config, hmac_key=derive_audit_key(provider.get_key()))
        except Exception as exc:  # noqa: BLE001 — audit must never block startup
            logger.warning(
                "Audit key derivation from master key failed (%s); "
                "falling back to legacy .audit.key",
                exc,
            )
    elif provider is not None and legacy_key_path.exists():
        logger.info(
            "Legacy .audit.key found; continuing to use it for chain "
            "continuity. Delete it to migrate to master-key-derived audit keys."
        )
    return AuditLogger(config)


def _configure_logging(debug: bool) -> None:
    from doctoragent.observability import configure_logging

    configure_logging(debug=debug)


def _create_agent(config: AegisConfig) -> AegisAgent | None:
    try:
        master_key_provider: MasterKeyProvider | None = None
        if config.security.windows_hello_enabled:
            storage_path = _master_key_storage_path(config)
            hello_salt = windows_hello.get_key_derivation_salt(storage_path)
            master_key_provider = create_master_key_provider(
                config.security.master_key_provider,
                storage_path,
                password=config.security.master_key_password,
                hello_salt=hello_salt,
            )
        agent = AegisAgent(
            config,
            audit_logger=_build_audit_logger(config, master_key_provider),
            master_key_provider=master_key_provider,
        )
        # Rebuild in-memory state for tasks left incomplete by a previous run
        # (crash / SIGKILL). Previously this was defined but never invoked, so
        # interrupted tasks silently stayed in their last persisted state.
        try:
            resumed = agent.resume_incomplete()
            if resumed:
                logger.info("Resumed %d incomplete task(s) from prior run", len(resumed))
        except Exception as exc:  # noqa: BLE001 — startup must not crash on resume
            logger.warning("resume_incomplete failed: %s", exc)
        return agent
    except (RuntimeError, FileNotFoundError) as exc:
        if isinstance(exc, WindowsHelloError):
            raise
        logger.warning("Cannot initialize master key: %s", exc)
        return None


def _task_store(config: AegisConfig) -> TaskStore:
    return TaskStore(config.paths.index / "tasks.db")


def _count_files(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(1 for entry in path.iterdir() if entry.is_file())


def _count_vault_files(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(1 for entry in path.rglob("*") if entry.is_file())


# ---------------------------------------------------------------------------
# Common click options
# ---------------------------------------------------------------------------


def _common_options(f):
    f = click.option("--inbox", type=click.Path(path_type=Path), help="Override Inbox directory.")(
        f
    )
    f = click.option("--vault", type=click.Path(path_type=Path), help="Override Vault directory.")(
        f
    )
    f = click.option("--index", type=click.Path(path_type=Path), help="Override Index directory.")(
        f
    )
    f = click.option("--debug", is_flag=True, help="Enable debug logging.")(f)
    return f


def _build_config(**kwargs) -> AegisConfig:
    config = AegisConfig.load_from_file()
    if kwargs.get("debug"):
        config.debug = True
    _configure_logging(config.debug)
    if kwargs.get("inbox"):
        config.paths.inbox = kwargs["inbox"]
    if kwargs.get("vault"):
        config.paths.vault = kwargs["vault"]
    if kwargs.get("index"):
        config.paths.index = kwargs["index"]
    return config


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """DoctorAgent - 临床AI智能体，支持用药安全审查、危急值预警、病历文书生成与合规审计。"""


@cli.command()
@click.argument("query")
@click.option("--semantic", is_flag=True, help="Enable semantic search via embeddings.")
@click.option("--top-k", type=int, default=5, help="Number of results to return.")
@_common_options
def search(query, semantic, top_k, **kwargs):
    """Search vault content by keywords."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is not None:
        from doctoragent.api.schemas import SearchQuery

        async def _search():
            return await agent.search(SearchQuery(query=query, top_k=top_k, semantic=semantic))

        results = asyncio.run(_search())
    else:
        store = _task_store(config)
        try:
            results = store.search(query, top_k=top_k)
        except (OSError, ValueError, RuntimeError) as exc:
            click.echo(f"Search failed: {exc}", err=True)
            return 1
    if not results:
        click.echo(f"No results found for: {query}")
        return 0
    for i, r in enumerate(results, 1):
        click.echo(f"{i}. {r.vault_path}")
        click.echo(f"   Category: {r.category}")
        click.echo(f"   Summary: {r.summary}")
        click.echo(f"   Score:   {r.score:.4f}")
        click.echo()
    return 0


@cli.command()
@_common_options
def status(**kwargs):
    """Show agent status (inbox/vault counts, recent tasks)."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    inbox_count = _count_files(config.paths.inbox)
    vault_count = _count_vault_files(config.paths.vault)

    click.echo("=== DoctorAgent Status ===")
    click.echo(f"  Inbox files : {inbox_count}")
    click.echo(f"  Vault files : {vault_count}")
    click.echo()

    if agent is not None:
        recent = agent.task_store.list_recent(limit=5)
    else:
        store = _task_store(config)
        recent = store.list_recent(limit=5)

    if recent:
        click.echo("Recent tasks:")
        for s in recent:
            src = str(s.source_path) if s.source_path else "N/A"
            click.echo(f"  {s.task_id}  [{s.state}]  {src}")
            if s.message:
                click.echo(f"    {s.message}")
    else:
        click.echo("No recent tasks.")
    return 0


@cli.command()
@click.argument("category", required=False, default=None)
@_common_options
def list_files(category, **kwargs):
    """List vault files, optionally by category."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is not None:
        items = agent.task_store.list_vault_files(category)
    else:
        store = _task_store(config)
        items = store.list_vault_files(category)
    if not items:
        msg = f" in category '{category}'" if category else ""
        click.echo(f"No vault files found{msg}.")
        return 0
    msg = f" in category '{category}'" if category else ""
    click.echo(f"Vault files{msg} ({len(items)} total):")
    click.echo()
    for i, item in enumerate(items, 1):
        click.echo(f"{i}. {item['vault_path']}")
        click.echo(f"   Category: {item['category']}")
        if item["summary"]:
            click.echo(f"   Summary: {item['summary']}")
        if item["tags"]:
            click.echo(f"   Tags:    {', '.join(item['tags'])}")
        click.echo()
    return 0


@cli.command()
@click.argument("output_dir", type=click.Path(path_type=Path))
@click.option("--category", default=None, help="Filter by category.")
@click.option("--query", default=None, help="Search query to filter files.")
@_common_options
def export(output_dir, category, query, **kwargs):
    """Export (decrypt) vault files to a directory."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None or agent.master_key_provider is None:
        click.echo("Cannot export: master key is not configured.", err=True)
        return 1
    items = agent.task_store.list_vault_files(category)
    if not items:
        msg = f" in category '{category}'" if category else ""
        click.echo(f"No vault files found{msg}.")
        return 0
    if query:
        from doctoragent.api.schemas import SearchQuery

        async def _search():
            return await agent.search(SearchQuery(query=query))

        results = asyncio.run(_search())
        result_paths = {str(r.vault_path) for r in results}
        items = [item for item in items if str(item["vault_path"]) in result_paths]
        if not items:
            click.echo(f"No vault files match query: {query}")
            return 0

    from doctoragent.security.keytree import derive_vault_key

    vault_key = derive_vault_key(agent.master_key_provider.get_key())
    output_dir.mkdir(parents=True, exist_ok=True)
    vault_manager = VaultManager(config.paths.vault, vault_key, agent.audit_logger)
    exported = 0
    for item in items:
        vault_path = Path(item["vault_path"])
        if not vault_path.exists():
            continue
        dest = output_dir / vault_path.name
        if dest.exists():
            suffix = os.urandom(8).hex()
            dest = output_dir / f"{dest.stem}_{suffix}{dest.suffix}"
        try:
            vault_manager.decrypt(vault_path, item["salt"], dest)
            exported += 1
            click.echo(f"  Exported: {dest}")
        except (OSError, ValueError, RuntimeError) as exc:
            click.echo(f"  Failed:   {vault_path} ({exc})", err=True)
    click.echo(f"\nExported {exported}/{len(items)} file(s) to {output_dir}")
    return 0


@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind address.")
@click.option("--port", type=int, default=8000, help="TCP port.")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development.")
@_common_options
def serve(host, port, reload, **kwargs):
    """Start the DoctorAgent API server."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None:
        click.echo("Cannot start API server: master key is not configured.", err=True)
        return 1
    from doctoragent.api.server import is_available, run_server

    if not is_available():
        click.echo("FastAPI is required. Install with: pip install doctoragent[server]", err=True)
        return 1
    run_server(config, agent, host=host, port=port, reload=reload)
    return 0


def _resolve_import_targets(paths, is_dir):
    targets = []
    for p in paths:
        if is_dir:
            if not p.is_dir():
                logger.warning("Skip (not a directory): %s", p)
                continue
            for f in sorted(p.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    targets.append(f)
        else:
            if not p.is_file():
                logger.warning("Skip (not a file): %s", p)
                continue
            if p.is_symlink():
                logger.warning("Skip (symlink): %s", p)
                continue
            targets.append(p)
    return targets


def _stage_into_inbox(src, inbox, move):
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / src.name
    if dest.exists():
        suffix = os.urandom(4).hex()
        dest = inbox / f"{dest.stem}_{suffix}{dest.suffix}"
    if move:
        shutil.move(str(src), str(dest))
    else:
        shutil.copy2(str(src), str(dest))
    return dest


async def _process_import_batch(agent, events):
    from doctoragent.api.schemas import TaskStatus

    results = []
    for ev in events:
        try:
            status = await agent.on_file_event(ev)
            results.append(status)
        except (RuntimeError, OSError, ValueError) as exc:
            results.append(TaskStatus(task_id=ev.event_id, state="FAILED", message=str(exc)))
    return results


@cli.command()
@click.argument("paths", nargs=-1, type=click.Path(path_type=Path), required=True)
@click.option(
    "--dir", "is_dir", is_flag=True, help="Treat paths as directories; ingest recursively."
)
@click.option("--move", is_flag=True, help="Move originals instead of copying.")
@click.option("--no-wait", is_flag=True, help="Stage files and exit (no processing).")
@_common_options
def import_files(paths, is_dir, move, no_wait, **kwargs):
    """Batch-ingest files into the Vault."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None or agent.master_key_provider is None:
        click.echo("Cannot import: master key is not configured.", err=True)
        return 1
    from doctoragent.api.schemas import FileEvent

    targets = _resolve_import_targets(list(paths), is_dir)
    if not targets:
        click.echo("No files to import.", err=True)
        return 1
    click.echo(f"Importing {len(targets)} file(s)...")
    inbox = config.paths.inbox
    if no_wait:
        count = 0
        for src in targets:
            try:
                staged = _stage_into_inbox(src, inbox, move)
                click.echo(f"  Staged: {staged}")
                count += 1
            except OSError as exc:
                click.echo(f"  Failed to stage {src}: {exc}", err=True)
        click.echo(f"\nStaged {count}/{len(targets)} file(s) into {inbox}.")
        return 0 if count == len(targets) else 1
    events = []
    staged_sources = []
    for src in targets:
        try:
            staged = _stage_into_inbox(src, inbox, move)
            events.append(FileEvent(event_id=uuid4(), source_path=staged, event_type="created"))
            staged_sources.append(src)
        except OSError as exc:
            click.echo(f"  Failed to stage {src}: {exc}", err=True)
    if not events:
        click.echo("No files staged successfully.", err=True)
        return 1
    statuses = asyncio.run(_process_import_batch(agent, events))
    succeeded = 0
    failed = 0
    for src, status in zip(staged_sources, statuses, strict=True):
        if status.state == "COMPLETED":
            succeeded += 1
            click.echo(f"  OK:       {src}  ->  {status.task_id}")
        else:
            failed += 1
            msg = f"  ({status.message})" if status.message else ""
            click.echo(f"  {status.state}: {src}{msg}", err=True)
    click.echo(f"\nImported {succeeded}/{len(staged_sources)} staged ({failed} failed).")
    return 0 if failed == 0 else 1


@cli.command()
@click.option("--name", default="stdin.txt", help="Filename for piped content.")
@click.option("--no-wait", is_flag=True, help="Stage and exit (no processing).")
@_common_options
def pipe(name, no_wait, **kwargs):
    """Read stdin into the Vault (pipe mode for shell pipelines)."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None or agent.master_key_provider is None:
        click.echo("Cannot pipe: master key is not configured.", err=True)
        return 1
    from doctoragent.api.schemas import FileEvent

    data = sys.stdin.buffer.read()
    if not data:
        click.echo("No data received on stdin.", err=True)
        return 1
    inbox = config.paths.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / name
    if dest.exists():
        suffix = os.urandom(4).hex()
        dest = inbox / f"{dest.stem}_{suffix}{dest.suffix}"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    if no_wait:
        click.echo(f"Staged stdin ({len(data)} bytes) as {dest}")
        return 0
    event = FileEvent(event_id=uuid4(), source_path=dest, event_type="created")
    status = asyncio.run(agent.on_file_event(event))
    if status.state == "COMPLETED":
        click.echo(f"OK: stdin ({len(data)} bytes) -> {status.task_id}")
        return 0
    msg = f" ({status.message})" if status.message else ""
    click.echo(f"{status.state}: stdin{msg}", err=True)
    return 1


@cli.command()
@click.argument("script", type=click.Path(path_type=Path))
@click.option("--dry-run", is_flag=True, help="Validate without executing.")
@_common_options
def run(script, dry_run, **kwargs):
    """Execute a JSON orchestration script (batch workflows)."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None or agent.master_key_provider is None:
        click.echo("Cannot run script: master key is not configured.", err=True)
        return 1
    from doctoragent.api.schemas import FileEvent

    if str(script) == "-":
        raw = sys.stdin.read()
    else:
        raw = script.read_text(encoding="utf-8")
    try:
        steps = json.loads(raw) if isinstance(json.loads(raw), list) else [json.loads(raw)]
    except (json.JSONDecodeError, ValueError) as exc:
        click.echo(f"Invalid script JSON: {exc}", err=True)
        return 1

    if dry_run:
        click.echo(f"Script validated: {len(steps)} step(s) parsed.")
        for i, step in enumerate(steps, 1):
            action = step.get("action", "unknown")
            src = step.get("source", step.get("path", "N/A"))
            click.echo(f"  {i}. [{action}] {src}")
        return 0

    results = []
    for step in steps:
        action = step.get("action", "import")
        source = step.get("source") or step.get("path", "")
        move = step.get("move", False)
        src_path = Path(source)
        if not src_path.exists():
            results.append({"source": source, "state": "FAILED", "message": "File not found"})
            continue
        inbox = config.paths.inbox
        staged = _stage_into_inbox(src_path, inbox, move)
        event = FileEvent(event_id=uuid4(), source_path=staged, event_type="created")
        try:
            status = asyncio.run(agent.on_file_event(event))
            results.append(
                {"source": source, "state": status.state, "task_id": str(status.task_id)}
            )
        except (RuntimeError, OSError, ValueError) as exc:
            results.append({"source": source, "state": "FAILED", "message": str(exc)})

    click.echo(json.dumps(results, indent=2, default=str))
    failed = any(r.get("state") == "FAILED" for r in results)
    return 1 if failed else 0


@cli.command()
@click.option("--local-root", type=click.Path(path_type=Path), help="Local backup root.")
@_common_options
def backup(local_root, **kwargs):
    """Trigger an incremental backup to the configured storage backend."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None:
        click.echo("Agent not available; check master key configuration.", err=True)
        return 1
    from doctoragent.integrations.storage import backup_vault_to_backend, create_storage_backend

    integrations = config.integrations
    if not integrations.storage_enabled:
        click.echo("Remote storage is disabled (storage_enabled=False).", err=True)
        return 1
    try:
        backend = create_storage_backend(
            integrations,
            local_root=local_root or (config.paths.vault.parent / "Backups"),
        )
    except Exception as exc:
        click.echo(f"Storage backend misconfigured: {exc}", err=True)
        return 1
    result = backup_vault_to_backend(
        config.paths.vault,
        backend,
        audit_logger=getattr(agent, "audit_logger", None),
    )
    click.echo(f"Backend: {backend.backend_name}")
    click.echo(
        f"Uploaded: {len(result.uploaded)}, Skipped: {len(result.skipped)}, Removed: {len(result.removed)}"
    )
    if result.error:
        click.echo(f"Error: {result.error}", err=True)
    return 0 if result.ok else 1


@cli.command()
@click.option("--event", default="test", help="Event type to fire.")
@_common_options
def webhook_test(event, **kwargs):
    """Fire a test webhook event to configured endpoints."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None:
        click.echo("Agent not available.", err=True)
        return 1
    count = agent.dispatch_webhook(event, {"triggered_by": "cli", "test": True})
    click.echo(f"Dispatched to {count} endpoint(s).")
    return 0 if count > 0 else 1


@cli.command()
@click.argument("question")
@click.option("--top-k", type=int, default=5, help="Number of results to return.")
@click.option("--session-id", default=None, help="Conversation session ID for memory.")
@click.option("--no-memory", is_flag=True, help="Disable memory system.")
@_common_options
def ask(question, top_k, session_id, no_memory, **kwargs):
    """Ask a question about your vault content using RAG with context engineering."""
    config = _build_config(**kwargs)
    agent = _create_agent(config)
    if agent is None:
        click.echo("Cannot ask: master key is not configured.", err=True)
        return 1
    return cmd_ask(agent, config, question, top_k, session_id, no_memory)


@cli.command()
@click.argument("task")
@click.option("--max-iterations", type=int, default=10, help="Maximum reasoning iterations.")
@click.option("--verbose", is_flag=True, help="Show execution trajectory.")
@_common_options
def agent(task, max_iterations, verbose, **kwargs):
    """Run the intelligent agent with tool calling and reasoning.

    The agent can search documents, analyze content, extract information,
    compare documents, and execute multi-step tasks.
    """
    config = _build_config(**kwargs)
    agent_instance = _create_agent(config)
    if agent_instance is None:
        click.echo("Cannot run agent: master key is not configured.", err=True)
        return 1
    return cmd_agent(agent_instance, config, task, max_iterations, verbose)


@cli.group()
def clinical():
    """临床分析工具（用药安全审查、危急值预警、病历文书生成）。"""


@clinical.command("analyze")
@click.option("--patient-id", required=True, help="患者ID")
@click.option(
    "--medications",
    multiple=True,
    help="用药列表，可重复提供（如 --medications warfarin --medications ibuprofen）",
)
@click.option(
    "--allergies",
    multiple=True,
    help="过敏列表，可重复提供（如 --allergies penicillin）",
)
@click.option(
    "--vitals",
    multiple=True,
    help="生命体征，格式 key=value（如 --vitals hr=80 --vitals sbp=120）",
)
@click.option("--question", default="", help="临床问题")
@_common_options
def clinical_analyze(patient_id, medications, allergies, vitals, question, **kwargs):
    """执行临床工作流分析。

    并行运行病史 / 用药安全 / 文献检索专家 Agent，叠加确定性规则引擎结果
    （规则优先于 LLM），生成 SOAP 病历草稿与 ICD-10 编码建议。LLM 未配置时
    仅返回确定性规则结果。
    """
    _build_config(**kwargs)

    from doctoragent.clinical.agents.orchestrator import ClinicalOrchestrator
    from doctoragent.clinical.tools import create_clinical_registry

    vitals_dict: dict[str, float] = {}
    for entry in vitals:
        if "=" in entry:
            key, value = entry.split("=", 1)
            try:
                vitals_dict[key.strip()] = float(value.strip())
            except ValueError:
                logger.warning("忽略无法解析的生命体征项: %s", entry)

    patient_context = {
        "patient_id": patient_id,
        "medications": list(medications),
        "allergies": list(allergies),
        "vitals": vitals_dict,
    }

    async def _run_clinical():
        registry = create_clinical_registry()
        orchestrator = ClinicalOrchestrator(llm_provider=None, clinical_registry=registry)
        return await orchestrator.analyze(patient_context, question)

    result = asyncio.run(_run_clinical())
    click.echo(
        json.dumps(
            result.model_dump(),
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


@cli.command()
@click.option(
    "--shell", type=click.Choice(["bash", "zsh", "fish"]), default="bash", help="Target shell."
)
def completion(shell):
    """Emit a shell completion script (uses click's built-in completion)."""
    try:
        from click.shell_completion import get_completion_class
    except ImportError:
        click.echo("Shell completion requires Click 8.0+.", err=True)
        return 1
    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        click.echo(f"Unsupported shell: {shell}", err=True)
        return 1
    comp = comp_cls(
        cli=cli, ctx_args={}, prog_name="doctoragent", complete_var="_DOCTORAGENT_COMPLETE"
    )
    click.echo(comp.source())
    return 0


# ---------------------------------------------------------------------------
# Daemon mode (original default behaviour)
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--no-tray", is_flag=True, help="Run in headless mode without the system tray UI.")
@_common_options
def daemon(no_tray, **kwargs):
    """Run the DoctorAgent agent (monitoring loop, optional tray UI)."""
    config = _build_config(**kwargs)

    from doctoragent.config import PathConfig

    settings_path = PathConfig().settings
    if not settings_path.exists():
        logger.info("Settings file not found — launching first-run wizard.")
        from PyQt6.QtWidgets import QApplication

        from doctoragent.presentation.first_run_wizard import FirstRunWizard

        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv[:1])
        wizard = FirstRunWizard(AegisConfig())
        wizard.exec()
        logger.info("First-run wizard completed.")

    master_key_provider: MasterKeyProvider | None = None
    try:
        if config.security.windows_hello_enabled:
            storage_path = _master_key_storage_path(config)
            hello_salt = windows_hello.get_key_derivation_salt(storage_path)
            master_key_provider = create_master_key_provider(
                config.security.master_key_provider,
                storage_path,
                password=config.security.master_key_password,
                hello_salt=hello_salt,
            )
    except (RuntimeError, FileNotFoundError) as exc:
        if isinstance(exc, WindowsHelloError):
            raise
        logger.warning("Cannot initialize master key: %s", exc)
        return 1
    audit_logger = _build_audit_logger(config, master_key_provider)
    agent = AegisAgent(config, audit_logger=audit_logger, master_key_provider=master_key_provider)
    # Resume tasks interrupted by a prior crash/SIGKILL (parity with serve path).
    try:
        resumed = agent.resume_incomplete()
        if resumed:
            logger.info("Resumed %d incomplete task(s) from prior run", len(resumed))
    except Exception as exc:  # noqa: BLE001 — startup must not crash on resume
        logger.warning("resume_incomplete failed: %s", exc)
    logger.info("DoctorAgent is starting...")

    if no_tray:
        _run_headless(agent)
    else:
        try:
            _run_with_tray(agent, config)
        except ImportError as exc:
            logger.warning("Tray UI unavailable (%s); falling back to headless mode.", exc)
            _run_headless(agent)
    return 0


def _run_asyncio_loop(loop, shutdown):
    asyncio.set_event_loop(loop)
    while not shutdown.is_set():
        loop.run_until_complete(asyncio.sleep(0.2))


def _run_headless(agent):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown = threading.Event()
    # Handle SIGTERM (systemctl stop / kill) so the cleanup path runs
    # instead of being skipped by the default termination handler.
    try:
        signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    except (ValueError, OSError):
        # Not in the main thread or signals unsupported on this platform.
        pass
    agent.start_monitoring(loop)
    try:
        while not shutdown.is_set():
            try:
                loop.run_until_complete(asyncio.sleep(0.5))
            except KeyboardInterrupt:
                shutdown.set()
    finally:
        try:
            loop.run_until_complete(agent.aclose())
        except Exception as e:  # noqa: BLE001 - best-effort shutdown cleanup
            logger.warning("aclose() failed during shutdown: %s", e)
        except KeyboardInterrupt:
            # Already shutting down; do not let a late interrupt block cleanup.
            pass
        agent.stop_monitoring()
        if loop.is_running():
            loop.call_soon(loop.stop)
        loop.close()


def _run_with_tray(agent, config):
    loop = asyncio.new_event_loop()
    shutdown = threading.Event()
    asyncio_thread = threading.Thread(
        target=_run_asyncio_loop,
        args=(loop, shutdown),
        daemon=True,
    )
    asyncio_thread.start()
    agent.start_monitoring(loop)
    tray = _create_tray_app(config, agent.audit_logger, agent)
    try:
        tray.run()
    finally:
        shutdown.set()
        agent.stop_monitoring()
        asyncio_thread.join(timeout=2)
        try:
            loop.run_until_complete(agent.aclose())
        except Exception as e:  # noqa: BLE001 - best-effort shutdown cleanup
            logger.warning("aclose() failed during shutdown: %s", e)
        except KeyboardInterrupt:
            pass
        loop.close()


def main(args=None):
    """Entry point for the DoctorAgent CLI.

    Wraps :func:`cli` with ``standalone_mode=False`` so that tests can
    capture the return value instead of having ``SystemExit`` raised.
    """
    return cli.main(args=args, standalone_mode=False)


# ---------------------------------------------------------------------------
# Backward-compatible aliases (tests use these names)
# ---------------------------------------------------------------------------

run_headless = _run_headless
run_with_tray = _run_with_tray


def _create_tray_app(config: AegisConfig, audit_logger: Any = None, agent: Any = None) -> Any:
    """Import and create the tray application.

    When *agent* is supplied with a configured master-key provider, the vault
    key is derived from the master key so the tray's VaultBrowser can decrypt
    previews. Previously this was always ``None``, leaving the browser unable
    to decrypt anything.
    """
    from doctoragent.presentation.tray import TrayApplication

    vault_key = None
    provider = getattr(agent, "master_key_provider", None) if agent is not None else None
    if provider is not None:
        try:
            from doctoragent.security.keytree import derive_vault_key

            master_key = provider.get_key()
            if master_key:
                vault_key = derive_vault_key(master_key)
        except Exception as e:  # noqa: BLE001 - tray should still launch without decryption
            logger.warning("Failed to derive vault_key for tray: %s", e)
    return TrayApplication(config=config, audit_logger=audit_logger, vault_key=vault_key)


def cmd_search(agent, query, semantic=False, top_k=5):
    """Search vault content by keywords."""
    from doctoragent.api.schemas import SearchQuery

    async def _search():
        return await agent.search(SearchQuery(query=query, top_k=top_k, semantic=semantic))

    results = asyncio.run(_search())
    if not results:
        click.echo(f"No results found for: {query}")
        return 0
    for i, r in enumerate(results, 1):
        click.echo(f"{i}. {r.vault_path}")
        click.echo(f"   Category: {r.category}")
        click.echo(f"   Summary: {r.summary}")
        click.echo(f"   Score:   {r.score:.4f}")
        click.echo()
    return 0


def cmd_status(agent, config):
    """Show agent status."""
    inbox_count = _count_files(config.paths.inbox)
    vault_count = _count_vault_files(config.paths.vault)
    recent = agent.task_store.list_recent(limit=5)
    click.echo("=== DoctorAgent Status ===")
    click.echo(f"  Inbox files : {inbox_count}")
    click.echo(f"  Vault files : {vault_count}")
    click.echo()
    if recent:
        click.echo("Recent tasks:")
        for s in recent:
            src = str(s.source_path) if s.source_path else "N/A"
            click.echo(f"  {s.task_id}  [{s.state}]  {src}")
            if s.message:
                click.echo(f"    {s.message}")
    else:
        click.echo("No recent tasks.")
    return 0


def cmd_list(agent, category=None):
    """List vault files."""
    items = agent.task_store.list_vault_files(category)
    if not items:
        msg = f" in category '{category}'" if category else ""
        click.echo(f"No vault files found{msg}.")
        return 0
    msg = f" in category '{category}'" if category else ""
    click.echo(f"Vault files{msg} ({len(items)} total):")
    click.echo()
    for i, item in enumerate(items, 1):
        click.echo(f"{i}. {item['vault_path']}")
        click.echo(f"   Category: {item['category']}")
        if item["summary"]:
            click.echo(f"   Summary: {item['summary']}")
        if item["tags"]:
            click.echo(f"   Tags:    {', '.join(item['tags'])}")
        click.echo()
    return 0


def cmd_serve(agent, config, host="127.0.0.1", port=8000, reload=False):
    """Start the API server."""
    from doctoragent.api.server import is_available, run_server

    if not is_available():
        click.echo("FastAPI is required. Install with: pip install doctoragent[server]", err=True)
        return 1
    run_server(config, agent, host=host, port=port, reload=reload)
    return 0


def cmd_import(agent, config, paths, is_dir=False, move=False, no_wait=False):
    """Batch-ingest files."""
    from doctoragent.api.schemas import FileEvent

    targets = _resolve_import_targets(list(paths), is_dir)
    if not targets:
        click.echo("No files to import.", err=True)
        return 1
    click.echo(f"Importing {len(targets)} file(s)...")
    inbox = config.paths.inbox
    if no_wait:
        count = 0
        for src in targets:
            try:
                staged = _stage_into_inbox(src, inbox, move)
                click.echo(f"  Staged: {staged}")
                count += 1
            except OSError as exc:
                click.echo(f"  Failed to stage {src}: {exc}", err=True)
        click.echo(f"\nStaged {count}/{len(targets)} file(s) into {inbox}.")
        return 0 if count == len(targets) else 1
    events = []
    staged_sources = []
    for src in targets:
        try:
            staged = _stage_into_inbox(src, inbox, move)
            events.append(FileEvent(event_id=uuid4(), source_path=staged, event_type="created"))
            staged_sources.append(src)
        except OSError as exc:
            click.echo(f"  Failed to stage {src}: {exc}", err=True)
    if not events:
        click.echo("No files staged successfully.", err=True)
        return 1
    statuses = asyncio.run(_process_import_batch(agent, events))
    succeeded = 0
    failed = 0
    for src, status in zip(staged_sources, statuses, strict=True):
        if status.state == "COMPLETED":
            succeeded += 1
            click.echo(f"  OK:       {src}  ->  {status.task_id}")
        else:
            failed += 1
            msg = f"  ({status.message})" if status.message else ""
            click.echo(f"  {status.state}: {src}{msg}", err=True)
    click.echo(f"\nImported {succeeded}/{len(staged_sources)} staged ({failed} failed).")
    return 0 if failed == 0 else 1


def cmd_run(agent, config, script, dry_run=False):
    """Execute a JSON orchestration script."""
    if str(script) == "-":
        raw = sys.stdin.read()
    else:
        path = Path(script)
        if not path.exists():
            click.echo(f"Script not found: {script}", err=True)
            return 1
        raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        click.echo(f"Invalid script JSON: {exc}", err=True)
        return 1
    if isinstance(parsed, dict) and "steps" in parsed:
        steps = parsed["steps"]
    elif isinstance(parsed, list):
        steps = parsed
    else:
        steps = [parsed]
    if not steps:
        click.echo("Script contains no steps.", err=True)
        return 1
    if dry_run:
        click.echo(f"Script validated: {len(steps)} step(s).")
        for i, step in enumerate(steps, 1):
            click.echo(f"  {i}. {json.dumps(step, default=str)}")
        return 0
    results = []
    for step in steps:
        op = step.get("op", "")
        if op == "status":
            rc = cmd_status(agent, config)
            results.append({"op": "status", "state": "COMPLETED" if rc == 0 else "FAILED"})
        elif op == "list":
            category = step.get("category")
            rc = cmd_list(agent, category=category)
            results.append({"op": "list", "state": "COMPLETED" if rc == 0 else "FAILED"})
        elif op == "search":
            query = step.get("query", "")
            rc = cmd_search(agent, query)
            results.append({"op": "search", "state": "COMPLETED" if rc == 0 else "FAILED"})
        elif op == "import":
            paths = [Path(p) for p in step.get("paths", [])]
            rc = cmd_import(agent, config, paths)
            results.append({"op": "import", "state": "COMPLETED" if rc == 0 else "FAILED"})
        else:
            results.append({"op": op, "state": "FAILED", "message": f"Unknown operation: {op}"})
    click.echo(json.dumps(results, indent=2, default=str))
    return 1 if any(r.get("state") == "FAILED" for r in results) else 0


def cmd_ask(agent, config, question, top_k=5, session_id=None, no_memory=False):
    """RAG Q&A with context engineering and memory.

    Features:
    - Context engineering with token budget management
    - Four-layer memory system (short-term, working, episodic, long-term)
    - Query expansion for better recall
    - Cross-encoder re-ranking for precision
    - Conversation history management
    """
    if not question:
        click.echo("Please provide a question.", err=True)
        return 1

    # Get embedding provider from agent
    embedding_provider = getattr(agent, "_embedding_provider", None)

    # Get LLM provider from classifier
    llm_provider = None
    if hasattr(agent.classifier, "provider"):
        llm_provider = agent.classifier.provider

    # Initialize RAG pipeline
    from doctoragent.model.rag import RagPipeline

    rag = RagPipeline(
        db_path=config.paths.index / "tasks.db",
        embedding_provider=embedding_provider,
        llm_provider=llm_provider,
        tenant_id=agent.task_store._tenant_id,
    )

    # Execute RAG query
    click.echo(f"Searching vault for: {question}")
    click.echo()

    try:
        response = rag.ask(
            question,
            session_id=session_id,
            top_k=top_k,
            use_reranker=True,
            use_query_expansion=True,
            use_memory=not no_memory,
        )
    except Exception as exc:
        click.echo(f"RAG query failed: {exc}", err=True)
        return 1

    # Display answer
    click.echo("=== Answer ===")
    click.echo(response.answer)
    click.echo()

    # Display sources
    if response.sources:
        click.echo("=== Sources ===")
        for source in response.sources:
            chunk = source.chunk if hasattr(source, "chunk") else source
            vault_path = (
                chunk.get("vault_path", "unknown")
                if isinstance(chunk, dict)
                else getattr(chunk, "vault_path", "unknown")
            )
            category = (
                chunk.get("category", "")
                if isinstance(chunk, dict)
                else getattr(chunk, "category", "")
            )
            click.echo(f"[{source.source_label}] {vault_path}")
            click.echo(f"  Category: {category}")
            click.echo(f"  Score: {source.score:.4f}")
            click.echo()

    # Display stats
    click.echo("=== Stats ===")
    click.echo(f"Retrieval method: {response.retrieval_method}")
    click.echo(f"Chunks searched: {response.total_chunks_searched}")
    click.echo(f"Context tokens: {response.context_tokens_used}")
    click.echo(f"Memory used: {response.memory_used}")
    click.echo(f"Conversation turns: {response.conversation_turns}")
    click.echo(f"Model: {response.model_used}")

    if session_id:
        click.echo(f"Session ID: {session_id}")

    return 0


def cmd_agent(agent, config, task, max_iterations=10, verbose=False):
    """Run the intelligent agent with tool calling and reasoning.

    The agent can:
    - Search and analyze documents
    - Extract information
    - Compare documents
    - Remember user preferences
    - Execute multi-step tasks
    """
    if not task:
        click.echo("Please provide a task for the agent.", err=True)
        return 1

    # Get providers
    embedding_provider = getattr(agent, "_embedding_provider", None)
    llm_provider = None
    if hasattr(agent.classifier, "provider"):
        llm_provider = agent.classifier.provider

    if not llm_provider:
        click.echo("No LLM provider available. Please configure a connection.", err=True)
        return 1

    # Initialize systems
    from doctoragent.model.agent import AgentConfig, create_agent
    from doctoragent.model.rag import MemorySystem, RagPipeline

    db_path = config.paths.index / "tasks.db"

    rag = RagPipeline(
        db_path=db_path,
        embedding_provider=embedding_provider,
        llm_provider=llm_provider,
        tenant_id=agent.task_store._tenant_id,
    )

    memory = MemorySystem(db_path, agent.task_store._tenant_id)

    # Create agent with tools
    agent_config = AgentConfig(
        max_iterations=max_iterations,
        enable_planning=True,
        enable_reflection=True,
    )

    smart_agent = create_agent(
        llm_provider=llm_provider,
        rag_pipeline=rag,
        task_store=agent.task_store,
        memory_system=memory,
        config=agent_config,
    )

    click.echo(f"Agent task: {task}")
    click.echo()

    try:
        response = smart_agent.run_sync(task)
    except Exception as exc:
        click.echo(f"Agent failed: {exc}", err=True)
        return 1

    # Display answer
    click.echo("=== Agent Response ===")
    click.echo(response)
    click.echo()

    # Display trajectory if verbose
    if verbose:
        trajectory = smart_agent.get_trajectory()
        click.echo("=== Execution Trajectory ===")
        for i, step in enumerate(trajectory.steps, 1):
            click.echo(f"{i}. [{step.step_type.value}] {step.content[:200]}")
            if step.tool_name:
                click.echo(f"   Tool: {step.tool_name}")
                if step.tool_args:
                    click.echo(f"   Args: {step.tool_args}")
        click.echo()
        click.echo(f"Total tool calls: {trajectory.total_tool_calls}")
        click.echo(f"Total time: {trajectory.total_time_ms:.0f}ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
