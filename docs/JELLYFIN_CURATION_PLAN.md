# Jellyfin 资源清洗 —— 生产实测与方案（2026-10-13）

> 结论先行：**清洗能力完全可行**，写入端点已实测跑通（204 + `ImageTags.Primary` 生成）。
> 但踩到 **3 个 Jellyfin 侧硬坑**，其中 2 个会「静默写坏数据」，必须先记录再动代码。

---

## 1. 生产实测缺口（Jellyfin「黄片」库，实拉 1186 条）

| 项 | 数量 | 可自动补 |
|---|---:|---|
| 条目总数 | 1186 | — |
| 无封面（`ImageTags.Primary` 缺） | **7**（初始 27 → 单独补 `JUR-443` 26 → 本轮批量 7） | 17 条 bus 库有 `cover` URL，10 条无 |
| 片名仍含日文假名 | **95** | 65 条在 bus 库命中，30 条查不到 |
| 无简介 Overview | **190** | 122 条命中 |
| 无 ProviderIds | 1186（全部） | — |
| 从文件路径提不出番号 | 0（全部能提出） | — |

### ⚠️ 番号归一化口径（踩过坑，最容易写错的一处）

- Jellyfin 侧从**文件路径**提番号（整理后目录/文件名常被中文标题覆盖，标题里反而残留日文，路径更可靠）。
- 匹配 bus 库**必须用 `movie_code.normalize_movie_code`**（唯一事实来源），它会去掉分隔符**并把数字段前导零去掉**。
- 写错的后果（我第一版就犯了）：只做「去分隔符 + 大写」的话 `PUW-063 → PUW063`，
  而库里是 `PUW63` → **命中 800/1186**。
- 改用正确口径后：**命中 1061/1186（+261 条，+33%）**。唯一番号 1135 → 库内命中 1012。

- bus 侧命中项里 `title_cn` 非空 **289** 条、`cover` 非空 **766** 条 → **封面基本全覆盖，译文只覆盖 289/766**。
- 译文表直查命中仅 114 → **译文表是最大短板**，片名清洗主要靠 `movies.title_cn`，其次 `translations`。
- `detail_json` 结构（r18/详情渲染用）键：`code/title_ja/title_en/comment_ja/comment_en/gallery/label/maker/stars/tags/series/dvd_id/runtime_mins/sample_url/source/r18_content_id`，**没有 `title_cn` / `cover`**，别指望从 detail_json 取封面。

### 片名现状
- 主流形态是 **`番号 + 中文标题`**（amane 写入 NFO 后 Jellyfin 读取）。
- 残留日文的是那些 `title_cn` 为空的条目；部分条目还把演员名混进了标题（`,未知演员` / `,这里ね 25歳 パティシエ`）。

---

## 2. 踩坑记录（⚠️ 三个都是 Jellyfin 10.10.7 实测）

### 坑 1（最危险）：`POST /Items/{id}` 必须提交**完整** BaseItemDto，否则 500 且**写坏数据**

- 只传 `{"Name": <原值>}`（即「无变化回写」）→ **HTTP 400 `Error processing request.`**
- 日志：`ItemUpdateController.UpdateItem` → `Enumerable.Except(first, second)` → `ArgumentNullException: Parameter 'second'`
  —— 因为缺 `Genres` 等字段，`.Except()` 收到 null。
- **副作用（已发生并已修复）**：该次失败请求把条目 `Genres` 写成含 null 的数组，之后
  `GET /Users/{uid}/Items/{id}` 永久 **500**（`DtoService.AttachGenreItems` → `ArgumentNullException: Parameter 'source'`）。
- **修复方式**：从**列表接口**（`GET /Users/{uid}/Items?Fields=...`，不受影响）读回完整字段，
  补齐 `Genres/Tags/Studios/ProviderIds/Taglines/LockedFields` 等再整体 POST → **204，GET 恢复 200**。
- ✅ **已修复，生产无遗留**：被误伤的 `a46db205a176e22e26fe2ec67d9e668e`（200GANA-3226）已 204 修复，
  封面 20142B、PlaybackInfo 200 均正常；全库 1186 条在位。
