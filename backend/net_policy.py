"""出网代理策略：一个开关，两套语义。

背景（本机开发环境踩过多次）:
    IDE / 工具链会往 shell 里注入 `http_proxy`（例如 127.0.0.1:64891）。
    对**内网服务**（amane / Jellyfin / qB）的请求一旦被代理劫持，表现是：
      - 连到一个随机高端口后读超时；
      - 或 amane 返回 404 而不是 401（整个绝对 URI 被当成 path 解析）。
    排查一次要很久，所以内网调用默认**绕开**环境变量代理。

两套语义:
    1. 外网抓取（javbus / jable / discovery / netflav / 封面下载）
       → 走既有配置项 `system_proxy`（DB > env SYSTEM_PROXY），留空则直连，
       由 network_security_utils.build_system_proxy_config() 转换，本模块不参与。
    2. 内网服务（amane / Jellyfin / qB）
       → 默认不跟随 http_proxy/https_proxy；
       确需打开时用本模块的 `internal_proxy_enabled`。

部署形态:
    生产（NAS）: 出网由路由器负责，两个开关都保持默认（system_proxy 空、
    internal_proxy_enabled=false）→ 全部直连，这也是唯一经过实机验证的形态。
    开发（本机）: 外网抓取填 `system_proxy`；**极少数情况**（例如容器网络确实
    需要代理才能到达 NAS）才打开 `internal_proxy_enabled`。

取值优先级: DB config `internal_proxy_enabled` > env BUS_TERMINAL_INTERNAL_PROXY_ENABLED > False。
每次调用都重新求值，改开关不需要重启进程。
"""
from typing import Any, Optional

import requests

CONFIG_KEY = "internal_proxy_enabled"

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _db_value() -> Optional[str]:
    """读库里的开关。延迟导入避免与 database 循环依赖；库不可用时降级到环境变量。"""
    try:
        from database import db

        value = db.get_config(CONFIG_KEY)
    except Exception:  # noqa: BLE001 - 配置读取失败不应阻断调用
        return None
    return None if value is None else str(value)


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in _TRUE_VALUES


def internal_proxy_enabled() -> bool:
    """内网服务调用是否跟随系统代理环境变量。"""
    raw = _db_value()
    if raw is None:
        import config

        raw = getattr(config, "INTERNAL_PROXY_ENABLED", False)
    return _as_bool(raw)


def apply_policy(session: requests.Session) -> requests.Session:
    """按当前开关设置 session.trust_env。"""
    session.trust_env = internal_proxy_enabled()
    return session


_SHARED_SESSION = requests.Session()


def internal_session() -> requests.Session:
    """进程内共享的内网 session，每次都按当前开关刷新 trust_env。"""
    return apply_policy(_SHARED_SESSION)
