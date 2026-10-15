# BusTerminal 用户故事、内部数据流与服务分工

> 整理时间：2026-10-11。**本文替代 PRD §4「端到端核心业务闭环」作为当前视角**——
> PRD 是"单应用 10 模块"时代的产物，现在实际是 **7 个服务组成的分布式栈**。
> 全部数字来自生产环境实测（NAS 192.168.1.100），非估算。

---

## 0. 一句话记住分工

> **BusTerminal 决定「要不要、按什么番号」；qBittorrent 决定「怎么下」；amane 决定「怎么落盘」；Jellyfin 决定「怎么给用户看」。**

配套一句数据归属：

> **BusTerminal 持有"知识与决策"（145 万番号、订阅、队列）；amane 持有"文件与元数据实体"；Jellyfin 持有"用户的观看行为"。**

---

## 一、先建立心智模型：四个角色

| 角色 | 服务 | 类比 |
|---|---|---|
| **大脑** | bus_terminal | 图书管理员：知道有什么书、该不该进货、进哪本 |
| **仓库** | bus_postgres | 大脑的记忆（唯一的业务数据库） |
| **搬运工** | qBittorrent | 只负责把货拉回来，不拆箱、不上架 |
| **上架员 + 档案员** | amane | 唯一的"碰文件的人"：拆箱、改名、写说明卡（NFO）、贴封面 |
| **阅览室** | Jellyfin | 把上架好的书摆出来给读者看，并记录谁看了什么 |
| **两个旁路** | bus_backup / pt_ignore_guard | 定时备份；把"还没下完的"从阅览室藏起来 |

**最容易搞错的三条边界**（违反会出事）：

1. **只有 amane 能碰媒体文件**（移动/改名/写 NFO/拷封面）。BusTerminal 的整理器锁了 `allow_file_writes=False` —— 两边都写会互相覆盖。
2. **只有 BusTerminal 有决策权**（要不要下、下哪个版本、删不删）。
3. **Jellyfin 的观看记录是唯一真值**，BusTerminal 只是抄一份。

---

## 二、七个用户故事

### US-1｜"我想看看今天有什么新片"

| | |
|---|---|
| **用户做什么** | 打开首页/发现页，翻榜单 |
| **系统内部** | BusTerminal 抓 4 个站的榜单（jable / javbus / javdb / airav）→ 结果落缓存 → 标题送进翻译队列 → 页面显示中文标题 |
| **谁负责** | **bus_terminal**（抓取 + 翻译 + 缓存） |
| **别人做什么** | qB / amane / Jellyfin 全程不参与 |
| **当前状态** | ✅ 在跑。翻译积压 103 万条（详见 `docs/AMANE_STATION_STRATEGY.md` 同期的翻译普查） |

### US-2｜"我搜一个番号，想看它的资料和演员"

| | |
|---|---|
| **用户做什么** | 搜索框输番号 → 进详情页 |
| **系统内部** | 查自己的库（145 万条）→ 命中就展示标题/封面/演员/磁力列表 |
| **谁负责** | **bus_terminal** |
| **当前状态** | ✅ 搜索可用。⚠️ **但演员资料极薄**：10.2 万演员中，有头像 39%（今天刚补到 50%）、**有生日仅 193 条、有简介仅 4 条**（详见 `docs/DUPLICATE_AUDIT.md`） |

### US-3｜"我想下这部片，我不想等"

| | |
|---|---|
| **用户做什么** | 点一下"下载" |
| **系统内部** | ① 按分辨率偏好从多个磁力里选一个 → ② **三方查重**（本地库 / amane 是否已有 / Jellyfin 是否已有）→ ③ 磁盘空间闸门（低于 1190GB 就拒绝）→ ④ 写进下载队列 → ⑤ 5 秒内交给 qB |
| **谁负责** | **bus_terminal**（选磁力 + 查重 + 闸门 + 入队）、**qBittorrent**（真正下载） |
| **边界** | BusTerminal **从不解析种子内容、不碰下载中的文件** |
| **当前状态** | ✅ 在跑。队列里 309 条已提交 |

### US-4｜"下载完自动整理入库，我什么都不想管" ⚠️ **这条链路还没通**

这是**整套系统的核心承诺**，也是**目前最薄弱的一环**。

| | |
|---|---|
| **期望的流程** | qB 下完 → 自动搬到投递目录 → amane 扫描发现 → 按番号刮削元数据 → 整理改名成规范目录 → 写 NFO 与封面 → Jellyfin 扫到并可见 |
| **实际端到端走通** | **仅 5 部**（`movie_lifecycle` 里 `delivered + completed + visible` = 5） |
| **断在哪** | 投递成功 30 条，但 `delivered + failed` 有 30 条 —— 卡在 amane 的**整理（organize）**环节 |
| **根因（实测）** | amane 的 `REFRESH` 任务**失败率 61%**（FAILED 33 / DONE 21）。机制：`media_files.path` 有唯一约束，但写入是"裸 INSERT 且不捕获冲突" → 三种写入者（watchdog / 并发 refresh / 延迟登记）抢同一路径 → 一次冲突就让**整批任务判失败** → 后续的刮削任务也不生成 → 文件永久停在 `/hp/_incoming`，**且不会自愈**（下一轮认为"已在索引里"） |
| **副作用证据** | amane 的 `ORGANIZE` 任务**至今只成功过 6 次**；`media_files` 里还有 **140 条 PENDING**（登记了但没刮削） |
| **谁负责** | **bus_terminal**（编排投递顺序）+ **qBittorrent**（搬运目录）+ **amane**（扫描/刮削/整理）+ **Jellyfin**（呈现） |
| **顺序硬约束** | **必须先投递文件、再通知入库**（反了 amane 扫不到）；`refresh` 必须显式传 `scrape=[]`（否则一次调用把全库 pending 打进刮削队列） |

