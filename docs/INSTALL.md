# BusTerminal 安装指南

面向"第一次装"的读者。先看[零、整体怎么组织](#零先读这一节整体怎么组织)理解全貌，
再看[你应该用哪种方式](#一先确认你的情况)，然后照抄命令。

部署完成后有**初始化向导** `/setup` 引导你填接口与授权、下载习惯、屏蔽关键词、
缓存策略与入库闭环，并实时告诉你这套系统能做什么。**不要跳过它** ——
未完成向导时其他页面一律不开放。

> 想只看配置项的逐条说明，见 [CONFIGURATION.md](./CONFIGURATION.md)。

---

## 零、先读这一节：整体怎么组织

### 0.1 使用动线（从获取到用起来）

整个流程分五步。**每一步都有明确的验证方式**，不要跳步往下走。

```text
① 获取        git clone / docker run 预构建镜像 / 下载 release
                  │  验证：目录里有 docker-compose.install.yml 与 .env.install.example
                  ▼
② 配置        cp .env.install.example .env
              改两个值：端口、数据库口令
                  │  验证：cat .env 能看到你改过的值
                  ▼
③ 启动        docker compose -f docker-compose.install.yml up -d
                  │  验证：ps 显示 healthy；/api/health/check 返回 migration_required=false
                  ▼
④ 初始化     浏览器打开 http://<你的地址>:<端口> → 自动进入 /setup 向导
              分 7 步：能力说明 → 接口与授权 → 下载习惯 → 屏蔽关键词 →
                       缓存策略 → 数据库接入 → 入库闭环
                  │  验证：每步的"测试连接"按钮返回版本号 / 有 ✓
                  ▼
⑤ 日常使用    浏览与搜索 → 选片 → 下载 → （可选）自动刮削入库 → Jellyfin 播放
              设置页按需调整；数据看板看汇总
                  │  验证：接好外部服务后跑 /api/amane/selfcheck，checks 全 ok
```

**四种起手式，按你的情况选一种：**

| 你的情况 | 用哪个 | 一条命令 |
| --- | --- | --- |
| 只想先看看它长什么样 | 预构建镜像，不用 clone | `docker run -d --name bus_terminal -p 8012:8000 -v "$PWD/busterminal-data:/app/data" ghcr.io/seldoms/busterminal:latest` |
| 要正经用（含 PostgreSQL） | [最短路径](#二最短路径先跑起来再说) / [场景 B](#场景-b接入你已有的数据库) | `docker compose -f docker-compose.install.yml up -d` |
| 这台机器什么都没有，想一把装齐整条链路 | [场景 F](#场景-f全栈一键应用--数据库--下载器--刮削--媒体库) | `docker compose -f docker-compose.fullstack.yml up -d` |
| 不想自己一步步读文档 | [AGENT_FULLSTACK_INSTALL.md](./AGENT_FULLSTACK_INSTALL.md) | 把里面的提示词整段交给你的 AI agent |

**动线里三个"卡点"，出问题十有八九是这三个：**

1. **③ 起不来** —— 多半是数据库连不上或建表开关没开。见[常见故障](#常见故障)。
2. **④ 向导过不去** —— 向导没做完时**其他页面一律不开放**（前端 `SetupGate` 拦截），
   这是预期行为，不是故障。
3. **⑤ 下载了但没入库** —— 99% 是投递目录没对齐，见 [C-1](#c-1-关键前提目录必须对齐)。

### 0.2 安装路径怎么组织

**只有一个硬要求：三类数据要落在同一块持久盘上，且不要放在容器里。**

```text
<你的仓库目录>/                      ← 代码，可随时删掉重来
├── docker-compose.install.yml      ← 唯一需要用的编排文件
├── .env                            ← 你的配置（不入版本库）
├── data/                           ← 【必须持久化】封面缓存 + 运行配置 + SQLite 库
├── downloads/                      ← 【必须与 qB/amane 对齐】下载投递目录
└── pgdata/                         ← 【仅 with-db 方案】PostgreSQL 数据目录
```

| 目录 | 内容 | 丢了会怎样 | 建议位置 |
| --- | --- | --- | --- |
| `data/` | 封面缓存、系统配置、SQLite 回退库 | 封面要重新抓（慢），配置与业务数据丢失 | **机械盘**，纳入备份 |
| `downloads/` | qB 下载完成后投放的文件 | 下载成果丢失 | 大容量机械盘，**与 qB / amane 同一真实目录** |
| `pgdata/` | PostgreSQL 数据文件 | 数据全丢 | ⚠️ **必须机械盘**。放 SSD/NVMe 缓存盘会因 4k-fsync 极慢把容器拖死 |

三处怎么改：

```bash
# 在 .env 里
BUS_DATA_DIR=/mnt/hdd1/busterminal/data        # 或 ./data（跟着仓库走）
BUS_DOWNLOADS_DIR=/mnt/hdd1/downloads          # 必须与 qB / amane 看到的路径一致
BUS_PG_DATA_DIR=/mnt/hdd1/busterminal/pgdata   # 仅 with-db 方案需要
```

**NAS / 群晖类系统**：不要映射到系统盘或 SSD 缓存卷。先在 NAS 界面建好共享文件夹
（如 `docker/busterminal/data`），再把**宿主机真实路径**填进 `.env`。

**备份只需备份两样**：`data/` 目录 + Postgres 数据目录（或 `pg_dump` 的逻辑备份）。
代码目录不需要备份。

### 0.3 与依赖和外部服务的关系

BusTerminal 是**调度与索引层**：自己不下载、不刮削、不播放、不存媒体文件。
它只通过 HTTP API 指挥别的服务做事。

```text
                  ┌───────────────────┐
   要下载 ───────►│   qBittorrent     │   只负责下载
                  └───────────────────┘
                  ┌───────────────────┐
   要自动入库 ───►│   amane           │   只负责刮削 / 整理 / 按番号归档
                  └───────────────────┘
                  ┌───────────────────┐
   要播放 ───────►│   Jellyfin        │   呈现、播放、产生观看行为数据
                  └───────────────────┘
                  ┌───────────────────┐
   要中文标题 ───►│   翻译服务         │   google_free（零配置）/ Ollama / 百度
                  └───────────────────┘
                  ┌───────────────────┐
   要百万级数据 ─►│   PostgreSQL      │   留空则回退 SQLite，单机够用
                  └───────────────────┘
```

| 依赖 | 必需性 | 缺了会怎样 | 怎么接 |
| --- | --- | --- | --- |
| Docker 20.10+ / `docker compose` | **必需** | 装不起来 | 先装好 |
| PostgreSQL | 可选 | 回退 SQLite，几十万条以内够用 | `--profile with-db` 或填 `DATABASE_URL` |
| qBittorrent | 可选 | 只能浏览、搜索，不能下载 | 向导「接口与授权」填地址 + WebUI 账号密码 |
| amane | 可选 | 能下载，但不会自动刮削整理入库 | 向导填服务地址 + API 令牌 |
| Jellyfin | 可选 | 不能播放，也拿不到观看行为 | 向导填地址 + API Key |
| 翻译服务 | 可选 | 标题保持日文原文 | 默认 `google_free`，零配置 |

**两条容易踩的关系约束：**

1. **网络必须双向可达**：BusTerminal 要能访问 qB / Jellyfin / amane 的地址；
   容器部署时注意别让这些内网请求走代理（`BUS_TERMINAL_INTERNAL_PROXY_ENABLED` 保持 `false`）。
2. **目录必须对齐**：qB 的下载落点、amane 的库根、`DELIVERY_FOLDER` 必须是
   同一块存储上的同一条路径。三者不一致的典型症状是"下载完成了但永远不入库"。

---

## 一、先确认你的情况

先回答三个问题，再往下看对应场景。

| 问题 | 影响 |
| --- | --- |
| 有没有现成的 PostgreSQL 库要接？ | 有 → [场景 B](#场景-b接入你已有的数据库)，没有 → [场景 A](#场景-a最简试跑sqlite推荐先走这条) |
| 有没有 qBittorrent / Jellyfin？ | 有 → 装完在向导里填地址；没有 → 系统只能浏览，不能下载与播放 |
| Jellyfin 里建好专用媒体库了吗？ | **要下载入库就得先建**，见 [C-0](#c-0-先决条件jellyfin-里必须有一个专用媒体库)。系统不替你建库 |
| 要不要自动刮削整理（amane）？ | 要 → [场景 C](#场景-c完整链路下载--刮削--入库--播放)；不要 → 只到下载为止 |

**共同前置条件**

- Docker 20.10+ 与 `docker compose`（`docker compose version` 能输出即可）
- 一个数据目录（封面缓存 + 数据库），建议挂在机械盘
- 至少 2GB 可用内存

---

## 二、最短路径（先跑起来再说）

只想先看看这东西长什么样，10 分钟内能装完。

```bash
git clone https://github.com/seldoms/BusTerminal.git busterminal && cd busterminal

cp .env.install.example .env
# 只改这一行端口（其余先不动）
vi .env            # BUS_TERMINAL_PORT=8012

docker compose -f docker-compose.install.yml up -d
```

打开 `http://<你的机器IP>:8012`，会直接进入 `/setup` 向导，按提示填完即可。

默认用 **SQLite**（数据库文件在 `./data/movies.db`），不需要额外容器。

**验证是否装成功**

```bash
docker compose -f docker-compose.install.yml ps        # STATUS 应为 healthy
curl -s http://127.0.0.1:8012/api/health/live          # {"success":true,...}
curl -s http://127.0.0.1:8012/api/onboarding/status     # completed:false = 该填向导了
```

> ⚠️ **只有 HTTP 200 不代表能用**。`/api/health/live` 只证明进程活着。
> 真正要看业务是否正常，请接着跑：
>
> ```bash
> curl -s http://127.0.0.1:8012/api/health/check | python3 -m json.tool
> ```
>
> 关注两点：`"migration_required": false`（表已建好）与 `"status": "ok"`。
> 若 `migration_required` 为 true，说明 `BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION` 没生效。

---

## 场景 A：最简试跑（SQLite，推荐先走这条）

**适合**：想先评估功能，不需要已有组件。

就是上面的"最短路径"，什么都不用改。数据量到几十万条之前 SQLite 都够用。

**数据在哪**

```
./data/movies.db        # SQLite 数据库
./data/cover_cache/     # 封面缓存（重要性能资产，删了要重新抓）
./downloads/staging/    # qB 下载完成后的投递目录
```

---

## 场景 B：接入你已有的数据库

**适合**：已有 PostgreSQL，或需要 145 万级数据量、多人共用。

### B-1 用本项目自带的 Postgres 容器

```bash
cp .env.install.example .env
vi .env
#   1) 生成强密码填进去：openssl rand -base64 24
#      BUS_PG_PASSWORD=<刚生成的密码>
#   2) 数据库目录放机械盘：
#      BUS_PG_DATA_DIR=/mnt/hdd1/pgdata

docker compose -f docker-compose.install.yml --profile with-db up -d
```

启动后把连接串写进 `.env`（密码与上面一致）再重启：

```bash
vi .env
# 取消注释并改密码：
# DATABASE_URL=postgresql://busterminal:<密码>@bus_postgres:5432/busterminal

docker compose -f docker-compose.install.yml --profile with-db up -d
```

> ⚠️ **角色名与库名必须都叫 `busterminal`**。只改一个会导致连不上（`role does not exist`）。

> ⚠️ **数据库放机械盘，不要放 SSD / NAS 的 SSD 缓存卷**。
> PostgreSQL 的 4k-fsync 在这类盘上写入极慢，会把容器拖到无响应。

### B-2 接入已有的 PostgreSQL

```bash
vi .env
DATABASE_URL=postgresql://用户名:密码@数据库地址:5432/库名
# 然后正常起（不要加 --profile with-db）
docker compose -f docker-compose.install.yml up -d
```

**验证确实接上了**（向导第 5 步也有同样的体检）：

```bash
curl -s -X POST http://127.0.0.1:8012/api/onboarding/database/check \
  -H 'Content-Type: application/json' -d '{"expected_url":"postgresql://用户名:密码@数据库地址:5432/库名"}'
```

返回里 `matches_expected` 为 `true` 即接对了。

> ⚠️ `DATABASE_URL` **只在进程启动时读取，运行期无法切换**。
> 接错库要改 `.env` 后 `docker compose up -d` 重建容器。

---

## 场景 C：完整链路（下载 → 刮削 → 入库 → 播放）

**适合**：要自动化跑通整条流水线。

需要额外两个外部服务：**qBittorrent**（下载）与 **amane**（刮削整理）。
Jellyfin 用于呈现与播放，可选但强烈建议。

```
BusTerminal  ──决策/编排──┐
      │                  ├─→  qBittorrent  下载（只收磁力、设做种时限）
      ├─→  amane      刮削、整理、按番号归档
      └─→  Jellyfin  呈现与播放
```

### C-0 先决条件：Jellyfin 里必须有一个专用媒体库

**这一步要你手动做，系统不会替你建库**（系统不碰 Jellyfin 的建库与目录结构）。

去 Jellyfin 后台 →「控制台 → 媒体库 → 添加媒体库」：

| 项 | 建议填 |
| --- | --- |
| 媒体库名称 | `黄片`（或你自己的名字，见下方说明） |
| 内容类型 | **电影**（必须是电影，选成"剧集"不会被识别） |
| 路径 | 指向一个**专用空目录**，例如 `/media/adult` |

**为什么要单独建一个库，而不是混在现有库里**

- 本项目只对"电影"类型生效。混在剧集库里，BusTerminal 看不到、也不会入库。
- 自动化下载的东西不该打散进你现有的分类里，否则原有分类与筛选会被污染。
- 清理候选、删除审批这些自动化能力只在自己的库里生效，不会误伤你手动整理的内容。

**如果你已经有分类了**（比如 `电影/HD`、`电影/中文字幕`）——

**建议仍然新建一个专用库**。因为整理入库时会按 `工作室/番号` 重排目录结构，
混进已有分类会打乱你原有的目录组织。已有分类保留给手动管理的内容，
自动化内容单独放一个库，两边互不干扰。

**库名能不能改**：能。系统按两种方式找你的库 ——
库名**等于**你配置的名称，或库名里**含有「黄片」二字**。
所以叫「adult」「AV」这类名字时，要在向导第 6 步把
「Jellyfin 媒体库名称」填成你的实际库名，否则系统找不到这个库。

### C-1 关键前提：目录必须对齐

**投递目录（staging）必须是 qB 与 amane 看到的同一个真实目录。**

最常见的失败：qB 里填的是 `/downloads/staging`，amane 里填的是 `/mnt/hdd1/downloads/staging`，
容器内挂载点却只有 `/downloads`。结果就是下载完成了、amane 永远扫不到，表现为"入库一直卡住"。

在 `.env` 里把宿主机的真实路径填进去：

```bash
BUS_DOWNLOADS_DIR=/mnt/hdd1/downloads     # 必须与 qB/amane 实际路径一致
```

### C-2 在向导里填组件地址

启动后进 `/setup`：

| 步骤 | 填什么 | 怎么确认 |
| --- | --- | --- |
| qBittorrent | WebUI 地址 + 端口 + 账号密码 | 点「测试 qB 连接」，应显示版本号 |
| Jellyfin | 服务地址 + API Key | 点「测试 Jellyfin 连接」 |
| amane | 服务地址 + 令牌 | 点「测试 amane 连接」 |
| 自动刮削补齐 | 打开总开关；**媒体库名称填你实际的库名** | 见下方「确认系统能找到你的库」 |

> 密码类字段**不回显明文**。留空表示保持原值，不会覆盖已保存的密钥。

### C-3 确认系统能找到你的媒体库

填完地址后，先确认库能被发现再往下走：

```bash
curl -s http://127.0.0.1:8012/api/jellyfin/libraries | python3 -m json.tool
```

输出形如：

```json
{
  "success": true,
  "data": [
    { "id": "...", "name": "黄片", "type": "movies", "is_preferred": true }
  ],
  "preferred_library_id": "..."
}
```

看两点：

1. `data` 里有没有你建的那个库
2. `is_preferred: true` 的那一项就是系统认定的目标库。若它不是你的库，
   说明库名没对上，在向导第 6 步把「Jellyfin 媒体库名称」改成实际库名
   （或把库名改成含「黄片」二字）

> 这个接口只回库的 `id/name/type/is_preferred`，**不含条目数**。
> 想知道库里有多少条目，去 Jellyfin 后台看，或者用 `/api/jellyfin/stats`。

**若 `data` 里根本没有你建的库**，说明 Jellyfin 那边没建成功，或地址/API Key 不对。
回 [C-0](#c-0-先决条件jellyfin-里必须有一个专用媒体库) 核对库的内容类型是否为「电影」
（`type` 应为 `movies`）。

### C-4 验证整条链路

```bash
curl -s http://127.0.0.1:8012/api/amane/selfcheck | python3 -m json.tool
```

逐项看 `checks[]` 里的 `status`，全为 `ok` 才算链路通。
任何一环 `fail` 都会给出 `hint`，按提示处理。

---

## 场景 D：群晖 / 威联通等 NAS

**适合**：Synology DSM、QNAP QTS、TrueNAS、Unraid 等。

要点与普通 Linux 相同，注意三处：

1. **容器管理界面导入 compose**：把 `docker-compose.install.yml` 内容粘进去，
   环境变量按 `.env.install.example` 对应填写。界面里一般没有 `--profile`，
   所以用 SQLite 方案最省事（`bus_postgres` 那段可以整段删掉）。
2. **共享文件夹映射**：在 NAS 界面建好共享目录（如 `docker/busterminal/data`、
   `docker/busterminal/downloads`），再填到 `BUS_DATA_DIR` / `BUS_DOWNLOADS_DIR`。
3. **反向代理与 HTTPS**：给 `/setup` 配好域名与证书后再对外网开放。
   未完成向导时全站是拦截状态，先在局域网内做完向导。

---

## 场景 E：从源码构建（不用预构建镜像）

默认镜像就是 GHCR 上的官方预构建版，**不需要构建**。只有想用自己改过的代码时才走这条：

```bash
# 先取消 docker-compose.install.yml 里 busterminal 服务下 build 段的注释
docker compose -f docker-compose.install.yml up -d --build
```

构建产物会打上默认镜像名（`ghcr.io/seldoms/busterminal:latest`）。
不想覆盖它，就在 `.env` 里把 `BUS_TERMINAL_IMAGE` 改成别的名字再构建。

国内网络直连 pypi.org 可能失败，compose 里已默认走清华源。需要换源时改
`PIP_INDEX_URL` 为你可达的镜像。

---

## 场景 F：全栈一键（应用 + 数据库 + 下载器 + 刮削 + 媒体库）

**适合**：这台机器上什么都还没有，想一次把整条链路铺好。

[`docker-compose.fullstack.yml`](../docker-compose.fullstack.yml) 里已经把五个服务写好了：
`busterminal` + `bus_postgres` + `qbittorrent` + `amane` + `jellyfin`。

```bash
git clone https://github.com/seldoms/BusTerminal.git busterminal && cd busterminal
cp .env.fullstack.example .env
vi .env
docker compose -f docker-compose.fullstack.yml up -d
```

### F-1 先决定"装哪些"（默认其实只起 BusTerminal）

compose 里除 `busterminal` 之外的每个服务都挂在**一个 profile** 上，只有 profile 被启用
才会创建容器。所以第一件事是改 `.env` 里的这一行：

```bash
# 只装本体（SQLite，不接任何外部服务）        → 留空
# 本体 + 数据库                              → db
# 全都新建                                    → db,qb,amane,jellyfin
COMPOSE_PROFILES=db,qb,amane,jellyfin
```

**已经装过的服务不要写进去** —— 重复启用会撞端口、撞容器名。
已有的服务（哪怕跑在**别的机器**上）改成在 `.env` 里把接入地址指过去：

```bash
QB_HOST=192.168.1.50                       # 别处已有的 qB
AMANE_BASE_URL=http://192.168.1.50:8000    # 别处已有的 amane
JELLYFIN_HOST=http://192.168.1.50:8096     # 别处已有的 Jellyfin
```

命令行临时启用也等价：

```bash
COMPOSE_PROFILES=db,qb,amane,jellyfin docker compose -f docker-compose.fullstack.yml up -d
```

> `depends_on` 里的 `required: false` 需要 Docker Compose v2.24+。
> 版本更低时删掉整段 `depends_on`，改成先起数据库、等它 healthy 再起其余服务。

### F-2 路径只由一个变量决定

全栈部署最容易踩的坑不是容器起不起得来，而是**四个容器看到的目录不是同一份**。
这个 compose 把这件事收拢成一个变量：

```bash
MEDIA_ROOT=/mnt/hdd1/busterminal-media
```

它会被切成两半挂进各个容器，全部同源：

| 宿主机 | 容器内 | 谁在用 |
| --- | --- | --- |
| `${MEDIA_ROOT}/downloads` | `/downloads` | qBittorrent 下载、BusTerminal 核对 |
| `${MEDIA_ROOT}/library` | `/media` | amane 整理入库、Jellyfin 读取（只读） |

投递目录 `DELIVERY_FOLDER=/media/_incoming` 就落在库根里面，amane 因此能发现新文件。
**跨机器部署时这两条路径必须是几台机器都能访问的同一份存储**（NFS / SMB / 共享文件夹），
否则症状就是"下载完成了但永远不入库"。

### F-3 验证

```bash
docker compose -f docker-compose.fullstack.yml ps             # 每个都应为 healthy
curl -s http://127.0.0.1:<BUS_TERMINAL_PORT>/api/health/check  # migration_required 必须为 false

# 只对**本次新建**的服务单独验证
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<QB_WEBUI_PORT>   # 200
curl -s http://127.0.0.1:<AMANE_PORT>/api/health                            # ok
curl -s http://127.0.0.1:<JELLYFIN_PORT>/System/Info/Public                 # 有 ServerName
```

### F-4 还有几步必须你手工做

这些 agent 代替不了（[场景 C-0](#c-0-先决条件jellyfin-里必须有一个专用媒体库) 已详述前两条）：

1. 打开 `http://<地址>:<BUS_TERMINAL_PORT>` 走完 `/setup` 向导，建管理员账号。
2. **Jellyfin 里新建一个专用媒体库**：内容类型必须是「电影」，路径填 `/media`。
   不要混进你已有的分类库 —— 入库时会按 `工作室/番号` 重排目录。
3. 新建的 qBittorrent：初始密码在日志里
   `docker logs qbittorrent 2>&1 | grep -i "temporary password"`，登录后立即改，
   并把默认保存路径设为 `/downloads`。
4. 新建的 amane：令牌在日志里 `docker logs amane 2>&1 | grep -i token`；
   登录后在「管理 → 媒体库」添加一个库，媒体目录填 `/media`，自动化选「监控 + 刮削」。
5. 回到向导，把 qB / amane / Jellyfin 的地址与密钥填进去。
6. 跑 [`C-4`](#c-4-验证整条链路) 的自检确认链路通。

> 不想自己走这套流程？把 [AGENT_FULLSTACK_INSTALL.md](./AGENT_FULLSTACK_INSTALL.md) 的
> 提示词交给 AI agent —— 它会先探查本机、把清单交给你选、等你确认后才动手。

---

## 运维

### 日常命令

```bash
# 看状态与日志
docker compose -f docker-compose.install.yml ps
docker compose -f docker-compose.install.yml logs -f busterminal

# 改完 .env 后生效
docker compose -f docker-compose.install.yml up -d

# 强制重算数据看板（平时每天 11:30 / 18:00 自动算两次）
# 页面上的「数据看板汇总」区块也有同一个按钮：设置 → 缓存与性能
curl -s 'http://127.0.0.1:8012/api/bi/overview?refresh=true' | python3 -m json.tool
```

### 备份

**只需备份两样**：`./data`（含封面缓存与配置）与 Postgres 数据目录。

```bash
# Postgres 逻辑备份
docker exec bus_postgres pg_dump -U busterminal busterminal > backup_$(date +%F).sql
# 封面缓存与配置
tar czf backup_data_$(date +%F).tar.gz data/
```

### 回滚

```bash
# 数据不会丢：卷独立于镜像
docker compose -f docker-compose.install.yml down
docker compose -f docker-compose.install.yml up -d
```

---

## 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 打开是 `/setup` 一直转圈 | 向导状态接口没通 | `curl /api/onboarding/status`，看是否 401（鉴权）或 500 |
| 其它页面全部跳 `/setup` | 向导没做完 | 正常现象。做完向导即解除；或 `POST /api/onboarding/apply {"completed":true}` |
| 日志刷 `Automatic schema migration is disabled` | 建表开关没开 | `.env` 里设 `BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION=true` 后重启 |
| 接口报表不存在 | 同上，表没建 | 同上 |
| 容器反复重启 | 多半是数据库连不上 | 看日志里的 `role does not exist` / `could not connect` |
| `role does not exist` | 用户名/库名不一致 | 两个都要叫 `busterminal`，或改 `.env` 里的 `DATABASE_URL` |
| 下载完成但 amane 没反应 | 投递目录不一致 | 见 [C-1](#c-1-关键前提目录必须对齐) |
| 封面加载很慢 | 封面缓存目录被删了 | 缓存会自动重建；`BUS_DATA_DIR` 放机械盘收益更大 |
| 日志提示「未找到媒体库」 | 库名没对上 | 库名要么含「黄片」二字，要么在向导第 6 步填成实际库名；用 `/api/jellyfin/libraries` 看 `is_preferred` 落在哪个库上 |
| 入库后 Jellyfin 里看不到 | 库的内容类型不是「电影」 | 建库时选错类型了，重建一个内容类型为「电影」的库 |
| 数据都在库里但系统当没入库 | 把内容混在剧集库里 | 本项目只处理「电影」类型，需独立建库（见 [C-0](#c-0-先决条件jellyfin-里必须有一个专用媒体库)） |
| 页面打开但数据都是 0 | 数据库连的是空库 | 用 `/api/onboarding/database/check` 核对实际连的是哪个库 |

**排查顺序**：先 `ps` 看容器是否 healthy → 再看 `logs` → 再用上面的验证命令。
**不要**只凭"页面能打开"就认为装好了。

---

## 相关文档

- 配置项逐条说明：[CONFIGURATION.md](./CONFIGURATION.md)
- 初始化向导：`/setup`（部署后第一件事）
- API 接口清单：[API.md](./API.md)
- 让 AI agent 帮你全栈部署（先探查 → 交你选 → 再执行）：[AGENT_FULLSTACK_INSTALL.md](./AGENT_FULLSTACK_INSTALL.md)
- 让 AI agent 帮你装本体与排障：[AGENT_INSTALL_PROMPT.md](./AGENT_INSTALL_PROMPT.md)
- 安全策略与部署边界（根目录）：[SECURITY.md](../SECURITY.md)
- 许可证与第三方代码声明：[NOTICE](../NOTICE)