# -*- coding: utf-8 -*-
"""番号归一化 —— 单一事实来源（Single Source of Truth）
清洗存量数据与代码入库路径必须共用本函数，否则新数据会再次造成重复污染。

归一化规则（按优先级）：
  1. 去除抓取日期后缀        NMSL-014_2025-08-07 → NMSL-014
  2. 统一大写                ssd0004            → SSD0004
  3. 去除分隔符（- _ 空格）  SSD-003            → SSD003
  4. 字母+数字分段，数字去前导零  SSD-000003 / SSD-03 → SSD3
  5. 其余形态（含非字母数字）原样保留，只做去除空白
"""
import re

_DATE_SUFFIX = re.compile(r"_\d{4}-\d{2}-\d{2}$")
_PART = re.compile(r"^([A-Z]*)(\d+)$")


def normalize_movie_code(raw) -> str:
    """把任意来源的番号写法归一到统一形态。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    s = _DATE_SUFFIX.sub("", s)          # 1. 去日期后缀
    s = s.upper()                         # 2. 大写
    s = re.sub(r"[\s\-_]", "", s)         # 3. 去分隔符
    m = _PART.match(s)                    # 4. 去前导零
    if m:
        return m.group(1) + str(int(m.group(2)))
    return s


def is_same_movie(code_a, code_b) -> bool:
    return normalize_movie_code(code_a) == normalize_movie_code(code_b)


if __name__ == "__main__":
    # 注意：不追求把所有源（如 r18.dev 的 104anpd001）硬归到一个形态——
    # 激进剥离数字前缀有误判风险。跨源判重的主判据是 detail_url（详情页唯一），
    # 本函数只用做「同源写法差异」的辅助交叉验证。
    cases = [
        ("SSD-003", "SSD3"), ("SSD-000003", "SSD3"), ("SSD003", "SSD3"),
        ("SSD-03", "SSD3"), ("SSD-00003", "SSD3"), ("ssd0004", "SSD4"),
        ("NMSL-014_2025-08-07", "NMSL14"), ("NMSL-014", "NMSL14"),
        ("ZVJL-001", "ZVJL1"), ("ZVJL-000001", "ZVJL1"), ("ZVJL001", "ZVJL1"),
        ("HMD-01", "HMD1"), ("HMD-00001", "HMD1"), ("HMD001", "HMD1"),
        ("MIDE-123", "MIDE123"), ("DWD-006", "DWD6"), ("BRD-011", "BRD11"),
        # 数字前缀形态：保守处理，仅去分隔符与大写，不做前缀剥离
        ("104anpd001", "104ANPD001"),
    ]
    ok = 0
    for src, exp in cases:
        got = normalize_movie_code(src)
        flag = "OK " if got == exp else "FAIL"
        if got == exp:
            ok += 1
        print(f"  {flag} {src:22s} → {got:14s} (期望 {exp})")
    print(f"\n{ok}/{len(cases)} 通过")
    raise SystemExit(0 if ok == len(cases) else 1)
