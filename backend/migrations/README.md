# 数据库迁移说明

运行时迁移由 `backend/database.py` 中的版本化迁移账本管理，当前版本会记录在 `schema_migrations` 表中。`001_*.sql` 和 `002_*.sql` 是历史 PostgreSQL 参考脚本，不再作为当前应用的迁移入口。

新数据库会直接建立当前结构并登记全部版本。旧数据库默认不会执行 `ALTER TABLE`、队列去重或演员别名回填；`GET /api/health/check` 会通过 `database.schema` 返回当前版本和 pending 列表。

当前 v4 迁移为 `download_queue` 增加 `next_retry_at`，并重建活动队列唯一索引，使 `waiting_external_service` 状态继续参与影片和磁链去重。该迁移不会自动连接或启动 qBittorrent，也不会提交真实下载。

v5 新增持久化 `external_tasks` 队列，用于保存 Jellyfin 等外部服务任务的幂等键、重试次数、错误摘要、下次重试时间和恢复结果。单任务默认最多自动重试 20 次，耗尽后保留为 `needs_attention`，不会丢弃或阻塞其他任务。

v6 为演员元数据补全增加首次错误、最近错误和完成时间字段。该任务继续复用 `actresses` 表中已有的请求时间与尝试次数，不重复写入通用外部任务队列。

v7 为影片生命周期补齐 `discovery_status`、`decision_status`、`organize_status` 三个独立维度，并为事件增加旧状态、新状态、执行结果和公开错误摘要。原有下载、Jellyfin、清理字段与 API 参数保持兼容。
v8 为清理候选增加决策人、决策原因和决策时间。批准仅表示候选已审核，不会触发文件或 Jellyfin 删除；真实删除仍必须经过独立保护接口。

v9 为 `download_queue` 增加 `save_path` 和 `category`。手工磁链与自动下载统一先写入持久队列；qBittorrent 离线时任务按退避策略继续重试，恢复后使用原始保存目录和分类提交。

旧数据库迁移属于高风险操作。迁移前必须停止写入、完成备份并明确确认，然后在维护窗口临时设置：

```powershell
$env:BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION="true"
python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

确认健康检查中的 `migration_required` 为 `false` 后，应移除该环境变量并正常重启。不要长期启用迁移开关，也不要同时运行旧 SQL 脚本和版本化迁移。