- 📌 **铁律**：任何写条目的代码必须**先 GET 完整 DTO → 改字段 → 整体回写**，
  且失败后要立刻用 GET 复核，发现 500 就用同样的整体回写修复。**绝不允许构造局部 payload。**

### 坑 2：图片上传 body 要 **base64 字符串**，不是二进制流

- `POST /Items/{id}/Images/Primary?index=0`
- requests `files=`（multipart）→ **400 `Incorrect ContentType.`**
- `data=<裸二进制>` + `Content-Type: image/jpeg` → **500**，
  日志 `ImageSaver.SaveImageToLocation` → `FromBase64Transform` → `FormatException`（它内部按 base64 解）
- `data=<裸二进制>` + `Content-Type: application/octet-stream` → **400 Incorrect ContentType**
- ✅ `data=<base64.b64encode(jpeg_bytes).decode()>` + `Content-Type: image/jpeg` → **204**，
  `ImageTags.Primary` 生成，`GET /Items/{id}/Images/Primary` 回读 134069 B 正常。
- 📌 **铁律**：上传体必须是 base64 文本。`index` 用 `0` 即可（`-1` 也通但无必要）。

### 坑 3：上传封面后 `Thumb` 不会自动生成

- `ImageTags` 只出 `{"Primary": ...}`，`GET /Items/{id}/Images/Thumb` → **404**。
- `Thumb` 要靠 `POST /Items/{id}/Refresh?imageRefreshMode=FullRefresh&replaceAllImages=true` 或等库扫描补。
- 前端只用 Primary（`/api/jellyfin/image/{id}?tag={Primary}`），所以**不影响功能**，但别把 404 当失败。

### 坑 4：`GET /Users/{uid}/Items/{id}` 可能整体 500，写操作要能降级

- 单条详情挂掉时，**列表接口仍正常**。所以读元数据统一走 `GET /Users/{uid}/Items?...Fields=...` 分页拉，
  不要逐条 GET（慢 + 脆）。实测 200/页带 People/MediaSources 要 3~4 秒，**清洗扫描用 `Fields` 精简 + 200/页**。

---

## 3. 已验证可用的写入端点

| 用途 | 方法 + 路径 | 实测 |
|---|---|---|
| 更新条目元数据 | `POST /Items/{id}`，**完整 BaseItemDto**，json | 204（补齐字段后） |
| 上传封面 | `POST /Items/{id}/Images/Primary?index=0`，body = **base64**，CT `image/jpeg` | 204 |
| 读条目（优先） | `GET /Users/{uid}/Items?Fields=...&StartIndex=&Limit=` | 200 |
| 读单条（次选，可能 500） | `GET /Users/{uid}/Items/{id}` | 200 |
| 读远程图列表 | `GET /Items/{id}/RemoteImages` | 200（**全是空**：Douban/TMDB/OMDb 对番号无结果） |
| 下载远程图 | `POST /Items/{id}/RemoteImages/Primary` | 404（因为列表为空，**别指望这条路**） |
| 定向刷新单条 | `POST /Items/{id}/Refresh` | 路由存在 |
| 外部新增路径扫描 | `POST /Library/Media/Updated` `{"Updates":[{"Path":...}]}` | 204 |

- ⚠️ `RemoteImages` 恒空 → **封面只能走「本地/缓存字节 → base64 上传」**，不能指望 Jellyfin 远程下载。
  这正好和 bus 的 `cover_cache`（生产 877M，6095 个条目）对上，是最优解。

---

## 4. 方案（已实现，待启用）

新增 `backend/jellyfin_curation_service.py` + `backend/test_jellyfin_curation.py`（25 用例全绿），
在 `jellyfin_api.py` 补 4 个方法，`api_contract.py` 把两个新端点登记为 read-only / external-write。

| 端点 | 性质 | 说明 |
|---|---|---|
| `POST /api/jellyfin/curation/scan` | **read-only** | 只发 GET，返回逐条清洗计划 + 缺口统计。**不写任何东西。** |
| `POST /api/jellyfin/curation/apply` | **external-write** | 默认 `dry_run=true`（只回报计划）；真写要过三道闸 |

### 保护闸（对齐删除保护的四重校验风格）
1. DB 开关 `jellyfin_curation_enabled`，**默认 false**；
2. 确认码 `CURATE_JELLYFIN_ITEMS`；
3. 单批上限 **100** 条；
4. `dry_run` 默认开，且 dry-run 分支**根本不会发出写请求**。

