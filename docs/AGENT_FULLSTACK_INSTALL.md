# 让 AI Agent 帮你全栈部署 BusTerminal

把下面的提示词整段交给你的 agent（Claude Code / Cursor / Codex / 任何能操作终端的 agent）。
它会**先探查、再问你要装哪些、经你确认后才动手**，最后跑真实验证。

适用范围：BusTerminal + PostgreSQL + qBittorrent + amane + Jellyfin 这一整套。

> 已经有部分服务（哪怕在**别的机器**上）也照样能用 ——
> agent 会先盘点本机现状，再让你逐项选择「新建 / 复用 / 接入 / 不需要」。
>
> 只想装 BusTerminal 本体、不接任何外部服务？用
> [docs/AGENT_INSTALL_PROMPT.md](AGENT_INSTALL_PROMPT.md) 里那份更轻量的提示词。

---

## 复制以下全部内容给 agent

````text
请帮我部署 BusTerminal 全栈（媒体库自动化：内容发现 → 下载 → 刮削入库 → 媒体库播放）。

项目仓库：https://github.com/seldoms/BusTerminal
部署文件：docker-compose.fullstack.yml + .env.fullstack.example（仓库根目录）

这是**先探查、后确认、再执行**的流程。三个硬性要求：
  A. 没经我确认，不许执行任何安装/启动/写入动作；
  B. 我已有的服务不许动、不许重装、不许改它的配置；
  C. 报告里每个结论都要有命令输出支撑，拿不到就写「拿不到」，不许编。

═══════════════════════════════════════════════════════════════
阶段 1 · 环境探查（全部只读，一个字节都不要改）
═══════════════════════════════════════════════════════════════

1) 先把项目拉下来（只拉取，不部署任何东西）：
     git clone https://github.com/seldoms/BusTerminal.git
     cd BusTerminal
   然后把 docs/INSTALL.md 和 docs/CONFIGURATION.md 读一遍，了解全貌。

2) Docker 环境：
     docker --version && docker compose version
   需要 Docker 20.10+ 且带 compose 子命令；不支持就停下来告诉我。

3) **盘点本机已有的容器**（这一步最关键，决定后面跳过什么）：
     docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
   逐个判断哪些是这一栈的组件，按镜像名和端口识别：
     PostgreSQL   镜像含 postgres / pgvector，端口 5432
     qBittorrent  镜像含 qbittorrent，WebUI 端口常见 8080 / 8086
     amane        镜像含 amane，端口 8000
     Jellyfin     镜像含 jellyfin，端口 8096
   对每个疑似目标，额外确认一下它是不是真的要复用：
     docker inspect <容器名> --format '{{.Config.Image}} | {{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'

4) 端口占用（用来给每个服务挑一个真正空闲的端口）：
     ss -ltn  ||  netstat -ltn          # Linux
     lsof -iTCP -sTCP:LISTEN -P -n      # macOS
   把 8012 / 8086 / 6881 / 8013 / 8096 这几个候选端口逐个查一遍，
   被占用就往上找一个空闲的。

5) 磁盘与内存：
     df -h
     free -h  ||  vm_stat
   重点找**容量足够的机械盘/大容量卷**，记下可用的挂载点和剩余空间。
   媒体库会持续增长，不要把下载区和媒体库放在系统盘。

6) 目录现状（只列，不要创建也不要删）：
   - 候选安装目录是否已存在、里面有没有东西
   - 检查是否有**同名容器**（bus_terminal / bus_postgres / qbittorrent / amane / jellyfin）
     在跑：有的话**不要覆盖**，告诉我

7) 其它机器上的服务（如果我说过某组件在别的机器上）：
     nc -z -w3 <地址> <端口>      # 或 curl -sS -m3 -o /dev/null -w '%{http_code}' http://<地址>:<端口>
   确认从**本机**能不能连上。连不上就得先解决网络，别急着装。

═══════════════════════════════════════════════════════════════
阶段 2 · 把清单交给我，由我决定装哪些（不要自己拍板）
═══════════════════════════════════════════════════════════════

探查完，把结果整理成下面这种表格发给我，**然后停下来等我回答**：

【服务清单】
| # | 组件 | 本机现状 | 建议 | 需要我选 |
|---|------|---------|------|---------|
| 1 | BusTerminal | 未安装 | 新建 | —（本来就是装它） |
| 2 | PostgreSQL | 未发现 | 新建 | A. 新建自带库  B. 用 SQLite（更省事）  C. 接我已有的（我给连接串） |
| 3 | qBittorrent | 发现容器 xxx 在跑 | 复用 | A. 复用现有的  B. 另装一个  C. 接别的机器 |
| 4 | amane | 未发现 | 新建 | A. 新建  B. 接别的机器（给地址+Token） |
| 5 | Jellyfin | 192.168.1.50:8096 可连 | 接入 | A. 接入它  B. 本机另装一个 |

