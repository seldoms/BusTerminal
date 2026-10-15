# 让 AI Agent 帮你装 BusTerminal

把下面整段提示词交给你的 agent（Claude Code / Cursor / Codex / 任何能操作终端的 agent）。
它会按流程检查环境、生成配置、启动容器、**并真正验证是否可用**。

> 两个用法：
> - **全新安装**：只发「安装模式」那一段。
> - **已经装好了要排查**：只发「排障模式」那一段。
>
> ⚠️ **这份只装 BusTerminal 本体。** 想连 qBittorrent / amane / Jellyfin /
> PostgreSQL 一起**全栈部署**（并且让 agent 先探查本机、把清单交给你选、
> 已有的服务自动跳过、经你确认后再动手），用
> [AGENT_FULLSTACK_INSTALL.md](./AGENT_FULLSTACK_INSTALL.md)。

---

## 安装模式（复制以下全部内容给 agent）

````text
请帮我在本机部署 BusTerminal（一个媒体库自动化控制台：内容发现 → 下载 → 刮削入库 → 媒体库展示）。

项目文件在本仓库根目录，部署要用的是 docker-compose.install.yml 和 .env.install.example。
请先读 docs/INSTALL.md 了解全貌，再按下面步骤执行。

## 严格遵守的红线
1. **不要删除或覆盖任何已有数据。** 数据目录（./data、./pgdata）、Jellyfin 媒体库、
   qB 下载目录一律只读挂载，绝不 rm -rf。如果检测到这些目录已有非空数据，
   **停下来告诉我**，让我确认后再继续，不要自作主张清理。
2. **不要关闭鉴权。** REQUIRE_AUTH 保持 true。设成 false 意味着任何人都能删除数据。
3. **不要跳过验证。** 容器起来不等于能用。必须跑完下面「验证」里的每一条命令，
   并把真实输出贴给我。没验证就宣布成功是不可接受的。
4. **不要硬编造结果。** 如果某项拿不到（比如 qB 版本号），就如实说拿不到，
   不要写成"已连接"。这些数字是要用来做决策的。
5. 改动前先备份你要改的配置文件。

## 步骤 1：环境体检（先看清现状再动手）
依次执行并把结果汇总给我：
- `docker --version && docker compose version`     # 需要 20.10+ 且有 compose 子命令
- `docker ps --format '{{.Names}}\t{{.Status}}'` # 有没有同名容器在跑
- `df -h .`                                        # 剩余磁盘（数据目录要放机械盘）
- `free -h`                                        # 至少 2GB 可用内存
- 检查 8012 端口是否被占用：`ss -ltnp | grep 8012`
- 列出仓库根目录下的 docker-compose*.yml，说明哪个是废弃的
  （提示：run.yml / nas.yml 是把前后端拆成两个容器的旧方案，现在后端自己托管前端，
   拆开跑不起来，用 docker-compose.install.yml）

## 步骤 2：告诉我你的方案，不要直接开始装
先用一段话说清楚你准备怎么做，包含：
- 用 SQLite 还是 PostgreSQL（SQLite 更省事；已有库或需要百万级数据量才用 PG）
- 数据目录放哪（建议机械盘，不要放 SSD/缓存盘）
- 宿主机端口用哪个
- 需要我提供哪些信息（qB/Jellyfin/amane 的地址与密钥；没有就说没有）

**如果用户要用 Jellyfin 下载入库，必须同时提醒这件事（系统不替你建库）：**

> 需要你先去 Jellyfin 后台「控制台 → 媒体库 → 添加媒体库」建一个**专用库**：
> 名称建议 `黄片`、内容类型必须是**电影**、路径指向一个专用空目录。
> 不要把这些内容混进你现有的分类库 —— 本项目只处理「电影」类型，
> 且入库时会按 `工作室/番号` 重排目录，混进已有分类会打乱你原有的组织。
> 建好之后告诉我库名，我填进向导。

**然后停下来等我确认。**