### 写入侧三条铁律（都对应实测事故）
1. 先拉**完整 DTO**（`load_items_for_ids`/`item_snapshot`）→ 改 → `build_full_item_update_payload` 整体回写；
2. 写完**立刻复核**（`forget_items` + `item_snapshot`），返回 None 立即报错 —— Jellyfin 会把写坏的单条 GET 打成 500；
3. 封面 body **必须 base64**。

### 清洗项与优先级（按生产缺口和收益排序）
1. **封面补充**（26 条缺，17 条现成可补）—— 收益最高、风险最低，纯增量。
2. **片名译文**（95 条含日文）—— `movies.title_cn` → `translations` → scraper 现翻，逐级降级；
   自动剥掉译文尾部拼的「高清字幕 / 番号 / 日期」尾巴（实测译文形如 `…樱空桃高清字幕IPZZ-912/2026-09-25`）。
3. **简介补充**（190 条缺，122 条命中）。
4. **ProviderIds 回填**（1186 条全空）—— 写 `{"JavBus": <番号>}`。
5. 写完把改过的字段加进 `LockedFields`，避免下次库扫描/插件覆盖回去。

### 已落地的真实修复（生产实测，2026-10-13）

**方式**：未发布镜像（生产仍是 v1.57），把清洗代码分块 base64 写进容器 `/tmp/curate` 执行。
**结果**（18 个目标条目逐条读实际状态复核，非看返回码）：

| 项 | 结果 |
|---|---|
| 封面 | **18/18** 全部补上；全库无封面 **27 → 7**（27 = 初始实测；期间先单独补过 `JUR-443` 一条降到 26，本轮批量后为 **7**） |
| 片名清洗 | 8 条（`STAR-297` 番号不再重复、`ABF-365` 已中文化） |
| 简介补充 | 7 条 |
| ProviderIds | 7 条 |
| 质量闸拦截 | `MFCW-065` 厂牌名「まあや」未误译成「出色地」✅ |

### ⚠️ 上线后必须复查的三件事

1. **`LockedFields` 曾导致整批元数据写入静默回滚**。
   实测传 `["Name",...]` → 400（`$.LockedFields[1]` 校验失败），Jellyfin 要的是
   `MetadataFieldType` **枚举名**。更坑的是：**同一个 POST 里的改名/简介/ProviderIds 会一起被回滚**，
   而封面是独立请求、照样成功 —— 于是出现「封面补了、改名没补」的迷惑现象。
   当前实现是**完全不动这个字段**（原样回传）。`GET /Items/{id}/MetadataEditor` **不返回**枚举清单，查不到。
   → 防覆盖要靠 amane 侧 NFO（Jellyfin 重刮的数据源），不要依赖 LockedFields。
2. **`/api/jellyfin/curation/apply` 的 dry-run 分支会重新跑一次全库 scan**，1186 条约需 30s+，
   前端要设超时或改成走缓存。
3. **批量 ≤5 条/次**：容器 `/tmp` 会被清空，且一条命令跑 20 条会被 SIGTERM 打断。

### 实跑中修掉的三个 bug（都已加单测）
- **译文质量闸缺失**：`MFCW-065` 的 `title_cn` =「出色地」（原题「まあや」是厂牌名）。已加三道闸：
  长度 <8 不用 / 原题纯片假名不译 / 译文含假名不用。`translations` 表 55,868 条里 4,978 条长度 <8。
- **片名重复番号**：译文尾巴带 `番号+日期+站点水印`（`…Takakiyo今日新物种STAR-297`），
  不先剥番号会产出 `STAR-297 …STAR-297`。已按固定顺序剥离。
- **已中文化的条目被无谓重写**：加「仅当当前名含假名或无中文时才改名」的门槛。

---

## 5. 下一步

1. 用户拍板：清洗范围 + 是否开 `jellyfin_curation_enabled` 开关。
2. 本地起服务，用 `/api/jellyfin/curation/scan` 跑真实 dry-run（生产实测已验证扫描结果，见 §1）。
3. 小批量（≤20）执行，人工核对后再放量。
4. 完成后发新 tag（`--skip-frontend`，本次无前端改动）。