【路径方案】（给出你推荐的具体路径 + 理由）
| 用途 | 你推荐的路径 | 所在磁盘/剩余空间 | 说明 |
|------|------------|-----------------|------|
| 媒体总根（下载区 + 媒体库） | | | 会挂给 qB / amane / Jellyfin |
| BusTerminal 数据（封面缓存等） | | | 建议机械盘，容量要够 |
| 数据库数据目录 | | | 必须有，且放机械盘 |

【端口方案】
| 服务 | 你推荐的宿主机端口 | 该端口现在是否空闲 | 依据 |
|------|-----------------|-----------------|------|

如果发现我列的服务**不在同一台机器**上，额外提醒我一次：
「你选择了跨机器部署。下载区/媒体库必须是这几台机器都能访问的同一份目录
（NFS / SMB / 群晖共享文件夹均可）。如果做不到共享，
qBittorrent 和 amane 必须放在同一台机器上，否则会出现『下载完成了但永远不入库』。」

═══════════════════════════════════════════════════════════════
阶段 3 · 生成方案并再次确认
═══════════════════════════════════════════════════════════════

等我回答之后，把最终方案写成一份「安装确认单」给我，包含：
  - 将要新建哪几个容器（明确列出，没被选中的一个都不要起）
  - 每个容器的端口映射
  - 每个容器的目录映射（宿主路径 → 容器内路径）
  - 数据库：自建 / SQLite / 外部（外部的话是哪个地址）
  - 预计占用的磁盘和端口
  - 你要执行的完整命令

**我回复「确认」之后才能开始阶段 4。**

═══════════════════════════════════════════════════════════════
阶段 4 · 执行安装（只装我选定的）
═══════════════════════════════════════════════════════════════

1) 生成配置：
     cp .env.fullstack.example .env
   按确认单改这些项：
     COMPOSE_PROFILES   ← ★ 只写要**新建**的服务（db / qb / amane / jellyfin，逗号分隔）
     MEDIA_ROOT / BUS_DATA_DIR / BUS_PG_DATA_DIR / 各配置目录 ← 换成确认过的路径
     BUS_TERMINAL_PORT / QB_WEBUI_PORT / QB_TORRENT_PORT / AMANE_PORT / JELLYFIN_PORT
     PUID / PGID        ← 用 `id -u` / `id -g` 的真实值
     BUS_PG_PASSWORD + DATABASE_URL 里的口令 ← 用 `openssl rand -base64 24` 生成，两处要一致
     下游服务地址（QB_HOST / AMANE_BASE_URL / JELLYFIN_HOST）← 复用的填它的真实地址
   把 .env 里的口令记为敏感信息：不要打印到聊天里，也不要提交进 git。

2) 建目录并启动（只启动选定的服务，profile 已在 .env 里）：
     mkdir -p <确认过的各个路径>
     docker compose -f docker-compose.fullstack.yml up -d
   如果 Docker Compose 低于 v2.24 而你看到 depends_on / required 相关报错，
   改成先起数据库、等它 healthy，再起其余服务。

3) 等 60 秒让各容器初始化完成。

═══════════════════════════════════════════════════════════════
阶段 5 · 验证（每条都要跑，把真实输出贴给我）
═══════════════════════════════════════════════════════════════

  docker compose -f docker-compose.fullstack.yml ps
      → 每个容器的 STATUS 要是 healthy（不是 running）

  docker compose -f docker-compose.fullstack.yml logs --tail=80 busterminal
      → 不能有 ERROR / traceback

  curl -s http://127.0.0.1:<BUS_TERMINAL_PORT>/api/health/check
      → status=ok，且 migration_required=false

  curl -s http://127.0.0.1:<BUS_TERMINAL_PORT>/api/onboarding/status
      → 引导未完成（会返回 401 或 completed=false，都属正常）

再对**本栈新建**的每个服务单独验证连通性：
  qBittorrent  → curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<QB_WEBUI_PORT>
  amane        → curl -s http://127.0.0.1:<AMANE_PORT>/api/health
  Jellyfin     → curl -s http://127.0.0.1:<JELLYFIN_PORT>/System/Info/Public

判定标准（不要放宽）：
  - 容器必须 healthy。一直 running 不 healthy 的，去看它的 logs 找原因。
  - migration_required 必须是 false。为 true 说明建表没跑，
    检查 .env 里 BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION=true 是否生效，然后重建容器。
  - **只看到容器 healthy 不等于链路通。** 真正的链路验证在阶段 6。

═══════════════════════════════════════════════════════════════
阶段 6 · 报告 + 交接需要我手工完成的部分
═══════════════════════════════════════════════════════════════

