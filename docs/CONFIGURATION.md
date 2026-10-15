# 配置指南

面向使用者的配置参考。安装步骤见 [INSTALL.md](./INSTALL.md)，
首次配置的实际操作在部署后的 `/setup` 向导里完成。

本文回答三个问题：

1. 配置放在哪里、怎么改、多久生效。
2. 每一项配置是什么意思、必填还是可选。
3. 哪些是敏感项、应该怎么填（以及**不应该**怎么填）。

---

## 一、配置分三层，优先级从低到高

```text
① 环境变量（.env 或 compose 的 environment）
        ↓ 被覆盖
② 数据库里的系统配置（后台「设置」页写入）
        ↓ 被覆盖
③ 运行时即时参数（个别接口的查询参数）
```

| 层 | 改法 | 生效时机 | 适合放什么 |
| --- | --- | --- | --- |
| ① 环境变量 | 编辑 `.env` 后 `docker compose up -d` 重建容器 | **重启生效** | 部署级参数：端口、数据目录、数据库连接、代理 |
| ② 系统配置（数据库） | 后台「设置」页，或 `/setup` 向导 | 保存即生效 | 外部服务地址与密钥、屏蔽关键词、缓存策略、自动化模式 |
| ③ 运行时参数 | 接口查询参数 | 即时 | 一次性操作，如 `?refresh=true` 强制重算 |

**关键区别**：`DATABASE_URL` 属于①，**只在进程启动时读取**，改了必须重建容器，运行期改不动。
而 qB / Jellyfin / amane 的地址与密钥属于②，在设置页改完立即生效。

> ⚠️ 同一个配置项如果两层都设了，**② 赢**。比如 `.env` 里设了 `AMANE_BASE_URL`，
> 后来又在设置页填了 amane 地址，以设置页为准。排障时先看设置页。

---

## 二、配置放在哪里（文件与路径）

| 位置 | 内容 | 是否入版本库 |
| --- | --- | --- |
| `.env`（仓库根，你自己创建） | 环境变量。由 `.env.install.example` 复制而来 | ❌ 绝不 |
| `./data/`（`DATA_DIR`） | 封面缓存、运行配置、SQLite 回退库 | ❌ 本地数据 |
| `./pgdata/`（`BUS_PG_DATA_DIR`） | PostgreSQL 数据目录 | ❌ 本地数据 |
| `./downloads/`（`BUS_DOWNLOADS_DIR`） | 下载投递目录 | ❌ 本地数据 |

**路径写法约定**（本仓库所有文档与模板都遵守）：

- 模板与示例**只用占位符或相对路径**：`./data`、`/mnt/hdd1/busterminal/data`、`<你的数据目录>`。
- **不出现任何人的本机绝对路径**（如 `~\...`）。
- compose 里用 `${VAR:-默认值}` 形式，让使用者能在 `.env` 里覆盖。

---

## 三、必须改的配置（只有两项）

| 项 | 为什么必须改 | 建议 |
| --- | --- | --- |
| `BUS_TERMINAL_PORT` | 默认 `8012`，可能与你的既有服务冲突 | 换成没被占用的端口 |
| `BUS_PG_PASSWORD` | 模板里是占位符 `changeme_please_generate_a_strong_one` | `openssl rand -base64 24` 生成 |

其余全部有可用默认值。**首次部署不需要改任何其它东西**。

> `BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION=true` 在模板里已经设好，**别去掉**，
> 否则自动建表被跳过，容器能起来但接口会报表不存在。

---

## 四、外部服务依赖关系

BusTerminal 本身是**调度与索引层**，自己不下载、不刮削、不播放。
三类外部服务的连接信息全部在部署后的 `/setup` 向导或后台「设置」里填，
**不需要写进 `.env`**（写进去也行，但设置页的值优先）。

```
                       ┌────────────────────┐
       可选 ──────────►│   qBittorrent      │  只负责下载
       （要下载就要）   └────────────────────┘
                       ┌────────────────────┐
       可选 ──────────►│   amane            │  只负责刮削 / 整理 / 归档
       （要自动入库就要）└────────────────────┘
                       ┌────────────────────┐
       可选 ──────────►│   Jellyfin         │  呈现与播放
       （要播放/看行为就要）└──────────────────┘
                       ┌────────────────────┐
       可选 ──────────►│   翻译服务          │  google_free / Ollama / 百度
       （要看中文标题就要）└──────────────────┘

   BusTerminal 只通过 HTTP API 与它们通信，
   不共享文件系统、不写它们的数据库、不需要装在同一个 Docker 网络里
   （但必须网络可达）。
```

