/**
 * 引导页的「这套系统能做什么」提示文案。
 *
 * 每条都对应 `docs/services/bus_terminal.md` 里真实存在的能力域（A~H），
 * 不写代码里没有的功能 —— 引导页说能做、实际做不了，比不说更糟。
 */

export const CAPABILITY_DOMAINS = [
    {
        id: 'discovery',
        title: '影片检索与发现',
        summary: '从 JavBus / Jable / JavDB / AirAV 抓列表与详情，按清晰度偏好选最优磁力，标题自动译成中文。',
    },
    {
        id: 'cover',
        title: '封面本地缓存',
        summary: '外网封面被墙或很慢时走后端代理 + 本地缓存，加载速度基本靠这层缓存撑着。',
    },
    {
        id: 'download',
        title: '下载与做种',
        summary: '点下载后自动入库去重、排队、投递 qBittorrent、设做种时限，完成后自动交付。',
    },
    {
        id: 'subscription',
        title: '订阅与女优追踪',
        summary: '订阅分类或女优后定期巡检新片，按自动化设置自动入队；女优页可一键订阅。',
    },
    {
        id: 'ingest',
        title: '入库闭环（刮削 + 整理）',
        summary: '下载完成后自动补番号、抓元数据、按「工作室/番号」归档，最后触发 Jellyfin 刷新。',
    },
    {
        id: 'library',
        title: '媒体库与播放',
        summary: '读 Jellyfin 媒体库做展示与播放代理；卡片上能直接看到「已入库/可下载」。',
    },
    {
        id: 'lifecycle',
        title: '生命周期追踪与清理审批',
        summary: '一部片从发现到入库到清理的全链路状态可查；清理只生成候选，删除要人工确认。',
    },
    {
        id: 'ops',
        title: '运维与健康检查',
        summary: '设置页、后台任务状态、运行日志、媒体栈自检、定时备份与磁盘水位监控。',
    },
];

/**
 * 每一步对应的能力提示：做什么 → 填了这里能解锁什么 → 不填会怎样。
 * `requires` 用于判断这一步的关键配置是否已具备，实时显示就绪状态。
 */