## 步骤 3：生成配置
- `cp .env.install.example .env`
- 生成强密码填入：`openssl rand -base64 24`
- 按上面的方案调整 .env（端口、数据目录、是否要 PG）

## 步骤 4：启动
```bash
# SQLite 方案
docker compose -f docker-compose.install.yml up -d

# PostgreSQL 方案（密码校验只在带 profile 时才触发，所以这条命令会强制检查密码是否已填）
docker compose -f docker-compose.install.yml --profile with-db up -d
```
等 60 秒让容器完成初始化。

## 步骤 5：验证（每条都要跑，把真实输出贴给我）
```bash
docker compose -f docker-compose.install.yml ps          # STATUS 必须是 healthy，不是 running
docker compose -f docker-compose.install.yml logs --tail=80 busterminal   # 看有没有 ERROR / traceback

curl -s http://127.0.0.1:<端口>/api/health/live          # 进程活着
curl -s http://127.0.0.1:<端口>/api/health/check         # ★ 关键：status=ok 且 migration_required=false
curl -s http://127.0.0.1:<端口>/api/onboarding/status    # 引导状态，completed 应为 false
```

**判定标准（不要放宽）**：
- `ps` 里必须显示 `healthy`。如果是 `running` 但一直不 healthy，看日志找原因。
- `/api/health/check` 里 `migration_required` 必须是 `false`。
  若为 `true`，说明建表没执行，检查 `.env` 里
  `BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION=true` 是否生效，然后重建容器。
- 日志里出现 `Automatic schema migration is disabled` 就是上面那个问题。
- `/api/onboarding/status` 若返回 401，说明鉴权已开且还没建管理员，
  这时用浏览器打开首页会进 `/setup` 创建管理员，属正常。

## 步骤 6：报告
给我一份简短报告，包含：
- 实际用的端口、数据目录位置、SQLite 还是 PG
- 上面的验证命令的真实输出
- 有没有未解决的问题（有就直说，不要说"全部正常"来掩盖）
- 下一步我该做什么（通常是：浏览器打开地址 → 走 /setup 向导填组件地址）

## 如果启动失败
按这个顺序排查，把每步结果告诉我：
1. `logs` 里第一条 ERROR 是什么（不要跳过日志直接猜）
2. 数据库连不上 → 确认 `.env` 里 DATABASE_URL 的用户名/库名是否都叫 busterminal，
   两者不一致会报 `role does not exist`
3. 端口冲突 → 改 .env 里的 BUS_TERMINAL_PORT
4. 磁盘/权限 → 数据目录当前用户是否可写
5. 以上都不是 → 把完整 logs 给我，我们一起看

常见问题对照表在 docs/INSTALL.md 的「常见故障」，先查那张表。
````

---

## 排障模式（已经装好了，只发这一段）

````text
我的 BusTerminal 出了 <描述问题>，请帮我排查。

## 红线
1. 不要删除/重置任何数据（尤其是 ./data 和数据库卷）。排查阶段一律只读操作。
2. 不要为了让接口返回 200 而放宽校验或关掉功能。
3. 不要在没拿到真实输出前下结论。每个判断都要有命令输出支撑。

## 先采集现状（全部执行并把输出给我）
```bash
docker compose -f docker-compose.install.yml ps
docker compose -f docker-compose.install.yml logs --tail=200 busterminal
curl -s http://127.0.0.1:<端口>/api/health/live
curl -s http://127.0.0.1:<端口>/api/health/check
curl -s http://127.0.0.1:<端口>/api/onboarding/status
curl -s http://127.0.0.1:<端口>/api/amane/selfcheck     # 媒体栈链路自检
curl -s http://127.0.0.1:<端口>/api/tasks/status        # 后台任务状态
```

## 然后按症状定位
- **打不开页面** → 看容器是否 healthy、端口是否被占
- **页面一直跳 /setup** → 引导没完成，是正常拦截；确认是否误重置
- **提示表不存在 / 日志有 schema migration is disabled**
  → .env 里 BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION 改成 true 并 `up -d` 重建
