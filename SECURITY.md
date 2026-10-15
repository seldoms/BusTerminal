# 安全策略

## 报告安全问题

请**不要**通过公开 issue 报告安全漏洞。请改用 GitHub 的
「Security → Report a vulnerability」（私密漏洞报告）功能，或在仓库的
Security 页面找到联系方式。我们会在确认后尽快回复，并在修复发布前与你同步进度。

请在报告中尽量包含：影响版本、复现步骤、影响范围，以及（如有）可行的修复建议。

## 支持范围

| 版本 | 是否接受安全修复 |
| --- | --- |
| `main` 分支 | 是 |
| 已发布的 `v*` tag | 仅最新一个 tag |
| 更早的 tag | 否 |

## 部署安全边界（请务必阅读）

BusTerminal 是**面向可信内网**的自托管工具，请在部署时保持以下边界：

1. **不要直接暴露到公网。** 请放在反向代理（带认证）之后，或仅通过 VPN / 内网访问。
2. **首次部署保持 `REQUIRE_AUTH=true`**，并在 `/setup` 向导里建好管理员账号。
   `REQUIRE_AUTH=false` 等于无鉴权，任何人都能读取与删除你的数据。
3. **不要把 `.env`、含真实口令的 compose 文件、API Key 提交到任何仓库。**
   仓库自带的 `.env.example` / `.env.install.example` 只含占位符。
4. **维护类开关默认关闭**（`BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION`、
   `BUS_TERMINAL_ALLOW_DESTRUCTIVE_MAINTENANCE`、`BUS_TERMINAL_ALLOW_CACHE_MAINTENANCE`）。
   只在备份后、明确的维护窗口内临时打开。
5. **删除类操作有保护闸**：Jellyfin 删除需要确认码，批量操作有数量上限，
   自动清理候选默认不会真正删除任何 Jellyfin 条目或本地文件。
6. **对外部服务写入凭据一律不回显**：后台设置页的密码/令牌字段只写不读，
   留空表示保持原值。

## 已知的非问题

- 服务在未完成 `/setup` 向导前会拦截全站，这是**预期行为**，不是漏洞。
- `/api/health/live` 无需鉴权且只返回进程存活状态，**不构成信息泄露**；
  需要业务状态请用 `/api/health/check`（受鉴权保护）。