export const STEP_CAPABILITIES = {
    services: {
        headline: '填好这里，打通四个服务，全链路才能自动跑起来',
        points: [
            {
                icon: 'download',
                title: 'qBittorrent —— 负责下载与做种',
                detail: '所有下载请求经它落地。系统会自动过滤种子内的广告/样片文件、设置做种时限。',
                requires: ['qb_host', 'qb_username'],
            },
            {
                icon: 'ingest',
                title: 'amane —— 负责刮削与整理落盘',
                detail: '全系统唯一碰文件的组件：按番号抓元数据、写 NFO、按工作室/番号归档。',
                requires: ['amane_base_url', 'amane_token'],
            },
            {
                icon: 'library',
                title: 'Jellyfin —— 负责呈现与播放',
                detail: '接上后卡片会显示「播放」，入库闭环也能确认影片真的可见。',
                requires: ['jellyfin_host'],
            },
            {
                icon: 'discovery',
                title: '翻译服务 —— 日文标题转中文',
                detail: '未翻译的条目不会下发给页面。选本地大模型可无限量且不出网。',
                requires: [],
            },
        ],
        footnote: '这些地址与密钥都存在本地数据库的 config 表里，敏感项加密存储。',
    },

    download: {
        headline: '决定「系统替你自动做多少」',
        points: [
            {
                icon: 'download',
                title: '磁盘熔断',
                detail: '剩余空间低于阈值时自动暂停新下载，避免把盘写满导致 Jellyfin 读取异常。',
                requires: [],
            },
            {
                icon: 'download',
                title: '做种时限',
                detail: '到点自动停止做种，释放带宽与磁盘。与 qB 侧设置互为兜底。',
                requires: [],
            },
            {
                icon: 'subscription',
                title: '自动化模式',
                detail: '「建议」只列候选等你确认；「自动放行低风险」才会替你直接下载。',
                requires: [],
            },
            {
                icon: 'download',
                title: '清晰度偏好',
                detail: '同一部片有多个版本时，按这个偏好选磁力。',
                requires: [],
            },
        ],
        footnote: '拿不准就先用「建议」模式跑一段时间，看清候选质量再决定是否放开自动化。',
    },

    filter: {
        headline: '一条规则，同时作用于「看得见」和「下得下」',
        points: [
            {
                icon: 'filter',
                title: '列表侧拦截',
                detail: '命中的条目不会出现在首页、分类、发现流和搜索结果里。',
                requires: [],
            },
            {
                icon: 'download',
                title: '下载侧拦截',
                detail: '手动点下载、订阅命中、榜单自动下载、自动化决策 —— 全部走同一道闸门。',
                requires: [],
            },
            {
                icon: 'filter',
                title: '匹配范围',
                detail: '同时检查标题、番号与演员名，忽略大小写与分隔符。番号带 VR 的片子同样会被挡住。',
                requires: [],
            },
        ],
        footnote: '建议先点「试算」看看会挡住什么。屏蔽词填太宽（比如一个常见单词）会静默挡掉大量正常影片。',
    },

    cache: {
        headline: '决定「多久重新抓一次」和「最多占多少盘」',
        points: [
            {
                icon: 'cover',
                title: '磁盘上限',
                detail: '超过限额后按最近最少使用清理。封面缓存是主要占用方，也是加载速度的来源。',
                requires: [],
            },
            {
                icon: 'discovery',
                title: '详情缓存有效期',
                detail: '超时的影片详情会重新抓取。调太短会让外网请求变频繁。',
                requires: [],
            },
            {
                icon: 'discovery',
                title: '发现列表缓存有效期',
                detail: '决定首页/分类/发现流多久刷新一次。',
                requires: [],
            },
        ],
        footnote: '网络通畅可以调短追求新鲜度；外网慢或被墙就调长，缓存命中率越高越省事。',
    },

    database: {
        headline: '决定「历史数据能不能接着用」',
        points: [
            {
                icon: 'ops',
                title: '已有数据直接复用',
                detail: '影片、演员、译文等数据都在库里，接着用就无需重新抓取百万级条目。',
                requires: [],
            },
            {
                icon: 'cover',
                title: '缓存与业务数据分离',
                detail: '封面缓存是独立目录，可以放高速盘；数据库建议放机械盘。',
                requires: [],
            },
        ],
        footnote: '连接串由环境变量 DATABASE_URL 决定，进程启动时读取。这里能做体检与比对，但切换必须改环境变量后重启。',
    },

    jellyfin: {
        headline: '下载完成后，自动变成「可播放的成品」',
        points: [
            {
                icon: 'ingest',
                title: '自动补番号',
                detail: '文件名常带水印和前缀，自动从路径与标题回推真实番号，刮削才准。',
                requires: [],
            },
            {
                icon: 'ingest',
                title: '抓元数据并归档',
                detail: '按「工作室/番号」目录结构存放，Jellyfin 侧能正确识别为一部影片。',
                requires: [],
            },
            {
                icon: 'library',
                title: '触发刷新并确认可见',
                detail: '入库后刷新媒体库并回读校验，确保不只是「文件在」而是「真的能播」。',
                requires: [],
            },
            {
                icon: 'lifecycle',
                title: '全程可追溯',
                detail: '每一步的成败都记录在生命周期里，失败能在界面上看到卡在哪一环。',
                requires: [],
            },
        ],
        footnote: '这一段会实际写文件、改名、调用 amane。建议先在测试环境跑通再全开。',
    },
};

export const STEP_STEP_HINTS = {
    services: '必填项越多，后面能自动跑的能力越多。可以先只填 qB 与 Jellyfin，其余稍后随时回来改。',
    download: '这些参数随时可改，不影响已经下载好的影片。',
    filter: '支持每行一个或逗号分隔。随时可改，改完立即生效。',
    cache: '容量与有效期可以都调保守一些，之后随时调大。',
    database: '不确定就先跳过体检，直接保存即可。',
    jellyfin: '不确定就先只开「启用入库闭环」总开关，其余保持默认。',
};