from typing import Any, Dict, Optional
import ipaddress


def is_http_url(url: Optional[str]) -> bool:
    return bool(url and str(url).lower().startswith(("http://", "https://")))


def build_system_proxy_config(proxy_url: Any) -> Optional[Dict[str, str]]:
    value = str(proxy_url or "").strip()
    if not value:
        return None
    return {"http": value, "https": value}


def is_fake_proxy_ip_address(address: str) -> bool:
    return ipaddress.ip_address(address) in ipaddress.ip_network("198.18.0.0/15")


def is_blocked_remote_ip(address: str, *, allow_fake_proxy_ip: bool = False) -> bool:
    ip = ipaddress.ip_address(address)
    if allow_fake_proxy_ip and is_fake_proxy_ip_address(address):
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