### US-5｜"我想在 Jellyfin 里看片"

| | |
|---|---|
| **用户做什么** | 打开 Jellyfin，或从 BusTerminal 的库页面点播 |
| **系统内部** | Jellyfin 读 amane 写好的 NFO 和成品文件 → 建条目 → 播放（转码/HLS）；BusTerminal 只做**代理转发**（不把 API Key 暴露给浏览器） |
| **谁负责** | **Jellyfin**（呈现 + 播放） |
| **边界** | 播放/收藏的**权威数据在 Jellyfin**，BusTerminal 只读 |
| **当前状态** | ✅ 播放链路正常；能播多少取决于 US-4 入库了多少 |

### US-6｜"我关注一个演员，她出新片自动下"

| | |
|---|---|
| **用户做什么** | 在演员页点"订阅" |
| **系统内部** | 写订阅记录 → 定时巡检 → 命中新番号 → 走 US-3 的下载决策 |
| **谁负责** | **bus_terminal**（订阅 + 巡检 + 决策） |
| **当前状态** | ✅ 225 条订阅在跑。⚠️ 匹配质量受"演员档案薄"拖累（同名演员多，参 `docs/DUPLICATE_AUDIT.md`） |

### US-7｜"我看完了，帮我清掉"

| | |
|---|---|
| **用户做什么** | 什么都不做（期待系统自动建议清理） |
| **系统内部** | Jellyfin 播放 → webhook 回传 → BusTerminal 记"已看" → 生成**清理候选清单（只登记不删）** → 人工确认后才执行三联删除（Jellyfin 条目 + 文件 + 种子） |
| **谁负责** | **Jellyfin**（行为源）+ **bus_terminal**（决策）+ **amane / qBittorrent / Jellyfin**（各自执行自己那份删除） |
| **当前状态** | ⚠️ 清理候选 0 条；**删除保护默认关闭**（这是刻意的，不是 bug）。另外 Jellyfin 的 webhook 回传地址疑似还是旧主机，**被动通道可能静默失效** |

---

## 三、内部数据流（端到端）

### 3.1 主链路（用户点下载 → 能在 Jellyfin 看）

| # | 谁 | 做什么 | 数据落到哪 |
|---|---|---|---|
| 0 | bus_terminal | 发现/搜索 → 查重（本地 + amane + Jellyfin） | `movies` / `cache` |
| 1 | bus_terminal | 选磁力 → 磁盘闸门 → 写队列 | `download_queue`（status=pending） |
| 2 | bus_terminal → qB | 5 秒轮询取任务 → 提交磁力（打 `jav` 标签） | qB 内部状态 |
| 3 | qB + pt_ignore_guard | 下载中产生占位文件；守卫给"只有未完成标记"的目录放 `.ignore`，让 Jellyfin 先别扫到空壳 | 文件系统 |
| 4 | bus_terminal（60 秒） | 巡检下载状态：设做种时限 → 过滤广告小文件 → 判完成 → 记录 | `downloads` / `movie_lifecycle` |
| 5 | bus_terminal → qB | 达成投递条件 → **先暂停** → 移动目录到 `/hp/_incoming`（同盘秒移） | 文件系统 |
| 6 | bus_terminal → amane | 提交三条任务：`refresh`（只登记）→ `scrape`（刮元数据）→ `organize`（整理改名+写 NFO） | amane 的 `media_files` / `metadata` |
| 7 | bus_terminal → Jellyfin | 触发扫库 → Jellyfin 读 NFO 建条目 | Jellyfin 库 |
| 8 | bus_terminal → 前端 | 展示库页面；封面走代理 + 本地缓存 | `cover_cache` |
| 9 | Jellyfin → bus_terminal | 播放/收藏事件回传（webhook 或主动拉） | `movie_lifecycle` |
| 10 | bus_terminal | 生成清理候选（只登记不删） | `cleanup_candidates` |

### 3.2 数据流向一句话版

```
前端 ──▶ BusTerminal ──┬─▶ bus_postgres（唯一业务库：145 万番号）
                       ├─▶ qBittorrent（下磁力）──▶ 共享卷
                       ├─▶ amane（扫描/刮削/整理）──▶ 共享卷 + amane.db
                       ├─▶ Jellyfin（读库/播放/删除）
                       └─▶ 外部站点（javbus / javdb / jable / airav、gfriends 头像库）
共享卷 ──▶ Jellyfin 扫库可见
Jellyfin ──▶ BusTerminal（观看事件回传）
bus_backup ──▶ bus_postgres（每日 04:40 pg_dump）
pt_ignore_guard ──▶ 共享卷（放/摘 .ignore）
```