报告包含：
  - 实际新建了哪些容器、跳过了哪些（以及跳过理由）
  - 每个服务的访问地址与端口
  - 媒体总根、数据目录、数据库目录的最终位置
  - 上面每条验证命令的真实输出
  - 未解决的问题（有就直说，不要用「全部正常」掩盖）

然后**逐条列出需要我自己动手的步骤**（这些你代替不了我）：
  1. 打开 http://<地址>:<BUS_TERMINAL_PORT> → 会自动进 /setup 向导，创建管理员账号
  2. 如果新建了 qBittorrent：从日志里取初始密码
       docker logs qbittorrent 2>&1 | grep -i "temporary password"
     登录后立刻改密码，并把默认保存路径设为 /downloads
  3. 如果新建了 amane：从日志里取 API Token
       docker logs amane 2>&1 | grep -i token
     用它登录 amane，在「管理 → 媒体库」里**添加一个库**，
     媒体目录填 /media，自动化级别选「监控+刮削」
  4. 如果新建了 Jellyfin：打开 http://<地址>:<JELLYFIN_PORT> 走完初始向导，
     然后在「控制台 → 媒体库」**新建一个专用库**：
       内容类型必须是「电影」
       路径填 /media
       ⚠️ 不要把它混进你已有的分类库 —— 本项目只处理「电影」类型，
          入库时会按 工作室/番号 重排目录，混进已有分类会打乱你原有组织
  5. 回到 BusTerminal 的 /setup 向导，把 qB / amane / Jellyfin 的地址与密钥填进去
  6. 完成后跑一次链路自检，把结果给我：
       curl -s http://127.0.0.1:<BUS_TERMINAL_PORT>/api/amane/selfcheck

═══════════════════════════════════════════════════════════════
红线（任何阶段都适用）
═══════════════════════════════════════════════════════════════

1. **不许删除或覆盖任何已有数据。** 媒体目录、下载目录、数据库卷一律只读对待，
   绝不 rm -rf。检测到目标目录已有非空数据 → 停下来问我。
2. **不许动我已有的服务。** 只做复用与接入，不重装、不改它的配置、不 restart 它。
3. **不许关闭鉴权。** REQUIRE_AUTH 保持 true。
4. **不许自作主张扩大安装范围。** 我没选的服务一个都不要起。
5. **不许把容器 running 当成部署成功。** 必须跑完阶段 5 和阶段 6 的验证。
6. 改任何配置文件前先备份。
7. 口令、Token、API Key 不要打印到聊天记录里，也不要以明文提交进 git。

如果中途失败，按这个顺序排查并把每步结果告诉我：
  1. 出错容器的 logs 里第一条 ERROR 是什么（不要跳过日志直接猜）
  2. 数据库连不上 → DATABASE_URL 里的用户名/库名是否都是 busterminal，两者不一致会报 role does not exist
  3. 端口冲突 → 换一个空闲端口后重新 up
  4. 目录权限 → 数据目录当前用户（PUID/PGID）是否可写
  5. 以上都不是 → 把完整 logs 给我，一起看

常见问题对照表见 docs/INSTALL.md 的「常见故障」。
````

---

## 三种常见起手式

### 全新机器，想一把装齐

提前告诉 agent：

> 这台机器是全新的，什么服务都没有。除了 PostgreSQL 我倾向用自带的，
> 其余（qBittorrent / amane / Jellyfin）都新建。媒体盘挂载在 /mnt/media，
> 空间够大，就用它。

### 已经有 Jellyfin，只想补下载和刮削

> 我本机的 Jellyfin 已经在 8096 跑着了，那个不要动。
> 我需要你新建 BusTerminal + PostgreSQL + qBittorrent + amane，
> 并且把 BusTerminal 接到我现有的 Jellyfin 上（API Key 我一会儿给你）。
> 注意我现有 Jellyfin 的媒体库路径是 /volume1/media，你必须让 amane
> 整理出来的文件落到那个目录下，否则 Jellyfin 扫不到。

### 服务分散在多台机器

> 我的 Jellyfin 在 192.168.1.50，qBittorrent 在 192.168.1.60，
> BusTerminal 装在这台 192.168.1.70 上。
> 请先确认这台机器能不能连到那两个地址，并且确认三台机器都能访问到
> 同一份存储（我有群晖共享 //192.168.1.10/media）。
> 如果共享做不到，直接告诉我需要改哪台，别硬装。

---

## 一句话提醒

如果 agent 说「部署完成，一切正常」，但没有贴出阶段 5 和阶段 6 里那些命令的真实输出，
**那不算验证**。本文档把「先探查 → 交你选 → 再执行」写成了硬流程，
就是为了避免 agent 自作主张在你机器上装一堆你没要的东西。