| 服务 | 是否必需 | 缺了会怎样 | 需要的信息 |
| --- | --- | --- | --- |
| **qBittorrent** | 可选 | 只能浏览与搜索，不能下载 | 地址、端口、用户名、口令（WebUI 必须开启） |
| **amane** | 可选 | 能下载但不会自动刮削整理入库 | 服务地址 + API 令牌 |
| **Jellyfin** | 可选 | 不能播放，也拿不到观看行为数据 | 服务地址 + API Key（+ 可选 用户 ID） |
| **翻译服务** | 可选 | 标题保持日文原文 | `google_free` 零配置；或 Ollama / 百度 AK-SK |
| **PostgreSQL** | 可选 | 回退 SQLite，单机足够用 | 连接串 |

**三者之间的目录必须对齐**：qB 的下载落点、amane 的库根、BusTerminal 的投递目录
（`DELIVERY_FOLDER`）必须是同一块真实存储上的同一条路径，否则表现为"下载完成了但一直不入库"。
详见 [INSTALL.md 场景 C-1](./INSTALL.md#c-1-关键前提目录必须对齐)。

---

## 五、敏感项怎么填

**统一原则**：

1. **密钥只走界面或环境变量，永不写进任何会被提交的文件。**
2. 后台设置页的密码 / 令牌字段**只写不读**（不回显明文）。
   **留空 = 保持原值不变**，不会把已保存的密钥清空。想换就填新的。
3. `.env` 一旦创建就保持"不入版本库"（`.gitignore` 已覆盖 `.env` / `.env.*`）。
4. 分享配置文件给他人排障前，先把口令字段清空。

| 敏感项 | 存放位置 | 备注 |
| --- | --- | --- |
| `BUS_PG_PASSWORD` | `.env` | 同时被注入自带的 PG 容器 |
| `BUS_TERMINAL_CONFIG_SECRET` | `.env` | 加密库内敏感配置的密钥；**留空 = 那批配置以明文入库**（见下） |
| qB 用户名 / 口令 | 设置页（推荐）或 `.env` | 设置页更安全，不回显 |
| Jellyfin API Key | 设置页 | 在 Jellyfin 后台「API 密钥」生成 |
| amane token | 设置页 | 在 amane 设置页复制 |
| 百度翻译 AK / SK | `.env` 或设置页 | |
| `SYSTEM_PROXY` | `.env` | 若代理带账号密码，等同敏感项 |

### 5.1 库内敏感配置的加密

`BUS_TERMINAL_CONFIG_SECRET` 是库内敏感配置的加密密钥（AES-256-GCM，密文带 `enc:v1:` 前缀）。
**留空不影响功能，但那批配置会以明文存在数据库里**，任何拿到 `pg_dump` 的人都能直接读到下游服务凭据。

被加密的键（`backend/database.py` 的 `SENSITIVE_CONFIG_KEYS`）：

`auth_secret`、`baidu_secret`、`jellyfin_api_key`、`email_pass`、`system_proxy`、`qb_config`、`qb_password`、`amane_token`

三条必须知道的规则：

1. **加密只在写入时发生。** 补上密钥**不会**自动加密已有值，必须去设置页把这几项重填一遍
   （旧明文值仍可读，所以重填前功能正常，不用停服）。
2. **启动日志会点名明文项。** 若检测到还有敏感键没加密，会打出
   `BUS_TERMINAL_CONFIG_SECRET is not set: ... stored in PLAINTEXT`（没配密钥）
   或 `Config values still stored in PLAINTEXT: ...`（配了但旧值没重存）。查到这两条就去重填。
3. 密钥换了会导致旧的密文解不开（那几项需要重新填写）；`.env` 请自行备份到安全位置。

---

## 六、完整配置项参考

下表按用途分组。**默认值**一列即为不配置时的行为。
标"设置页"的项优先走界面配置。

### 6.1 基础运行

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BUS_TERMINAL_PORT` | `8012` | 宿主机映射端口（容器内固定 `8000`） |
| `BUS_TERMINAL_BIND` | `0.0.0.0` | 监听地址；只想本机访问填 `127.0.0.1` |
| `DATA_DIR` | `./data` | 数据目录（封面缓存 + 配置 + SQLite 回退库） |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `TZ` | `Asia/Shanghai` | 时区，影响看板与定时任务的时间口径 |

### 6.2 数据库

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 空 | 空 = 用 `DATA_DIR/movies.db`（SQLite）。**只在启动时读取** |
| `BUS_PG_USER` / `BUS_PG_DB` | `busterminal` | ⚠️ 两者都必须叫 `busterminal`，只改一个会 `role does not exist` |
| `BUS_PG_PASSWORD` | 占位符 | **必须改** |
| `BUS_PG_DATA_DIR` | `./pgdata` | ⚠️ 放机械盘，放 SSD/缓存盘会因 4k-fsync 拖垮容器 |
| `BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION` | `true`（模板） | 首次部署保持 true 建表；日常建议改回 `false` |

### 6.3 安全与鉴权

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REQUIRE_AUTH` | `true` | ⚠️ 保持 true。false = 无鉴权 |
| `BUS_TERMINAL_CONFIG_SECRET` | 空 | 加密库内敏感配置（见 §5.1）；建议用 `openssl rand -base64 32` 生成。**留空 = 那批配置明文入库**（启动日志会告警） |
| `CORS_ORIGINS` | 空 | 同源部署时留空 |
| `BUS_TERMINAL_ALLOW_DESTRUCTIVE_MAINTENANCE` | `false` | 破坏性维护闸门 |
| `BUS_TERMINAL_ALLOW_CACHE_MAINTENANCE` | `false` | 缓存维护闸门 |
| `BUS_TERMINAL_SHOW_ENDPOINTS` / `SHOW_RAW_API_RESPONSE` / `WRITE_RAW_API_RESPONSE` / `SHOW_UNSAFE_RAW_API_RESPONSE` | `false` | 调试开关，生产必须全 false |

### 6.4 网络与代理

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SYSTEM_PROXY` | 空 | 出网代理，抓取境外源站时填 |
| `BUS_TERMINAL_INTERNAL_PROXY_ENABLED` | `false` | ⚠️ 必须 false，否则内网服务请求被代理劫持 |
| `EXTRA_DOMAINS` | 空 | 额外内容源镜像域名，逗号/空格/分号分隔 |
| `SCRAPER_ALLOW_INSECURE_IMAGE_FETCH` | `false` | 图床证书异常时的降级开关，优先修证书 |

### 6.5 缓存与性能

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CACHE_SIZE_LIMIT_GB` | `10` | 磁盘图片缓存上限（设置页的值优先） |
| `BUS_TERMINAL_INFORMATION_FLOW_PRELOAD_PAGES` | `10` | 信息流预加载页数 |
| `BUS_TERMINAL_MOVIE_DETAIL_CACHE_TTL` | `21600` | 详情缓存 TTL（秒） |
| `BUS_TERMINAL_IMAGE_FAILURE_TTL` | `1800` | 坏图标记 TTL（秒） |
| `GROOM_TASK_INTERVAL` | `60` | 下载后处理间隔（秒） |
| `SEEDING_MAINTENANCE_INTERVAL` | `14400` | 做种维护间隔（秒） |
| `BUS_TERMINAL_PRELOAD_ON_STARTUP` | `true` | 启动时预加载 |
| `BUS_TERMINAL_DISABLE_STARTUP_TASKS` | `false` | 本地调试建议 `true`，避免产生外部副作用 |

### 6.6 外部服务（设置页优先）

| 变量 | 说明 |
| --- | --- |
| `QB_HOST` / `QB_PORT` / `QB_USERNAME` / `QB_PASSWORD` / `QB_VERIFY_SSL` | qBittorrent 连接信息。**`QB_HOST` 不要带协议前缀**，只填主机名或地址 |
| `BUS_TERMINAL_QB_ALLOWED_SAVE_ROOTS` | qB 保存路径白名单（留空 = 不限制） |
| `DELIVERY_FOLDER` | 下载完成后的投递目录（容器内路径），默认 `/downloads/staging` |
| `JELLYFIN_HOST` / `JELLYFIN_API_KEY` / `JELLYFIN_USER_ID` | Jellyfin 连接信息 |
| `JELLYFIN_LIBRARY_PATH` | Jellyfin 媒体库根路径 |
| `AMANE_BASE_URL` / `AMANE_TOKEN` | amane 服务地址与令牌。地址留空 = 未配置，功能会明确报错 |
| `AMANE_TIMEOUT_SECONDS` / `AMANE_TASK_TIMEOUT_SECONDS` | amane 请求与任务等待上限 |
| `AMANE_HEAL_ENABLED` | 入库自愈巡检，**默认 false**（会自动写 amane） |

### 6.7 翻译

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRANSLATION_PROVIDER` | `google_free` | 可选 `google_free` / `baidu_dev` / `ollama_local` |
| `BAIDU_APP_ID` / `BAIDU_SECRET` | 空 | 百度翻译开发者凭据 |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | 本地大模型地址（默认回环，即同机部署） |
| `OLLAMA_MODEL` | `qwen3:8b` | 模型名，请按你实际拉取的模型填 |
| `OLLAMA_TIMEOUT` / `OLLAMA_BATCH_SIZE` | `300` / `12` | 单次请求超时与批量条数 |

### 6.8 媒体文件与自动化

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BUS_TERMINAL_MANAGED_MEDIA_ROOTS` | 空 | 允许整理的媒体根目录白名单，逗号分隔；留空 = 不限制 |
| `BUS_TERMINAL_LEGACY_ORGANIZER` | `false` | 旧版组织器实现（已不推荐） |
| `JABLE_HOT_AUTO_DOWNLOAD` | `false` | 热榜自动下载，默认关闭 |

---

## 七、验证配置是否生效

```bash
# 1. 进程与配置加载（不需鉴权）
curl -s http://<你的地址>:8012/api/health/live

# 2. 数据库状态与迁移是否完成（需鉴权）
curl -s http://<你的地址>:8012/api/health/check | python3 -m json.tool
#    关注 "status": "ok" 与 "migration_required": false

# 3. 外部依赖连通性（需鉴权）
curl -s http://<你的地址>:8012/api/health/deps | python3 -m json.tool

# 4. 媒体栈自检（接了 amane/Jellyfin/qB 才用）
curl -s http://<你的地址>:8012/api/amane/selfcheck | python3 -m json.tool
#    逐项看 checks[].status 是否全为 ok

# 5. 当前生效的系统配置（需鉴权；密钥字段是掩码，不会回显明文）
curl -s http://<你的地址>:8012/api/system/config | python3 -m json.tool
```

> ⚠️ **HTTP 200 不等于配置正确**。`/api/health/live` 只证明进程活着。
> 判断配置是否真的生效，必须看后面几个接口返回体里的业务字段。

---

## 八、常见配置错误

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 日志刷 `Automatic schema migration is disabled` | 建表开关没开 | `.env` 设 `BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION=true` 后重建容器 |
| 接口报表不存在 | 同上，表没建 | 同上 |
| 容器反复重启 | 数据库连不上 | 看日志里 `role does not exist` / `could not connect` |
| `role does not exist` | 用户名/库名没都叫 `busterminal` | 两者改成一致，或改 `DATABASE_URL` |
| 改了 `DATABASE_URL` 没反应 | 该项只在启动时读取 | `docker compose up -d` 重建容器 |
| 设置页填了地址但系统还用旧值 | 环境变量与设置页冲突 | 设置页的值优先；确认保存成功后刷新页面 |
| 密钥填了却提示未配置 | 该字段留空表示"保持原值"，没有原值就等于没配 | 重新填写并保存 |
| 内网服务连不上但代理正常 | 内网请求走了代理 | 确认 `BUS_TERMINAL_INTERNAL_PROXY_ENABLED=false` |
| 数据库卡死、容器无响应 | 库放在 SSD/NVMe 缓存盘 | 迁到机械盘 |
| 缓存目录变大 | 正常行为，达上限 90% 会自动整理到 80% | 用 `CACHE_SIZE_LIMIT_GB` 或设置页调整上限 |