- **下载完成但没入库**
  → ① `/api/amane/selfcheck` 看哪一环 fail
  → ② 核对投递目录：qB 里的路径、amane 里的路径、.env 里的 BUS_DOWNLOADS_DIR
     三者必须是同一个真实目录，这是最常见的原因
- **日志提示「未找到媒体库」/ 入库后 Jellyfin 里看不到**
  → 用 `curl -s http://127.0.0.1:<端口>/api/jellyfin/libraries` 看 `is_preferred`
     落在哪个库上；若没有 true 的项或不是目标库，说明库名没对上
  → 库的**内容类型必须是「电影」**，选成剧集系统不会处理
  → 库名要么含「黄片」二字，要么在向导里填成实际库名
- **封面加载慢** → 封面缓存目录是否被清空；确认数据目录在机械盘
- **数据看起来不对** → 先用 `/api/onboarding/database/check` 确认实际连的是哪个库，
  再判断是不是接错库。DATABASE_URL 运行期改不了，要改 .env 后重建容器
- **数据看板数字看着离谱**（比如存储几 PB）→ 这是口径混算，
  检查是不是把本地候选库条目数和媒体库条目数搞混了

## 输出要求
按「现象 → 根因 → 证据（命令输出）→ 处理动作 → 是否需要我确认」的结构写。
需要我确认才能做的动作（改配置、重建容器、删任何东西）先说明再等确认。
````

---

## 三种典型场景的补充提示

### 只想先看看（不接外部服务）

告诉 agent：「先用 SQLite 方案跑起来，我不接 qB 和 Jellyfin，只要能进页面看功能。」
这样不需要准备任何密钥。系统可以浏览内容、查数据看板，但不能下载与播放。

### 已有 PostgreSQL 要接进去

附加这段：

````text
我要接入已有的 PostgreSQL，不要用你自带的数据库容器。
数据库地址是：postgresql://用户名:密码@地址:5432/库名

请把它写进 .env 的 DATABASE_URL，然后启动。
启动后用这个命令确认接对了：
curl -s -X POST http://127.0.0.1:<端口>/api/onboarding/database/check \
  -H 'Content-Type: application/json' \
  -d '{"expected_url":"postgresql://用户名:密码@地址:5432/库名"}'

返回里 matches_expected 必须是 true。如果不是 false，请告诉我当前实际连的是哪个库，
不要自己去改数据库里的东西。
````

### 要跑通完整链路（qB + amane + Jellyfin）

附加这段（这段最容易出问题，务必让 agent 认真核对目录）：

````text
我要跑通完整链路：BusTerminal → qBittorrent 下载 → amane 刮削整理 → Jellyfin 呈现。

各组件地址和密钥我会提供给你。在启动之前，请先帮我核对**目录一致性**，
这是这条链路最常见的失败点：

1. 我会告诉你 qBittorrent 里的下载保存路径
2. 我会告诉你 amane 里的库路径
3. .env 里的 BUS_DOWNLOADS_DIR 会映射成容器内的 /downloads

请确认这三者指向同一个真实目录 —— 如果 qB 写 /downloads/staging 而容器挂载的是
/mnt/hdd1/downloads，下载能完成但 amane 永远扫不到，表现为"一直卡在已下载未入库"。

启动后请跑：
curl -s http://127.0.0.1:<端口>/api/amane/selfcheck

把 checks 里每一项的 status 和 hint 贴给我，逐项确认都是 ok 再算链路通了。
只看到容器 healthy 不足以说明链路通。
````

---

## 一句话提醒

如果 agent 告诉你"部署完成，一切正常"，但没贴出上面验证命令的真实输出，
**那不算验证**。这份文档里的验证步骤就是为了防止这种情况 ——
容器显示 `running` 但接口报表不存在、页面能开但功能全空，都是真实发生过的。