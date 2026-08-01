# 发布指南

本文件说明如何将 DoctorAgent 发布到三大平台：**PyPI**、**Docker (GHCR)**、**GitHub Releases**。

---

## 前置准备

### 1. 版本号同步

发布前确保以下文件的版本号一致（以 `<VERSION>` 代指本次发布的版本号，例如 `0.3.1`）：

| 文件 | 字段 | 示例 |
|------|------|------|
| `pyproject.toml` | `version` | `<VERSION>` |
| `doctoragent/__init__.py` | `__version__` | `<VERSION>` |
| `Dockerfile` | `ARG DOCTORAGENT_VERSION` | `<VERSION>` |
| `CHANGELOG.md` | `## [x.y.z]` | `## [<VERSION>]` |

统一校验（`Makefile` 的 `check-version` 目标比对此三处的版本号是否一致）：

```bash
make check-version
```

> `check-version` 同时比对 `pyproject.toml` / `__init__.py` / `Dockerfile` 三处版本号；`CHANGELOG.md` 的版本条目需在发布前手动确认与之一致。

### 2. PyPI Trusted Publisher 配置（首次发布）

使用 OIDC Trusted Publishing，无需管理 API token：

1. 登录 [PyPI](https://pypi.org/account/register/)
2. 进入 Account settings → Publishing → Add a new publisher
3. 选择 GitHub，填入：
   - Repository owner: `doctoragent`
   - Repository name: `doctoragent`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
4. 保存

> 如果已配置 Trusted Publisher，`release.yml` 中的 `pypa/gh-action-pypi-publish` 会自动使用 OIDC 认证。
> 否则取消注释 `password: ${{ secrets.PYPI_API_TOKEN }}` 并在 GitHub Secrets 中添加 `PYPI_API_TOKEN`。

### 3. GHCR 权限

GitHub Container Registry 使用内置的 `GITHUB_TOKEN`，无需额外配置。
首次推送镜像后，在 GitHub → Packages → doctoragent → Settings 中设置可见性（推荐 Public）。

---

## 发布流程

### 方式一：Tag 触发（推荐）

```bash
# 1. 确保所有改动已提交并推送到 main
git checkout main
git pull origin main

# 2. 更新版本号（以 0.3.1 为例）
#    编辑 pyproject.toml, doctoragent/__init__.py, Dockerfile, CHANGELOG.md

# 3. 提交版本变更
git add -A
git commit -m "chore: bump version to 0.3.1"

# 4. 创建并推送 tag
git tag v0.3.1
git push origin v0.3.1

# 5. GitHub Actions 自动触发 release.yml 工作流
#    查看: https://github.com/weed33834/DoctorAgent/actions
```

推送 tag 后，`release.yml` 会依次执行：

1. **📦 PyPI** — 构建 wheel + sdist → 发布到 PyPI
2. **🐳 Docker GHCR** — 构建 linux/amd64 + linux/arm64 镜像 → 推送到 `ghcr.io/weed33834/doctoragent`（镜像名跟随发布仓库 `${{ github.repository }}` 小写）
3. **🚀 GitHub Release** — 从 CHANGELOG.md 提取发布说明 → 创建 GitHub Release

### 方式二：手动触发

在 GitHub Actions 页面选择 "Release" 工作流 → Run workflow → 输入版本号。

---

## 发布后验证

### PyPI

```bash
pip install doctoragent==<VERSION>
python -c "import doctoragent; print(doctoragent.__version__)"
```

访问 https://pypi.org/project/doctoragent/ 确认页面已更新。

### Docker

```bash
docker pull ghcr.io/weed33834/doctoragent:<VERSION>
docker run --rm ghcr.io/weed33834/doctoragent:<VERSION> --version
docker inspect ghcr.io/weed33834/doctoragent:<VERSION> --format '{{.Config.Labels}}'
```

### GitHub Release

访问 https://github.com/weed33834/DoctorAgent/releases 确认 Release 已创建。

---

## Docker 镜像标签说明

每次发布会生成以下标签：

| 标签 | 说明 | 示例 |
|------|------|------|
| `latest` | 最新稳定版 | `ghcr.io/weed33834/doctoragent:latest` |
| `x.y.z` | 精确版本 | `ghcr.io/weed33834/doctoragent:0.3.1` |
| `x.y` | 次版本（最新补丁） | `ghcr.io/weed33834/doctoragent:0.3` |
| `sha-xxxxxxx` | Git commit SHA | `ghcr.io/weed33834/doctoragent:sha-a1b2c3d` |

---

## 回滚

如果发布出现问题：

```bash
# 1. 从 PyPI 撤回（不可删除，只能 yank）
#    https://pypi.org/manage/project/doctoragent/releases/

# 2. 从 GHCR 删除镜像版本
#    GitHub → Packages → doctoragent → Package settings → Manage versions

# 3. 删除 GitHub Release
#    GitHub → Releases → 对应版本 → Delete

# 4. 删除 tag（谨慎操作）
git tag -d v<VERSION>
git push origin :refs/tags/v<VERSION>
```

---

## CI/CD 工作流一览

| 工作流 | 文件 | 触发条件 | 功能 |
|--------|------|----------|------|
| CI | `.github/workflows/ci.yml` | push/PR to main | Lint (ruff) + 测试 (pytest) + 覆盖率 |
| Security | `.github/workflows/security.yml` | push/PR + 每周一 | bandit + pip-audit |
| Release | `.github/workflows/release.yml` | push tag `v*.*.*` | PyPI + Docker GHCR + GitHub Release |
