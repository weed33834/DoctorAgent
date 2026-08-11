#!/usr/bin/env python3
"""Boot the REAL DoctorAgent server on :3000 with a live LLM connection
(api.hcnsec.cn) so the console chat streams real model output.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

KEY = os.environ.get("K2", "<REDACTED_API_KEY>")
DATA = pathlib.Path(tempfile.mkdtemp(prefix="doctoragent-live-"))

# master key password so AegisAgent can build its key provider
os.environ.setdefault("DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD", "live-demo-pass-2026")
os.environ.setdefault("DOCTORAGENT_API_TOKEN", "demo-token")

from doctoragent.config import AegisConfig  # noqa: E402

cfg = AegisConfig()
cfg.paths.inbox = DATA / "Inbox"
cfg.paths.vault = DATA / "Vault"
cfg.paths.index = DATA / "Index"
cfg.paths.logs = DATA / "Logs"
cfg.paths.connections = DATA / "Config" / "connections.json"
for p in (cfg.paths.inbox, cfg.paths.vault, cfg.paths.index, cfg.paths.logs, cfg.paths.connections.parent):
    p.mkdir(parents=True, exist_ok=True)

from doctoragent.connections.manager import ConnectionManager  # noqa: E402
from doctoragent.connections.models import AuthMethod, Connection, PlatformType  # noqa: E402

cm = ConnectionManager(cfg.paths.connections)
# 加入 hcnsec 连接，并只保留它以确保被选为默认
conn = Connection(
    name="hcnsec", platform_type=PlatformType.OPENAI_COMPATIBLE,
    base_url="https://api.hcnsec.cn", model_name="step-3.5-flash",
    api_key=KEY, auth_method=AuthMethod.API_KEY,
    is_local=True, is_cloud_authorized=True, timeout=60.0, priority=0,
)
cm.add(conn)
for c in cm.list_all():
    if c.id != conn.id:
        try:
            cm.delete(c.id)
        except Exception:  # noqa: BLE001
            pass
print("connections:", [c.name for c in cm.list_all()], flush=True)

from doctoragent.orchestration.agent import AegisAgent  # noqa: E402

agent = AegisAgent(config=cfg, connection_manager=cm)
print("agent built; provider model:", getattr(agent.classifier.provider, "connection", None).model_name, flush=True)

from doctoragent.api.server import create_app  # noqa: E402

app = create_app(cfg, agent)
print("app ready on :3000", flush=True)

import uvicorn  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=3000, log_level="warning")
