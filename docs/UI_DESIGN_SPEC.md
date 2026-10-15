# BusTerminal 前端 UI/UX 设计风格与排版约束规范 (UI Design Spec)

> **文档版本**：v1.1  
> **更新时间**：2026-07-30  
> **状态**：正式生效  
> **目标**：规范界面美学、收拢字号阶梯、优化导航与区域排版一致性；v1.1 补充交互与移动端强制规范。

---

## 1. 现状问题诊断与分析

经过对项目中全部 CSS 及组件代码的扫描：
1. **字体种类 (Font Families)**：控制良好，使用了 1 种主 Sans-Serif 系统字体 + 1 种 Monospace 等宽字体。
2. **字号滥用现象 (Font Size Pollution)**：项目中散落使用了多达 **14 种不同的字号**（`10px`, `11px`, `12px`, `13px`, `14px`, `15px`, `16px`, `18px`, `20px`, `22px`, `24px`, `28px`, `36px`, `48px`）。
3. **痛点**：缺乏区域收拢规范，导航条使用了 `15px` 但搭配灰暗色与 `14px` 小图标显得文字偏小偏挤；海报卡片出现了低于可读性下限的 `10px/11px` 微小字。

---

## 2. 整体视觉美学 (Aesthetics & Design Tokens)

### 2.1 配色系统 (Color System)
* **主题基调 (Cyber-Dark Mode)**：深色沉浸式影音中心风格。
* **背景阶梯**：
  * 主背景 `--bg-body`: `#0f1115`
  * 面板/卡片 `--surface`: `#171a21` / `#1b1f28`
  * 导航/浮层遮罩 `--bg-dark`: `#0b0d11`
* **品牌高亮色 (Amber Accent)**：
  * `--brand`: `#f5b841` (琥珀金)
  * `--brand-2`: `#ffcc66`
  * `--primary-gradient`: `linear-gradient(135deg, #f5b841, #ffcc66)`
* **文本对比度**：
  * 一级高亮文本 `--text-main`: `#f4f6fb`
  * 二级文本 `--text-soft`: `#d1d5db` (提高灰白对比度，避免偏暗)
  * 提示/辅助文本 `--text-muted`: `#7d8596`

---

## 3. 字体与字号收拢约束 (Typography System)

### 3.1 字体种类规则 (Font Family Limit)
* **主 UI 字体 (Main UI Font)**：`Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif`
* **等宽字体 (Code / Log / Hash Font)**：`"SFMono-Regular", Consolas, "Liberation Mono", monospace`

---

### 3.2 区域字号收拢与映射表 (Regional Font Size Mapping)

全站字号严禁随意使用任意像素值，**彻底废除低于 `12px` 的字号**，精简归一为 **5 种通用标准层级**，严格映射至不同区域：

| 页面区域 (Page Region) | 约束字号 (Size) | 推荐字重 (Weight) | 行业及区域排版约束规范 |
|---|---|---|---|
| **1. 顶栏导航区 (Navbar)** | **`16px`** (`1rem`) | `600` (Semi-Bold) | **解决导航字小**：Logo 标 `20px`，菜单链接统一提升至 `16px`，图标统一调至 `18px`，间距增加至 `12px 16px`，未选中项对比度升至 `#d1d5db`。 |
| **2. 页面主头 (Page Hero/Header)** | **`28px`** (`1.75rem`) | `700` (Bold) | 页面唯一 H1 大标题（如“内容发现”、“女优主页”），单个页面仅保留一个 H1。 |
| **3. 板块/栏目头 (Section Title)** | **`20px`** (`1.25rem`) | `600` (Semi-Bold) | 各栏目/模块区域标题 H2（如“热播推荐”、“离线任务”），下方间距固定为 `16px`。 |
| **4. 影视/海报卡片 (Movie Card)** | **`15px`** (标题)<br>**`13px`** (元数据) | `600` / `400` | 影视/女优卡片标题统一为 `15px` 中粗，番号/日期/演员元数据统一收拢为 `13px`。**严禁使用 10px/11px**。 |
| **5. 表格 / 列表 / 正文 (Body/List)** | **`14px`** (`0.875rem`) | `400` (Regular) | 设置项 Label、任务队列行、日志面板、搜索框、Modal 正文。 |
| **6. 角标 / 状态 Tag (Badge/Tag)** | **`12px`** (`0.75rem`) | `500` (Medium) | **全站字号下限**：高清/中字 Badge、已下载角标、状态 Tag。行高限制为 `1.2`。 |

---

## 4. 交互与微动效约束 (Micro-Interactions)

1. **海报卡片 (Card Hover)**：
   * 悬浮时组件向上浮动 `transform: translateY(-4px)`，动画持续时间 `200ms ease`。
   * 悬浮时带品牌色微光阴影：`box-shadow: 0 0 0 1px rgba(245, 184, 65, 0.2), 0 12px 28px rgba(0, 0, 0, 0.3)`。