---

## 四、服务职责矩阵（按业务场景）

> 读法：✅ = 这件事归它；"—" = 明确不归它。

| 业务场景 | bus_terminal | bus_postgres | qBittorrent | amane | Jellyfin | 旁路 |
|---|---|---|---|---|---|---|
| 发现新片（榜单/搜索） | ✅ | 存 | — | — | — | — |
| 标题翻译 | ✅ | 存 | — | （自己另有一套，翻译元数据字段） | — | — |
| 番号识别 | ✅ | — | — | （文件路径相位推导） | — | — |
| 选磁力 / 版本仲裁 | ✅ | — | — | — | — | — |
| 下载执行 | — | — | ✅ | — | — | — |
| 种子文件过滤 | ✅ 决策 | — | ✅ 执行 | — | — | — |
| **文件移动/改名** | — | — | 只搬目录 | ✅ **独占** | — | — |
| **写 NFO / 封面** | — | — | — | ✅ **独占** | — | — |
| **元数据刮削** | 自己一套（单薄） | 存 | — | ✅（24 源聚合，但只按番号查） | （有插件但刻意不用） | — |
| 媒体库呈现/播放 | 只代理 | — | — | — | ✅ | — |
| 观看行为记录 | 抄一份 | 存 | — | — | ✅ **权威** | — |
| 删除决策 | ✅ | — | — | — | — | — |
| 删除执行 | — | — | 种子 | 文件 | 条目 | pt_ignore_guard 只放 `.ignore` |
| 封面缓存（给前端） | ✅ | — | — | （自己的 resources 库，互不相通） | （自己的图片，互不相通） | — |
| 未完成文件屏蔽 | — | — | — | — | — | ✅ pt_ignore_guard |
| 数据库备份 | — | — | — | — | — | ✅ bus_backup |

**⚠️ 三套互不相通的图片资产**（别搞混）：BusTerminal 的 `cover_cache`（给前端）、amane 的 `resources`（给 NFO）、Jellyfin 的内部图片。

---

## 五、数据归属：谁能写什么

| 数据 | 存在哪 | 谁可以写 | 谁只读 |
|---|---|---|---|
| 番号/影片知识（145 万） | bus_postgres `movies` | bus_terminal | 全部其他 |
| 订阅、下载队列、生命周期 | bus_postgres | bus_terminal | — |
| 文件索引 + 元数据实体 | amane.db（SQLite） | amane | bus_terminal（**只经 API，绝不直连库**） |
| 媒体文件本身 | 共享卷（NAS） | **仅 amane**（+ qB 的下载/搬运） | Jellyfin 读 |
| 观看/收藏 | Jellyfin | Jellyfin | bus_terminal 读 |
| 封面缓存 | `data/cover_cache` | bus_terminal | — |

**铁律**：段与段之间只有两种通信——**任务 API + 事件**。禁止跨服务直连数据库，禁止 import 对方的内部逻辑。

---

## 六、当前真实完成度（2026-10-11 实测）

| 用户故事 | 状态 | 证据 |
|---|---|---|
| US-1 发现新片 | ✅ 通 | 4 源榜单在跑 |
| US-2 搜索看详情 | ✅ 通 | 145 万番号可搜 |
| US-3 一键下载 | ✅ 通 | 队列 309 条已提交 |
| **US-4 自动整理入库** | ⚠️ **断** | **端到端仅 5 部**；amane REFRESH 失败率 61%；ORGANIZE 累计仅成功 6 次 |
| US-5 浏览播放 | ✅ 通 | 受 US-4 影响（能播的不多） |
| US-6 演员订阅 | ✅ 通 | 225 条订阅 |
| US-7 自动清理 | ⚠️ 未启用 | 候选 0 条，删除保护默认关 |

**一句话总结现状**：**"发现 → 决策 → 下载"这一段很强，"落盘 → 上架"这一段基本没通。**
系统能帮你把片子下回来，但下回来之后**大部分卡在投递目录里**，没能自动整理进 Jellyfin。

**修 US-4 的关键动作**（按优先级）：
1. 修 amane 写入冲突（`media_files.path` 的裸 INSERT + 不捕获 `IntegrityError`）—— 这一处不改，每批新片都会重现
2. 把 `/hp/_incoming` 里已堆积的文件手动捞出来重新走一遍（不会自愈）
3. 补一个"投递后 N 小时仍未入库"的告警，否则问题永远静默

---

## 七、相关文档

| 文档 | 内容 |
|---|---|
| `docs/PRD.md` | 产品需求与功能边界 |
| `docs/SRS.md` | 软件需求规格 |
| `docs/UI_DESIGN_SPEC.md` | 界面设计规范 |
| `docs/CONFIGURATION.md` | 配置项逐项参考 |
| `docs/INSTALL.md` | 安装、部署与使用动线 |
| `docs/API.md` | 接口清单（由 OpenAPI 自动生成） |
