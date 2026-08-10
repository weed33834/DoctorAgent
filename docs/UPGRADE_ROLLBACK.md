# DoctorAgent 升级与回滚方案 (Upgrade & Rollback Plan)

> 适用对象：DoctorAgent 运维、SRE、变更管理
> 版本：v1.0
> 配套文档：`docs/OPERATIONS.md`、`docs/DISASTER_RECOVERY.md`

---

## 目录

1. [版本策略](#1-版本策略)
2. [升级前检查清单](#2-升级前检查清单)
3. [升级路径](#3-升级路径)
4. [回滚流程](#4-回滚流程)
5. [灰度发布策略](#5-灰度发布策略)
6. [升级失败处理决策树](#6-升级失败处理决策树)
7. [验证清单](#7-验证清单)

---

## 1. 版本策略

### 1.1 语义化版本 (SemVer)

DoctorAgent 采用 `MAJOR.MINOR.PATCH` 语义化版本：

| 版本号 | 触发条件 | 兼容性 |
|--------|----------|--------|
| `MAJOR` (x.0.0) | 不兼容的 API/数据格式变更 | **需迁移**，必须回滚预案 |
| `MINOR` (1.x.0) | 向下兼容的功能新增 | 向下兼容，可平滑升级 |
| `PATCH` (1.0.x) | Bug 修复 | 向下兼容，零风险升级 |

### 1.2 LTS（长期支持）版本

- 每 12 个月发布一个 LTS 分支，提供 24 个月安全补丁
- 临床生产环境**建议长期停留在 LTS**，仅在补丁窗口内升级
- LTS 间的 MINOR 升级需通过变更评审

### 1.3 Breaking Change 政策

- Breaking change 只在 MAJOR 版本引入
- 至少提前 1 个 MINOR 版本发出 deprecation 警告
- 提供 `doctoragent migrate` 迁移工具与回滚脚本
- 数据格式变更必须可双向转换（升级 + 回滚）

### 1.4 版本一致性约束

三处版本号必须严格一致，否则发布失败：

| 位置 | 字段 |
|------|------|
| `pyproject.toml` | `version` |
| `doctoragent/__init__.py` | `__version__` |
| `Dockerfile` | `DOCTORAGENT_VERSION` |

统一校验：

```bash
make check-version
```

---

## 2. 升级前检查清单

### 2.1 变更窗口

- [ ] 选择临床低峰期（建议 02:00–05:00）
- [ ] 提前 3 个工作日通知临床用户
- [ ] 变更评审会已通过（涉及 MAJOR/MINOR 必须评审）
- [ ] 应急联系人在线（参见 `DISASTER_RECOVERY.md` §6）

### 2.2 版本与兼容性检查

```bash
# 1. 校验当前版本一致性
make check-version

# 2. 查看目标版本 release notes（CHANGELOG.md）
git log --oneline v$(doctoragent --version)..HEAD

# 3. 检查 schema 兼容性（dry-run 迁移）
doctoragent migrate --dry-run
```

- [ ] `make check-version` 通过
- [ ] CHANGELOG 已审阅，无未评估的 breaking change
- [ ] `migrate --dry-run` 无报错

### 2.3 备份

```bash
# 升级前必须完整备份（vault + index + config + audit 原子打包）
doctoragent backup
doctoragent backup verify --latest
```

- [ ] 升级前备份已完成并校验
- [ ] 备份 ID 已记录：`<backup-id>`
- [ ] 主密钥 Shamir 分片持有人可联系（回滚可能需密钥）

### 2.4 回滚预案就绪

- [ ] 旧版本包/镜像已缓存（不依赖 PyPI/Docker Hub 实时可用）
- [ ] 回滚步骤已打印并在现场备查
- [ ] 回滚验证脚本已准备

---

## 3. 升级路径

### 3.1 PyPI 升级（裸机部署）

```bash
# 1. 停止服务
doctoragent daemon stop
doctoragent serve stop

# 2. 升级包
pip install --upgrade doctoragent

# 3. 校验版本
doctoragent --version
make check-version

# 4. 执行数据库迁移（如需）
doctoragent migrate

# 5. 启动服务
doctoragent serve &
doctoragent daemon &

# 6. 验证（见 §7）
curl -s http://localhost:8000/health
```

### 3.2 Docker 升级（容器化部署）

```bash
# 1. 拉取新镜像
docker compose pull

# 2. 校验镜像版本
docker compose images | grep doctoragent

# 3. 滚动重启
docker compose up -d

# 4. 查看启动日志
docker compose logs -f --tail=100 doctoragent

# 5. 验证
curl -s http://localhost:8000/health
```

> 若使用 `--profile with-llm` 或 `--profile with-fhir`，升级命令需保留对应 profile 参数。

### 3.3 数据库迁移（SQLite schema 兼容性）

DoctorAgent 使用 SQLite + FTS5。迁移注意事项：

```bash
# 1. 迁移前必须备份（已在 §2.3 完成）
# 2. dry-run 预检
doctoragent migrate --dry-run

# 3. 执行迁移
doctoragent migrate

# 4. 校验 schema 版本
doctoragent db schema-version

# 5. 校验 FTS5 索引完整
doctoragent index verify
```

| 迁移类型 | 风险 | 处理 |
|----------|------|------|
| 新增列 | 低 | 自动迁移 |
| 新增表 | 低 | 自动迁移 |
| 列类型变更 | **高** | 提供双向转换脚本 |
| FTS5 schema 变更 | 中 | 需 `index rebuild` |
| 加密格式变更 | **高** | 全量重加密，需密钥轮换配合 |

- [ ] 迁移前备份已校验
- [ ] `migrate --dry-run` 通过
- [ ] 迁移后 schema 版本符合预期
- [ ] FTS5 索引校验通过

---

## 4. 回滚流程

### 4.1 回滚决策

回滚触发条件（满足任一即回滚）：

- `/health` 持续失败 > 5 min
- 审计日志 HMAC 校验失败
- Vault 写入异常率 > 1%
- 关键临床查询错误率 > 5%
- 数据迁移不可逆损坏

### 4.2 PyPI 回滚

```bash
# 1. 停止服务
doctoragent daemon stop
doctoragent serve stop

# 2. 安装旧版本
pip install doctoragent==<旧版本>

# 3. 校验版本
doctoragent --version
make check-version

# 4. 回滚数据库迁移（如升级时迁移了 schema）
doctoragent migrate --rollback --to-version <旧 schema 版本>

# 5. 启动服务
doctoragent serve &
doctoragent daemon &

# 6. 验证（见 §7）
curl -s http://localhost:8000/health
```

### 4.3 Docker 回滚

```bash
# 1. 修改 docker-compose.yml 中 image tag 为旧版本
#    image: ghcr.io/<org>/doctoragent:<旧版本>

# 2. 重新部署
docker compose up -d

# 3. 校验镜像版本
docker compose images | grep doctoragent

# 4. 验证
curl -s http://localhost:8000/health
```

### 4.4 数据回滚

当升级导致数据损坏时，从备份恢复：

```bash
# 1. 停止服务
doctoragent daemon stop
doctoragent serve stop

# 2. 从升级前备份恢复 vault/index/audit
doctoragent restore full --backup <backup-id> --target ~/DoctorAgent

# 3. 校验
doctoragent vault verify --all --sample 100
doctoragent audit verify --all
doctoragent index verify

# 4. 启动服务
doctoragent serve &
doctoragent daemon &
```

> 数据回滚会丢失升级后写入的数据。若升级后已有临床写入，需先与临床负责人评估数据丢失影响，参见 `DISASTER_RECOVERY.md` §4。

### 4.5 密钥回滚（撤销紧急轮换）

若升级过程中执行了 master key emergency rotation 需撤销：

```bash
# 1. 召集 ≥3 名 Shamir 持有人
# 2. 使用升级前归档的旧密钥分片重组
doctoragent key restore --shamir-threshold 3 \
  --from-archive <pre-upgrade-key-archive>

# 3. 校验 vault key 解锁
doctoragent key unlock
doctoragent vault verify --all --sample 100

# 4. 作废紧急轮换生成的新分片
doctoragent key shamir revoke --batch <emergency-batch-id>

# 5. 审计日志记录 key.rollback 事件
```

- [ ] 旧密钥分片已从离线归档取回
- [ ] ≥3 持有人到场核验身份
- [ ] Vault 解密校验通过
- [ ] 紧急分片已作废
- [ ] 审计日志记录回滚事件并上报合规

---

## 5. 灰度发布策略

### 5.1 Canary（金丝雀）

适用于 PATCH 与低风险 MINOR 升级。

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Stage 1     │ ──> │  Stage 2     │ ──> │  Stage 3     │
│  5% 流量      │     │  25% 流量     │     │  100% 流量    │
│  观察 1h      │     │  观察 4h      │     │  全量         │
└──────────────┘     └──────────────┘     └──────────────┘
```

- 每个 stage 必须通过 §7 验证清单
- 任一 stage 触发回滚条件 → 立即回滚至旧版本
- Canary 实例与稳定实例并行运行，按 header/用户分流量

### 5.2 蓝绿 (Blue-Green)

适用于 MAJOR 升级与高风险变更。

```
        ┌─────────────────┐
流量 ──> │  Blue (当前版本) │  ← 生产
        └─────────────────┘
        ┌─────────────────┐
        │  Green (新版本)  │  ← 预热 + 全量验证
        └─────────────────┘

验证通过后切换 LB → Green 成为生产，Blue 保留 24h 作为快速回滚
```

- Green 环境必须使用 Blue 的**只读副本**进行验证，不可双写
- 切换前 Green 必须完成 §7 全部验证
- Blue 保留至少 24h，确认稳定后下线

### 5.3 滚动 (Rolling)

适用于 HA 多实例部署的 PATCH 升级。

- 每次替换 1 个实例，等待健康检查通过后再替换下一个
- 滚动期间保持 N-1 实例可用
- 任一实例健康检查失败 → 暂停滚动，评估回滚

---

## 6. 升级失败处理决策树

```
升级后启动服务
       │
       ▼
 /health 是否 200?
       │
 ┌─────┴─────┐
 是          否
 │           │
 │           ▼
 │     日志是否有明确错误?
 │           │
 │      ┌────┴────┐
 │      是         否
 │      │          │
 │      ▼          ▼
 │  可快速修复?   联系厂商支持
 │      │          │
 │   ┌──┴──┐       │
 │   是     否      │
 │   │      │      │
 │   ▼      ▼      │
 │ 修复    回滚 ◄───┘
 │ 重启    (§4.2/4.3)
 │   │
 ▼   │
 验证通过? (§7)
 │   │
 ┌─┴─┐ │
 是   否 │
 │    └──> 回滚 (§4.2/4.3)
 ▼
 升级成功 ✅


回滚后验证:
   │
   ▼
 /health 200?
   │
 ┌─┴─┐
 是   否
 │    │
 ▼    ▼
完成  数据回滚 (§4.4)
      + 密钥回滚 (§4.5)
      + 紧急上报
```

**决策原则**：

1. **安全优先**：任何 PHI 风险或审计异常立即回滚，不尝试在线修复。
2. **时间盒**：从故障发生起 30 min 内无法恢复 → 启动回滚。
3. **不可逆操作**：迁移类操作前必须确认备份可回滚。
4. **升级通告**：P0/P1 故障必须按 `DISASTER_RECOVERY.md` §6 升级路径上报。

---

## 7. 验证清单

升级（或回滚）完成后，**必须全部通过**以下 5 项验证方可宣布完成：

### 7.1 健康检查 (Health)

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health
# 期望: 200
```

- [ ] `/health` 返回 200
- [ ] 连续 5 min 探测无失败

### 7.2 审计日志 (Audit)

```bash
# 1. 触发一次操作产生审计事件
doctoragent audit verify --since "10 minutes ago"
# 2. 确认新事件已写入且 HMAC 链完整
```

- [ ] 审计日志有新条目
- [ ] HMAC 校验通过
- [ ] 时间戳与 NTP 一致

### 7.3 抽样查询 (Sample Query)

```bash
# 使用已知病历执行一次完整查询
doctoragent query --sample "critical-labs"
# 验证返回结构、引用来源、危急值告警
```

- [ ] 查询返回成功
- [ ] 结果结构与升级前一致
- [ ] 引用来源可追溯

### 7.4 加密验证 (Encryption)

```bash
# 抽样解密 10 份病历
doctoragent vault verify --sample 10
# 校验 AES-256-GCM 完整性标签
```

- [ ] 10 份抽样全部解密成功
- [ ] GCM 认证标签校验通过
- [ ] 三层密钥层级正常工作

### 7.5 RBAC 权限 (RBAC)

```bash
# 用不同角色 token 测试访问控制
doctoragent rbac test --role clinician
doctoragent rbac test --role admin
doctoragent rbac test --role auditor
```

- [ ] 临床角色可查询，不可管理密钥
- [ ] 管理员可管理，审计只读
- [ ] 无权限操作被拒绝并记录审计

### 7.6 验证完成确认

- [ ] 以上 5 项全部通过
- [ ] 变更负责人签字确认
- [ ] 通知临床用户升级完成
- [ ] 变更记录归档（保留 ≥ 3 年，合规要求）
- [ ] 24h 后复查监控指标无异常

---

## 附录：相关文档

- 运维手册：`docs/OPERATIONS.md`
- 灾难恢复方案：`docs/DISASTER_RECOVERY.md`
- 变更日志：`CHANGELOG.md`
- 发布流程：`RELEASE.md`