2. **毛玻璃顶栏 (Sticky Glass Navbar)**：
   * 保持 `position: sticky; top: 0; z-index: 100;`
   * 背景为 `rgba(15, 17, 21, 0.85)` + `backdrop-filter: blur(16px)`。
3. **按钮与反馈 (Button States)**：
   * 主要按钮使用琥珀金渐变：`background: linear-gradient(135deg, #f5b841, #ffcc66); color: #000; font-weight: 600;`
   * 高风险删除按钮统一为警告红 (`#ef4444`) 并必须带有二次 Modal 确认。

---

## 5. 业务页面与设置中心分离交互规范 (Separation of Operations & Settings UX)

### 5.1 业务页面边界 (Operational Views)
* **包含页面**：发现页 (`/discovery`)、下载页 (`/downloads`)、媒体库页 (`/library`)、Jellyfin 控制中心 (`/jellyfin`)、清理中心 (`/cleanup`)、候选中心 (`/candidates`)、任务监控 (`/tasks`)。
* **交互约束**：
  * 页面**仅展示数据列表、操作按钮（如：一键下载、暂停/恢复、手动触发同步）及状态 Dashboard**。
  * **禁止包含**：IP/Port/Token/Password 输入框、全局参数 Toggle 开关、存储路径配置、删除阈值设置等表单。
  * **未配置提示引导**：当相关服务未配置时（例如 Jellyfin 未填 Token），业务页展示空态引导 Banner（`"Jellyfin 未连接，请前往 [系统设置] 配置"`），点击直达设置页。

### 5.2 全局设置中心 (`/settings`) 架构
* **统一入口**：全站所有配置项唯一入口，分类为 5 大卡片/Tab 组：
  1. 🌐 **网络与数据源设置**：站源选择、反爬代理、请求超时与域名轮询。
  2. ⚡ **qBittorrent 下载设置**：qB 地址、端口、账号密码、做种规则、默认保存路径、JAV 小文件自动过滤开关。
  3. 🎬 **Jellyfin 媒体库设置**：Jellyfin 服务 URL、API Key、Webhook Token、受控删除确认码门控。
  4. 🤖 **自动化与推荐配置**：自动化分级模式（Off/Suggest/Auto）、自动下载上限、自动清理阈值。
  5. 🧹 **磁盘与清理容限配置**：磁盘安全低水位阈值 (GB)、已看片源保留期规则、图片缓存上限。


---

## 6. 交互与移动端强制规范 (v1.1 新增，2026-07-30)

### 6.1 触屏可达性 (Touch-First)
* 任何核心操作（播放、下载、刷新、搜索）**禁止只绑定 hover 或右键菜单**：hover 只是桌面增强，触屏必须有等价入口。
* 海报卡片操作层：桌面 `:hover` 出现；触屏（`@media (hover: none)`）改为海报底部常驻半透明操作条。
* 触摸目标尺寸 ≥ 40px。

### 6.2 导航信息架构 (Navigation IA)
* 一级导航分两组：**内容浏览**（首页/发现/Jable热门/字幕/高清/女优/分类）与**管理**（订阅/下载/候选/任务/媒体库），组间分隔线区分。
* 任何已上线页面必须有导航入口；演示页、测试页不得注册路由。
* ≤640px 搜索入口以图标按钮 + 下拉搜索条提供，禁止直接隐藏搜索能力。

### 6.3 列表缓存与滚动恢复 (List Caching)
* 长列表页（海报墙类）统一经 `MovieInfiniteList` + `cacheKey` 接入路由级缓存（`utils/listCache.js`，TTL 5 分钟、LRU 20 条）：详情返回列表零请求秒开并恢复滚动位置。
* 手动刷新走 `refreshTrigger`，强制跳过缓存。
* 路由切换默认回滚顶部（`App.jsx` ScrollToTopOnRouteChange），缓存命中时由列表恢复覆盖。

### 6.4 错误与状态反馈 (Error & State UX)
* 用户可见的错误文案**必须是中文**：后端错误码经 `utils/errorMessages.js` 映射，未知机器码统一兜底，禁止英文码、`[object Object]`、裸 500 文本直出。
* 空数据是空态不是故障：列表为空返回 200 + 空数组，页面渲染"暂无内容"；只有真实异常才是错误态。
* 外部服务（Jellyfin/qB）不可用必须显式提示"连接失败"（`degraded` 标记），禁止静默显示为空数据。
* 加载失败态必须带"重试"与"返回"；禁止使用造假进度条，加载中只有 spinner。
* 批量危险操作用确认对话框，禁止要求用户手输英文确认串。

### 6.5 图标体系 (Icons)
* 全站统一使用已有依赖 **lucide-react**，禁止引用未安装的图标库（如 Font Awesome）；旋转动画用本地 CSS keyframes。
