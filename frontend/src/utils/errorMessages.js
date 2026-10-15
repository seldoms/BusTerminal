// 后端错误码（snake_case 机器码）→ 中文提示映射表
// 后端 detail/message 返回的错误码统一在这里翻译成用户可读文案，新增错误码直接往下补
export const API_ERROR_MESSAGES = {
    // 鉴权
    invalid_credentials: '用户名或密码错误',
    too_many_attempts: '尝试次数过多，请稍后再试',
    invalid_username: '用户名格式不正确',
    password_too_short: '密码长度不足',
    setup_already_completed: '初始化已完成，请直接登录',
    // 影片 / 刮削
    movie_code_not_found: '未找到对应番号',
    invalid_scraper_url: '无效的刮削链接',
    invalid_video_id: '无效的视频 ID',
    invalid_magnet_url: '无效的磁力链接',
    invalid_save_path: '保存路径无效',
    no_magnets_available_pending: '暂无可用磁力，任务已加入等待',
    no_suitable_magnet_pending: '暂无合适磁力，任务已加入等待',
    download_blocked: '下载被阻止，请检查权限设置',
    download_queue_options_unsupported: '当前下载队列不支持该操作',
    // 女优 / 订阅
    actress_not_found: '未找到该女优',
    missing_actress_id: '缺少女优 ID',
    missing_actress_identity: '缺少女优标识信息',
    missing_person_identity: '缺少人物标识信息',
    subscription_save_failed: '订阅保存失败',
    subscription_not_found: '订阅不存在',
    subscription_not_visible: '订阅内容当前不可见',
    invalid_subscription_url: '订阅链接无效',
    missing_subscription_url: '缺少订阅链接',
    missing_subscription_url_or_name: '缺少订阅链接或名称',
    // qBittorrent
    qb_unavailable: 'qBittorrent 不可用，请检查配置',
    qb_login_failed: 'qBittorrent 登录失败，请检查账号密码',
    invalid_qb_category: '无效的分类',
    unsupported_torrent_action: '不支持的种子操作',
    torrent_all_control_requires_confirmation: '批量操作全部种子需要确认',
    missing_or_invalid_torrent_hashes: '缺少或无效的种子标识',
    qb_torrent_control_failed: '种子操作失败',
    // Jellyfin
    jellyfin_unavailable: 'Jellyfin 不可用，请检查配置',
    invalid_jellyfin_item_id: '无效的 Jellyfin 项目 ID',
    jellyfin_stats_failed: '获取 Jellyfin 统计失败',
    jellyfin_delete_disabled: 'Jellyfin 删除功能未启用',
    jellyfin_api_key_required_for_host_change: '更改 Jellyfin 主机时必须重新填写 API Key',
    delete_reason_too_short: '删除原因过短',
    jellyfin_webhook_token_required: '缺少 Jellyfin Webhook 令牌',
    invalid_jellyfin_webhook_token: 'Jellyfin Webhook 令牌无效',
    // 发现 / 榜单
    unsupported_discovery_source: '不支持的数据源',
    invalid_rank_type: '无效的榜单类型',
    discovery_fetch_failed: '榜单数据获取失败',
    jable_hot_fetch_failed: '热门数据获取失败',
    // 看板 / 统计 / 搜索
    dashboard_stats_failed: '看板统计获取失败',
    search_movies_failed: '搜索失败，请稍后重试',
    // 缓存
    unsupported_cache_category: '不支持的缓存类型',
    cache_invalidation_confirmation_required: '缓存清理需要确认',
    cache_warmup_empty: '没有需要预热的缓存',
    // 任务 / 候选
    task_not_found: '任务不存在',
    invalid_task_id: '无效的任务 ID',
    task_cancelled: '任务已取消',
    candidate_not_found: '候选项不存在',
    candidate_state_conflict: '候选项状态已变化，请刷新后重试',
    invalid_download_candidate: '无效的下载候选项',
    cleanup_candidate_not_found: '清理候选项不存在',
    invalid_cleanup_candidate_id: '无效的清理候选项 ID',
    cleanup_candidate_state_conflict: '清理候选项状态已变化，请刷新后重试',
    cleanup_decision_reason_required: '请填写处理原因',
    // 参数 / 其他
    invalid_genre_type: '无效的分类类型',
    invalid_library_id: '无效的媒体库 ID',
    invalid_sort_by: '无效的排序字段',
    invalid_sort_order: '无效的排序方式',
    invalid_behavior_bucket: '无效的行为统计区间',
    invalid_status_filter: '无效的状态过滤条件',
    invalid_system_config: '系统配置参数无效',
    invalid_subscription_stats_response: '订阅统计数据异常',
    automation_actions_disabled: '自动化操作未启用',
    actress_save_failed: '女优信息保存失败',
    texts_must_be_list: '翻译文本格式错误',
    translation_batch_too_large: '翻译批量过大',
    translation_text_too_long: '翻译文本过长',
    image_proxy_failed: '图片加载失败',
};

// 从 FastAPI 422 校验错误数组中提取可读的字段提示
const formatValidationError = (detail) => {
    const first = Array.isArray(detail) ? detail.find((item) => item && typeof item === 'object') : null;
    const field = Array.isArray(first?.loc) && first.loc.length > 0
        ? String(first.loc[first.loc.length - 1])
        : '';
    return field ? `请求参数错误：${field}` : '请求参数错误';
};

// 纯英文机器码（snake_case / 短码）不直接糊给用户，统一换成通用提示
const MACHINE_CODE_PATTERN = /^[a-z][a-z0-9_]*$/;
const GENERIC_FAILURE_MESSAGE = '操作失败，请稍后重试';

export const mapApiErrorMessage = (rawMessage, fallback) => {
    // 422 校验错误：detail 是对象数组
    if (Array.isArray(rawMessage)) {
        return formatValidationError(rawMessage);
    }

    if (rawMessage == null || rawMessage === '') {
        return fallback;
    }

    if (typeof rawMessage !== 'string') {
        // detail 是对象等其他形态时兜底，避免出现 [object Object]
        return GENERIC_FAILURE_MESSAGE;
    }

    const text = rawMessage.trim();
    if (!text) return fallback;

    if (API_ERROR_MESSAGES[text]) {
        return API_ERROR_MESSAGES[text];
    }

    // 网络层错误
    if (text === 'Network Error') {
        return '网络连接失败，请检查网络后重试';
    }
    if (/^timeout of \d+ms exceeded$/i.test(text)) {
        return '请求超时，请稍后重试';
    }
    // axios 状态码错误 / 裸 500 文本
    const statusMatch = text.match(/^Request failed with status code (\d+)$/i);
    if (statusMatch) {
        const status = Number.parseInt(statusMatch[1], 10);
        if (status >= 500) return '服务器内部错误，请稍后重试';
        if (status === 404) return '请求的资源不存在';
        if (status === 403) return '没有权限执行该操作';
        return GENERIC_FAILURE_MESSAGE;
    }
    if (/internal server error/i.test(text)) {
        return '服务器内部错误，请稍后重试';
    }

    // 含中文的视为后端已经给过可读文案，原样返回
    if (/[一-鿿]/.test(text)) {
        return text;
    }

    // 纯英文机器码不给用户看
    if (MACHINE_CODE_PATTERN.test(text)) {
        return GENERIC_FAILURE_MESSAGE;
    }

    // 其余英文句子（如 FastAPI 默认 "Not Found"）相对可读，保留原文
    return text;
};
