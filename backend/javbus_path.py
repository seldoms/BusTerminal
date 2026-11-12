# -*- coding: utf-8 -*-
"""番号 → JavBus 详情页路径片段。

**为什么需要这个模块**：库里 `movies.id` 有三种写法混在一起

| 例子           | 来源     | JavBus 路径 | 能否拼出详情页 |
|----------------|----------|-------------|----------------|
| ``JUFE-633``   | javbus   | ``JUFE-633``   | ✅ 直接可用     |
| ``jur00696``   | amane    | ``JUR-696``    | ✅ 需要转换     |
| ``gma00090``   | amane    | ``GMA-090``    | ✅ 需要转换     |
| ``HTTM-60``    | 短编号   | ``HTTM-060``   | ✅ 需要补零     |
| ``ASBX6559``   | r18.dev  | —             | ❌ 站上没有     |

`maintenance_service.py` 之前直接 `f"{base_url}/{m_id}"` 拼 URL，
碰到 amane 形态的 id 必然 404（日志里 571 次「无法在页面中识别出 gid」
里有 107 次是这类）。

**关键实测结论（2026-11-07，生产容器内实测 32 个编号）**：
- JavBus 番号的数字部分**恰好 3 位、零填充**。少于 3 位补零、多于 3 位去零：
  ``HTTM-60`` 404 / ``HTTM-060`` 200；``JUR-696`` 200 / ``JUR-0696`` 404；
  ``GMA-90`` 404 / ``GMA-090`` 200；``MIRD-282`` 200 / ``MIRD-0282`` 404。
- **前缀必须大写**：实测 ``gma00090`` → 404，``GMA-090`` → 200 且有 gid。
- 带后缀的番号（``SS-155EX`` / ``NAAC-060B``）**站上没有**，
  这类不做转换、原样返回，让调用方走别的源。
- 纯数字前缀形态（如 ``021026-100``）不是 JavBus 番号，不转换。

注意与 `movie_code.normalize_movie_code` 的分工：那个函数**刻意去前导零**，
只能用于比对，**不能用来拼 URL**（``JUR-097`` 会变成 ``JUR97`` → 404）。
本模块只负责拼 URL，且是「唯一允许拼 URL 的地方」。
"""
import re

# 「字母前缀 + 数字」，允许 amane 的 0 填充形态（jur00696）与标准形态（JUR-696）
_CODE_RE = re.compile(r"^([A-Za-z]{2,6})[\s\-_]?(\d{1,6})$")

# 明显不是 JavBus 番号的形态，直接放弃转换
#   · 纯数字开头（021026-100 是日期型编号）
#   · 含非字母数字前缀（104anpd001 属于 r18.dev 体系）
_UNSUPPORTED_PREFIX = re.compile(r"^[0-9]")

# 已知站上没有的番号形态：带尾缀字母（SS-155EX / NAAC-060B）
_SUFFIXED = re.compile(r"^[A-Za-z]{2,6}[-\s]?\d{1,6}[A-Za-z]{1,3}$")


def to_javbus_path(movie_id) -> str:
    """把库内 id 换成 JavBus 详情页的路径片段（不含前导斜杠）。

    >>> to_javbus_path("JUFE-633")
    'JUFE-633'
    >>> to_javbus_path("jur00696")
    'JUR-696'
    >>> to_javbus_path("gma00090")
    'GMA-090'
    >>> to_javbus_path("HTTM-60")
    'HTTM-060'
    >>> to_javbus_path("ASBX6559")
    'ASBX6559'
    >>> to_javbus_path("SS-155EX")
    'SS-155EX'
    """
    raw = str(movie_id or "").strip()
    if not raw:
        return ""
    if _SUFFIXED.match(raw) and not _CODE_RE.match(raw):
        # SS-155EX / NAAC-060B：站上没有这种番号，原样返回不猜
        return raw.upper()
    m = _CODE_RE.match(raw)
    if not m:
        return raw
    prefix, digits = m.group(1), m.group(2)
    if _UNSUPPORTED_PREFIX.match(prefix):
        return raw
    # 数字位处理，JavBus 的规则是**数字部分恰好 3 位（零填充）**：
    #   · 2026-11-07 生产容器内实测（20 个短编号 + 12 个边界编号）：
    #       HTTM-60 404  / HTTM-060 200      AA-2 404  / AA-002 200
    #       JUR-97 404  / JUR-097 200        SS-15 404  / SS-015 200
    #       GMA-90 404  / GMA-090 200        MIRD-28 404 / MIRD-028 200
    #       JUR-696 200 / JUR-0696 404       MIRD-282 200 / MIRD-0282 404
    #     即：少于 3 位要补零，多于 3 位要去零，恰好 3 位原样。
    #   · amane 的 0 填充形态（jur00696 / gma00090）是 5 位，
    #     去前导零后再补到 3 位：jur00696 → JUR-696、gma00090 → GMA-090。
    #   · ASBX6559 这类「无分隔 + 四位数字」—— 站上没有（实测 404），
    #     原样返回让调用方走别的源，不要猜一个错形态浪费一次请求。
    if len(digits) >= 4 and not digits.startswith("0") and not any(
        sep in raw for sep in "-_ "
    ):
        # 无分隔 + 非 0 开头的四位数：ASBX6559 形态，站上没有，原样返回
        return raw.upper()
    digits = digits.lstrip("0") or "0"
    if len(digits) < 3:
        digits = digits.zfill(3)
    return f"{prefix.upper()}-{digits}"


def build_detail_url(base_url: str, movie_id) -> str:
    """拼出 JavBus 详情页完整 URL。

    >>> build_detail_url("https://www.buscdn.casa", "jur00696")
    'https://www.buscdn.casa/JUR-696'
    """
    base = str(base_url or "").rstrip("/")
    path = to_javbus_path(movie_id)
    if not base or not path:
        return ""
    return f"{base}/{path}"


if __name__ == "__main__":
    cases = [
        ("JUFE-633", "JUFE-633"),
        ("jur00696", "JUR-696"),
        ("gma00090", "GMA-090"),
        ("rbb00340", "RBB-340"),
        ("ofje00646", "OFJE-646"),
        ("JUR-097", "JUR-097"),
        ("ASBX6559", "ASBX6559"),
        ("SS-155EX", "SS-155EX"),
        ("NAAC-060B", "NAAC-060B"),
        ("021026-100", "021026-100"),
        # 短编号一律补零到 3 位（2026-11-07 实测）
        ("HTTM-60", "HTTM-060"),
        ("AA-2", "AA-002"),
        ("AAID-01", "AAID-001"),
        ("SS-15", "SS-015"),
        ("MIRD-28", "MIRD-028"),
    ]
    ok = 0
    for src, exp in cases:
        got = to_javbus_path(src)
        flag = "OK " if got == exp else "FAIL"
        ok += got == exp
        print(f"  {flag} {src:14s} → {got:14s} (期望 {exp})")
    print(f"\n{ok}/{len(cases)} 通过")
    raise SystemExit(0 if ok == len(cases) else 1)
