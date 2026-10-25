# BusTerminal

**面向个人媒体库的自动化控制台。** 把内容发现、订阅追更、磁链选择、下载调度、
文件整理、媒体库状态与清理维护串成一条闭环，并提供一个统一的操作界面。

**📖 在线介绍站点：<https://seldoms.github.io/BusTerminal/>**（含特性详解与常见问题，支持中英切换）

<p align="center">
  <img src="docs/images/onboarding-overview.png" width="720" alt="首次部署引导向导">
</p>

## 它解决什么问题

个人媒体库的痛点是"环节太多、工具太散"：在 A 站找片、在 B 站看磁链、
手动丢给下载器、下完再手动改名归档、最后让媒体服务器扫库。
每个环节都要人盯着，任何一环断了都很难发现。

BusTerminal 做的事情是**把这条链路的状态集中起来，并让每一步都可验证**：

```text
内容发现 → 元数据缓存 → 磁链选择 → 下载执行 → 文件投放
      → 媒体库入库 → 行为反馈 → 订阅 / 清理决策
```

它**不替代**你已有的下载器、刮削工具和媒体服务器 —— 它是这些工具之上的
**调度与索引层**，通过 HTTP API 指挥它们做事。

## 快速开始

### 一行命令跑起来

官方镜像已发布在 GHCR，不用 clone、不用构建：

```bash
docker run -d --name bus_terminal -p 8012:8000 \
  -v "$PWD/busterminal-data:/app/data" \
  ghcr.io/seldoms/busterminal:latest
```

然后打开 `http://<你的地址>:8012`，会自动进入**首次部署引导向导** `/setup`，
按步骤配置接口与授权、下载习惯、屏蔽关键词、缓存策略与入库闭环。

> **不要跳过向导。** 未完成向导时其它页面一律不开放，这是刻意的设计，
> 避免在半配置状态下把服务暴露出去。
>
> `/app/data` 是**必须持久化**的目录 —— 封面缓存、运行配置、SQLite 回退库都在里面。
> 删掉它等于把已经缓存好的几千张封面重新抓一遍。

默认使用 SQLite（单机几十万条以内够用），鉴权默认开启（`REQUIRE_AUTH=true`）。
要换 PostgreSQL、或不想在命令行里堆一串 `-e`，用下面的 compose。

