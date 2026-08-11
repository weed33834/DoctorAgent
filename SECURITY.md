# 安全政策（Security Policy）

DoctorAgent 处理**临床决策支持**与潜在 PHI（受保护健康信息）。请把安全问题放在首位。

## 支持的版本
| 版本 | 支持状态 |
|------|---------|
| 最新 release | ✅ 受支持 |
| 旧版本 | ❌ 请升级到最新版 |

## 报告漏洞（Responsible Disclosure）
请**不要**在公开 issue 中披露漏洞。通过以下任一方式报告：

1. 在 GitCode / GitHub 仓库创建**私密** Security advisory（如平台支持）
2. 或在 issue 中联系维护者（`pyproject.toml` 中的维护者邮箱）

请附上：
- 受影响的版本 / commit
- 复现步骤（最小示例）
- 潜在影响与建议修复

## 处理时限
- 确认：48 小时内
- 高危修复：7 天内出补丁
- 披露：修复发布后再公开

## 安全特性
本项目的安全基线（供评估参考，**不作为认证声明**）：
- PHI 脱敏（HIPAA Safe Harbor 19 类标识符）、AES-256-GCM 字段加密、HMAC-SHA256 链式审计
- RBAC + OIDC SSO + TOTP MFA、多租户隔离
- 代码执行沙箱（fail-closed：无有效 OS 隔离则拒绝运行）
- LLM 输出护栏（注入检测/禁止内容/PHI 泄露）、安全红队演练
- 供应链：依赖审计 + dependabot
