# DoctorAgent 运维手册 (Operations Manual)

> 适用对象：DoctorAgent 运维工程师、SRE、IT 管理员
> 版本：v1.0

---

## 目录

1. [部署架构概览](#1-部署架构概览)
2. [首次部署清单](#2-首次部署清单)
3. [日常运维任务](#3-日常运维任务)
4. [监控指标](#4-监控指标)
5. [告警阈值与响应](#5-告警阈值与响应)
6. [安全运维](#6-安全运维)
7. [性能调优](#7-性能调优)
8. [常见问题排查](#8-常见问题排查)

---

## 1. 部署架构概览

DoctorAgent 是合规优先、本地化部署、可审计的临床 AI 智能体。两种典型部署形态如下。

### 1.1 单节点部署 (Single-Node)

适用于中小型医院、单科室试点、POC 环境。

```
┌─────────────────────────────────────────────┐
│              单节点主机                       │
│  ┌────────────────────────────────────────┐ │
│  │ doctoragent serve  (FastAPI :8000)     │ │
│  │ doctoragent daemon (Inbox watcher)     │ │
│  └────────────────────────────────────────┘ │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐ │
│  │ Vault(AES) │ │ SQLite+FTS5│ │ Audit Log│ │
│  └────────────┘ └────────────┘ └──────────┘ │
│  ┌────────────────────────────────────────┐ │
│  │ Ollama (可选, --profile with-llm)       │ │
│  │ HAPI FHIR (可选, --profile with-fhir)   │ │
│  └────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

### 1.2 高可用部署 (HA)

适用于三甲医院、多院区、生产关键路径。

| 组件 | HA 形态 | 说明 |
|------|---------|------|
| API 服务 (`serve`) | 多实例 + 反向代理负载均衡 | 无状态，可水平扩展 |
| Inbox 守护进程 (`daemon`) | 单活（leader election）或分片 | 避免重复处理 |
| Vault 存储 | 共享加密卷 / NAS（NFS over TLS） | 文件级锁 |
| SQLite + FTS5 | 主从（Litestream / WAL 流式复制） | 或迁移至 PostgreSQL |
| 审计日志 | 追加至共享只追加卷 + 异步归档 | HMAC 校验链 |
| LLM (Ollama) | 多副本 + 上游网关熔断 | 详见 §5 熔断策略 |

### 1.3 核心组件

- **API 服务**：FastAPI，端口 `8000`，提供 REST 接口与控制台。
- **守护进程**：`doctoragent daemon` 监听 `Inbox/`，自动触发加密入库。
- **加密金库**：AES-256-GCM，三层密钥层级（master key → vault key → file key）。
- **存储索引**：SQLite + FTS5 全文索引。
- **审计日志**：追加式 NDJSON，每条记录 HMAC-SHA256，可篡改检测。
- **可选 LLM**：Ollama（`--profile with-llm`），失败可降级为规则引擎。
- **可选 FHIR**：HAPI FHIR（`--profile with-fhir`）。

---

## 2. 首次部署清单

### 2.1 环境准备

- [ ] 操作系统：Linux x86_64（推荐 Ubuntu 22.04 / RHEL 9）或 Windows Server 2019+
- [ ] Python 3.10+（裸机部署时）
- [ ] Docker 24+ 与 Docker Compose v2（容器化部署时）
- [ ] 磁盘空间：≥ 200 GB（Vault）+ 50 GB（Index/Audit/Log）
- [ ] 内存：≥ 16 GB（启用 Ollama 时 ≥ 32 GB）
- [ ] CPU：≥ 4 核（LLM 推理建议 ≥ 8 核 + GPU）
- [ ] 网络出口：仅允许访问院内 FHIR / 知识源白名单
- [ ] NTP 时钟同步（审计日志时间戳准确性必需）

### 2.2 依赖安装

容器化部署（推荐）：

```bash
git clone <repo-url> Doctoragent
cd Doctoragent
cp .env.example .env
# 编辑 .env 设置密钥与端点
docker compose --profile with-llm up -d
```

裸机部署：

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install doctoragent
```

### 2.3 配置

配置文件路径：`~/DoctorAgent/Config/settings.json`
数据目录：`~/DoctorAgent/{Inbox,Vault,Index,Config}`

关键环境变量：

| 环境变量 | 用途 | 示例 |
|----------|------|------|
| `DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD` | 主密钥口令 | （强随机串） |
| `DOCTORAGENT_MODEL__BASE_URL` | LLM 端点 | `http://ollama:11434/v1` |
| `DOCTORAGENT_API_TOKEN` | API 访问令牌 | （强随机串） |

主密钥提供方（`master_key_provider`）：`filepassword` / `dpapi`（Windows）/ `tpm` / `mac-keychain`。

### 2.4 启动与验证

```bash
# 启动 API 服务
doctoragent serve
# 启动 Inbox 守护进程
doctoragent daemon
```

验证：

```bash
curl -s http://localhost:8000/health
# 期望返回 HTTP 200
```

- [ ] `/health` 返回 200
- [ ] 控制台可访问（`http://<host>:8000/`）
- [ ] 审计日志目录有新条目写入
- [ ] `doctoragent backup --dry-run` 成功

---

## 3. 日常运维任务

### 3.1 备份

> **关键约束**：`vault/` + `index/` + `config/` 必须**一起**备份，否则恢复后索引与密文不一致。

```bash
# 触发一次远程备份
doctoragent backup
```

- 频率：每日 1 次（临床数据 RPO=24h，Vault RPO=1h，详见 `DISASTER_RECOVERY.md`）
- 验证：备份完成后检查 HMAC 校验和与脱密恢复测试

### 3.2 监控

详见 [§4 监控指标](#4-监控指标) 与 [§5 告警阈值与响应](#5-告警阈值与响应)。

### 3.3 日志轮转

```bash
# logrotate 示例 /etc/logrotate.d/doctoragent
/var/log/doctoragent/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

审计日志（NDJSON）为**追加式只追加**，不可轮转截断；采用按月归档 + 异步上传冷存储。

### 3.4 密钥轮换

主密钥轮换周期 90 天，详见 [§6 安全运维](#6-安全运维)。

### 3.5 容量规划

| 资源 | 增长预估 | 告警阈值 |
|------|----------|----------|
| Vault | 每日 ~500 MB（按 1000 份病历） | 剩余 < 20% |
| Index (SQLite) | 每日 ~50 MB | 单库 > 50 GB 需分片 |
| Audit Log | 每日 ~20 MB | 剩余 < 20% |
| Ollama 模型 | 每模型 4–40 GB | 磁盘 < 15% |

---

## 4. 监控指标

### 4.1 健康检查端点

```bash
GET /health  # HTTP 200 = 健康
```

建议每 30 秒探测一次，连续 3 次失败触发告警。

### 4.2 核心指标清单

| 指标 | 采集方式 | 含义 |
|------|----------|------|
| `health_status` | `/health` 探测 | 服务存活 |
| `audit_log_events_total` | 解析审计 NDJSON | 审计事件速率（异常突增/突降） |
| `vault_write_errors_total` | 应用日志 | Vault 写入失败次数 |
| `vault_size_bytes` | 文件系统 | Vault 目录占用 |
| `llm_request_latency_p95` | 应用 metrics | LLM 推理延迟 |
| `llm_circuit_breaker_open` | 应用 metrics | 熔断器状态（0/1） |
| `llm_degraded_mode` | 应用 metrics | 是否降级为规则引擎 |
| `inbox_backlog` | 应用 metrics | Inbox 待处理积压 |
| `fts_index_lag_seconds` | 应用 metrics | 索引落后于 Vault 的秒数 |

### 4.3 审计日志监控

审计日志为篡改可检测（HMAC-SHA256 链）。建议每 5 分钟运行一次完整性校验：

```bash
doctoragent audit verify --since "5 minutes ago"
```

校验失败必须立即告警。

---

## 5. 告警阈值与响应

### 5.1 告警矩阵

| 告警 | 阈值 | 严重度 | 响应动作 |
|------|------|--------|----------|
| `/health` 失败 | 连续 3 次 5xx | P1 | 重启服务 → 查日志 → 通知 oncall |
| LLM 超时 | p95 > 30s 持续 5 min | P2 | 检查 Ollama → 必要时切降级模式 |
| 熔断器开启 | `breaker_open=1` 持续 > 1 min | P2 | 自动降级规则引擎已生效，确认日志 |
| Vault 写入失败 | `vault_write_errors > 0` | P1 | 检查磁盘/权限/密钥 → 紧急轮换评估 |
| 审计日志异常 | HMAC 校验失败 / 写入中断 | P0 | 立即停服 → 安全应急 → 合规上报 |
| Inbox 积压 | `backlog > 1000` 持续 10 min | P3 | 扩容 daemon 或排查阻塞 |
| 索引延迟 | `index_lag > 60s` | P3 | 重跑索引重建 |
| 磁盘容量 | 剩余 < 20% | P2 | 扩容 / 归档冷数据 |

### 5.2 LLM 降级路径

当 `llm_provider=None` 或熔断器开启时，引擎自动降级为**确定性规则引擎 + 关键词检索**，仍可安全输出（无 LLM 生成内容）。

```bash
# 手动强制降级（紧急）
export DOCTORAGENT_MODEL__BASE_URL=""
# 重启服务使降级生效
```

降级期间应在控制台标注“LLM 不可用 — 规则引擎模式”，并通知临床用户。

---

## 6. 安全运维

### 6.1 主密钥轮换（90 天周期）

主密钥（master key）是三层密钥层级根。轮换流程：

1. **预生成新密钥材料**（Shamir 分片，3/5 阈值）。
2. **执行轮换**：

   ```bash
   doctoragent key rotate-master --provider filepassword
   ```

3. **重加密 vault key**（file key 不变，仅包裹层换）。
4. **验证**：抽样解密 10 份病历 + 审计日志写入一条 `key.rotation` 事件。
5. **归档旧密钥**至离线介质，保留 90 天后销毁。

- [ ] 轮换前完整备份
- [ ] 新密钥分片分发至 5 名持有人
- [ ] 旧密钥 Shamir 分片回收
- [ ] 审计日志记录轮换事件

### 6.2 紧急轮换（密钥疑似泄露）

```bash
doctoragent key rotate-master --emergency
```

- 立即停服 → 轮换 → 全量重加密 → 验证 → 恢复服务
- 通知安全负责人与合规官，按安全事件流程上报

### 6.3 权限审计

RBAC 角色与权限矩阵需按安全策略配置。每季度执行：

```bash
doctoragent rbac audit --since "last quarter"
```

- [ ] 复核所有管理员账号
- [ ] 清理离职/调岗账号
- [ ] 最小权限原则复核

### 6.4 PHI 访问审计

PHI（受保护健康信息）访问必须可审计。每月导出 PHI 访问记录：

```bash
doctoragent audit export --category phi-access --range "$(date -d 'last month' +%Y-%m)" 
```

异常访问模式（非工作时间、批量导出、跨科室）需人工复核。

---

## 7. 性能调优

### 7.1 LLM 并发

```jsonc
// settings.json
{
  "model": {
    "max_concurrency": 4,        // 按 GPU 显存调整
    "request_timeout_s": 30,
    "circuit_breaker": {
      "failure_threshold": 5,
      "recovery_seconds": 60
    }
  }
}
```

### 7.2 RAG 缓存

启用向量检索缓存可降低 LLM 调用 40–60%：

```jsonc
{
  "model": {
    "rag_cache": { "enabled": true, "ttl_seconds": 3600, "max_entries": 10000 }
  }
}
```

### 7.3 SQLite WAL 模式

```bash
sqlite3 ~/DoctorAgent/Index/doctoragent.db "PRAGMA journal_mode=WAL;"
sqlite3 ~/DoctorAgent/Index/doctoragent.db "PRAGMA synchronous=NORMAL;"
sqlite3 ~/DoctorAgent/Index/doctoragent.db "PRAGMA mmap_size=268435456;"
```

### 7.4 FTS5 索引优化

- 定期 `INSERT INTO ... rebuild` 重建碎片化索引
- 大库（> 50 GB）按月份分表
- 中文分词器配置（`tokenize='unicode61'` 或自定义 jieba）

```bash
# 索引重建（低峰期执行）
doctoragent index rebuild
```

---

## 8. 常见问题排查

| 现象 | 可能原因 | 解决方案 |
|------|----------|----------|
| `/health` 返回 5xx | 服务崩溃 / 端口占用 / 密钥加载失败 | `journalctl -u doctoragent` 查日志；检查 `MASTER_KEY_PASSWORD` |
| 启动报“master key unlock failed” | 口令错误 / 提供方不可用 | 核对 env var；Windows 检查 DPAPI/TPM 状态 |
| Vault 写入失败 | 磁盘满 / 权限 / 文件锁 | `df -h`；`chown`；检查 NFS 锁 |
| LLM 超时 | Ollama 过载 / GPU OOM | 降低 `max_concurrency`；重启 Ollama；切降级 |
| 熔断器持续开启 | LLM 端点不可达 | 检查 `MODEL__BASE_URL`；网络；临时降级 |
| 索引查询慢 | FTS5 碎片 / WAL 未 checkpoint | `doctoragent index rebuild`；`PRAGMA wal_checkpoint` |
| 审计 HMAC 校验失败 | 日志被篡改 / 磁盘损坏 | P0 告警 → 安全应急 → 从可信备份恢复 |
| Inbox 积压不消化 | daemon 未运行 / 文件锁死 | `doctoragent daemon status`；清理 `.lock` |
| 备份失败 | 远程不可达 / 凭据过期 | 检查备份目标连通性；轮换备份凭据 |
| 控制台 403 | API token 失效 / RBAC 拒绝 | 核对 `DOCTORAGENT_API_TOKEN`；复核角色 |
| 升级后启动失败 | schema 不兼容 / 版本不一致 | 回滚；运行 `make check-version` |
| Windows 下 DPAPI 报错 | 用户上下文变更 | 用相同 Windows 账户运行；或切 `filepassword` |

### 8.1 日志位置速查

| 日志 | 路径 |
|------|------|
| 应用日志 | `/var/log/doctoragent/app.log`（Linux） |
| 审计日志 | `~/DoctorAgent/Audit/audit.ndjson` |
| Ollama | `~/.ollama/logs/server.log` |
| 系统服务 | `journalctl -u doctoragent` |

### 8.2 获取诊断包

提交工单前请生成诊断包（脱敏）：

```bash
doctoragent support bundle --redact
```

---

## 附录：相关文档

- 灾难恢复方案：`docs/DISASTER_RECOVERY.md`
- 升级与回滚方案：`docs/UPGRADE_ROLLBACK.md`