> 镜像目前只构建 **linux/amd64**。ARM 设备（如部分 ARM 版 NAS、Apple Silicon）
> 请用 [场景 E](docs/INSTALL.md#场景-e从源码构建不用预构建镜像) 从源码构建。

### 用 compose 起（推荐，自带 PostgreSQL）

```bash
git clone https://github.com/seldoms/BusTerminal.git busterminal && cd busterminal

cp .env.install.example .env
vi .env                       # 只需改两项：BUS_TERMINAL_PORT 与 BUS_PG_PASSWORD

docker compose -f docker-compose.install.yml up -d
```

### 一把装齐整条链路（PostgreSQL + qBittorrent + amane + Jellyfin）

如果这台机器上什么都还没有，[`docker-compose.fullstack.yml`](docker-compose.fullstack.yml)
把应用、数据库、下载器、刮削入库、媒体库五个服务都写好了：

```bash
git clone https://github.com/seldoms/BusTerminal.git busterminal && cd busterminal
cp .env.fullstack.example .env
vi .env                       # 选要新建哪些服务、端口、以及媒体盘的路径
docker compose -f docker-compose.fullstack.yml up -d
```

**已经装过的服务不会被重复安装。** 每个可选服务各挂一个 profile，只把需要**新建**的
写进 `COMPOSE_PROFILES`；已有的（哪怕跑在**别的机器**上）不用启用，改成在 `.env` 里
把 BusTerminal 的接入地址指向它即可。

### 或者，让 AI agent 帮你装

把 [docs/AGENT_FULLSTACK_INSTALL.md](docs/AGENT_FULLSTACK_INSTALL.md) 里的提示词
整段交给你的 agent（Claude Code / Cursor / Codex 都可以）。它会按硬流程走：

```text
探查本机（已装容器 / 端口占用 / 磁盘余量）
   → 把服务清单交给你逐项选择（新建 / 复用 / 接入 / 不要）
   → 生成安装确认单 → 等你回复「确认」
   → 才动手 → 跑真实验证 → 报告 + 列出需要你手工完成的步骤
```

不确认不动手；你已有的服务它只复用、不重装、不改配置。

完整安装路径、使用动线与分场景步骤见 **[docs/INSTALL.md](docs/INSTALL.md)**；
每一项配置的含义见 **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**。

## 需要什么

| 依赖 | 必需性 | 缺了会怎样 |
| --- | --- | --- |
| Docker 20.10+ 与 `docker compose` | **必需** | 装不起来 |
| PostgreSQL | 可选 | 留空则回退 SQLite，单机几十万条以内够用 |
| [qBittorrent](https://www.qbittorrent.org/) | 可选 | 只能浏览与搜索，不能下载 |
| [amane](https://github.com/sqzw-x/amane) | 可选 | 能下载，但不会自动刮削整理入库 |
| [Jellyfin](https://jellyfin.org/) | 可选 | 不能播放，也拿不到观看行为数据 |
| 翻译服务（Google 免费 / Ollama / 百度） | 可选 | 标题保持原文 |

总线上的服务自己管自己的文件：BusTerminal **不碰媒体文件**，只负责决策与索引。

## 主要能力

| 能力 | 说明 |
| --- | --- |
| 内容发现与订阅 | 多来源发现、演员/分类/关键词订阅与自动追更 |
| 元数据与缓存 | 影片、演员、封面、翻译、详情页的分层缓存，本地图片缓存显著降低二次加载耗时 |
| 磁链选择 | 按清晰度、字幕、文件大小等规则筛选，避免手挑 |
| 下载调度 | 对接 qBittorrent，离线时按指数退避重试，不轰炸不可用服务 |
| 自动刮削入库 | 对接 amane，按番号归档；入库失败可在界面里看到原因并就地重试 |
| 媒体库集成 | 读取 Jellyfin 媒体库、封面、观看状态、收藏与人员信息 |
| 行为分析与自动化 | 根据观看行为生成关注、订阅、下载与清理候选（默认只建议，不自动执行） |
| 清理与维护 | 清理候选、缓存回收、做种维护、删除保护闸 |
| 屏蔽关键词 | 列表展示与下载入队两处同时拦截，保存前可试算命中范围 |
| 数据看板 | 类型分布、关注演员、播放统计、存储与运行时长的汇总看板 |
| 观看提醒邮件 | 长期未观看时生成带内联 SVG 图表的汇总邮件 |

<p align="center">
  <img src="docs/images/home-blurred.jpg" width="720" alt="首页（演示数据已打码）">
</p>

## 技术栈

- **后端**：Python 3.12 · FastAPI · Uvicorn
- **前端**：React 18 · Vite · Chart.js
- **数据库**：PostgreSQL 16（未配置时回退 SQLite）
- **部署**：Docker / Docker Compose，单容器即可运行（后端直接托管前端产物）

## 安全边界（请务必阅读）

本工具面向**可信内网**，请在部署时保持以下边界：

- **不要直接暴露到公网。** 建议放在带认证的反向代理之后，或仅通过 VPN / 内网访问。
- 首次部署保持 `REQUIRE_AUTH=true`，并在向导里建好管理员账号。
- 不涉及真实下载、删除、媒体库写入的操作可以放心试用；
  涉及破坏性操作时，系统有显式闸门与数量上限。
- 发现安全问题请按 [SECURITY.md](SECURITY.md) 的方式私下报告。

## 法律与合规

- 本项目是**调度与索引工具**，不内置、不分发任何受版权保护的媒体内容。
- 使用者需自行确保对接入的数据源、下载行为与所处理的内容符合当地法律法规
  及目标站点的服务条款。
- 本项目以 **GPL-3.0** 发布；其中包含移植自
  [amane](https://github.com/sqzw-x/amane)（同样是 GPL-3.0）的代码。
  详见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

> ⚠️ **分发已编译的镜像同样构成分发**：如果你把本项目打包成镜像提供给他人，
> 必须同时提供完整源码（本仓库地址即可满足该要求），并保留 GPL 声明。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/INSTALL.md](docs/INSTALL.md) | 安装指南：使用动线、安装路径组织、分场景步骤、故障排查 |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | 配置指南：三层配置优先级、逐项说明、敏感项怎么填 |
| [docs/AGENT_FULLSTACK_INSTALL.md](docs/AGENT_FULLSTACK_INSTALL.md) | 给 AI agent 的**全栈部署**提示词：先探查 → 交你选 → 再执行 |
| [docs/AGENT_INSTALL_PROMPT.md](docs/AGENT_INSTALL_PROMPT.md) | 给 AI agent 的轻量安装与排障提示词（只装本体） |
| [docs/API.md](docs/API.md) | HTTP 接口清单 |
| [SECURITY.md](SECURITY.md) | 安全策略与部署边界 |
| [NOTICE](NOTICE) | 版权与第三方代码声明 |

## 贡献

欢迎提交 Issue 与 Pull Request。提交前请确认：

1. 代码里不包含任何真实地址、凭据、个人路径（配置一律用占位符或环境变量）。
2. 新增功能配套了测试，且相关测试通过。
3. 若引入了第三方代码，请在 [NOTICE](NOTICE) 中登记来源与许可证。

本项目采用 GPL-3.0：你提交的代码将同样以 GPL-3.0 授权。
