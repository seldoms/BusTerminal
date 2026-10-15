#!/usr/bin/env python3
"""修正 amane `media_files.number`：错值改对、空值补齐。

背景（项目内部问题登记 KI-030 后续）
--------------------------------------
`number` 是 BusTerminal 把 amane 媒体记录和自己片库对上号的唯一钥匙，amane 自己却管不好它：

1. **错值**：amane 在 refresh 扫描 `_incoming` 时按**原始文件名**猜番号，而下载站会在
   文件名里塞水印，于是猜出垃圾：
     `4k2.mestart-646.mp4` → `MESTART-646`（真番号 START-646）
     `COM-956998`（真番号 JUR-265 / MBMH-097，两条不同片子被写成同一个值）
   organize 之后路径是对的（`/hp/MBM/MBMH-097/MBMH-097.mp4`），只有 number 列错。
2. **空值**：MoviePilot 时代遗留下来的目录（`/hp/all/<中文标题>/<番号> 演员/标题.mp4`）
   amane 填不出番号，全库 1188 条里一度 97.5% 为空。

本脚本按**路径**重新判号，只对「有把握」的记录下手：
  · 目录名和文件名判出的番号不同 → 本来是死结，这里用 BusTerminal 自己的 `movies` 表
    （144 万真实番号）裁决：谁在片库里存在就取谁，且要求它锚定在所在片段开头
    （`SNAP×SNAP model.021_七叶` 判出的 `SNAP-021` 开头不吻合，直接否掉）。
  · 判出的番号必须在 `movies` 表里存在，否则跳过——宁可不填，也不填错。

用法
----
需要先产出输入文件（容器内跑，amane.db 路径带日文，只能走容器通道）：

    # ① 导出 amane 记录（id/path/number）
    # ② 导出「片库里真实存在的番号」清单
    python scripts/fix_amane_numbers.py --input media.json --known-file known.txt            # 干跑
    python scripts/fix_amane_numbers.py --input media.json --known-file known.txt --apply \\
        --base-url http://192.168.1.100:8000 --token-file token.txt                        # 真跑

默认 dry-run，--apply 才写 amane。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from number_parsing import extract_number  # noqa: E402

_NON_ALNUM = re.compile(r"[^0-9A-Z]+")


def _key(value: str) -> str:
    """归一化用于「开头锚定」比较：只留大写字母数字。"""
    return _NON_ALNUM.sub("", str(value or "").upper())


def _anchored(code: str, segment: str) -> bool:
    """番号必须落在目录名/文件名的开头（忽略 `.`、`_`、空格与大小写）。

    反例：`SNAP×SNAP model.021_七叶` 能解出 `SNAP-021`，但归一化后是
    `SNAPSNAPMODEL021…`，并不以 `SNAP021` 开头 —— 说明它是从标题中段抠出来的，不可信。
    """
    return bool(code) and _key(segment).startswith(_key(code))


def resolve_number(path: str, known: frozenset[str] = frozenset()) -> tuple[str | None, str]:
    """从媒体路径判番号，返回 (番号, 来源)。

    · `dir` / `file` / `both`：目录与文件名一致，或只有一边解得出，直接采信；
    · `dir+known` / `file+known`：两边冲突，靠片库存在性 + 开头锚定裁决（目录优先）；
    · `conflict`：裁决不了，跳过；`none`：两边都解不出。
    """
    segments = [s for s in str(path or "").split("/") if s]
    if not segments:
        return None, "none"

    from_dir = extract_number(segments[-2]) if len(segments) >= 2 else None
    from_file = extract_number(segments[-1])

    if from_dir and from_file and from_dir.upper() != from_file.upper():
        for code, tag, segment in ((from_dir, "dir", segments[-2]),
                                   (from_file, "file", segments[-1])):
            if _anchored(code, segment) and code.upper() in known:
                return code, tag + "+known"
        return None, "conflict"

    code = from_dir or from_file
    if not code:
        return None, "none"
    tag = "both" if (from_dir and from_file) else ("dir" if from_dir else "file")
    return code, tag


def build_plan(
    records: list[dict],
    known: frozenset[str],
) -> tuple[list[dict], list[dict]]:
    """把记录分成「要改」与「跳过」。

    改的条件：判得出番号、番号在片库里存在、且与现状不同。
    空值 → `fill`；非空且不同 → `fix`。
    """
    plan: list[dict] = []
    skipped: list[dict] = []
    for rec in records:
        stored = str(rec.get("number") or "").strip()
        code, source = resolve_number(rec.get("path", ""), known)
        if not code:
            skipped.append({**rec, "reason": source})
            continue
        if stored.upper() == code.upper():
            skipped.append({**rec, "reason": "already_correct"})
            continue
        if code.upper() not in known:
            skipped.append({**rec, "reason": "not_in_library", "candidate": code})
            continue
        plan.append({
            "id": rec["id"],
            "path": rec.get("path", ""),
            "stored": stored,
            "number": code.upper(),
            "source": source,
            "kind": "fill" if not stored else "fix",
        })
    return plan, skipped


def read_token(token_file: str, token: str) -> str:
    if token_file:
        token = io.open(token_file, encoding="utf-8").read().strip()
    token = (token or "").strip().strip('"').strip("'").strip()
    if not token:
        raise SystemExit("缺少 API token：请用 --token-file 或 --token 提供")
    return token


def candidate_codes(records: list[dict]) -> list[str]:
    """收集所有可能出现的番号，供「片库里有没有这部片」一次性查询。"""
    codes: set[str] = set()
    for rec in records:
        segments = [s for s in str(rec.get("path") or "").split("/") if s]
        for segment in segments[-2:]:
            code = extract_number(segment)
            if code:
                codes.add(code.upper())
        stored = str(rec.get("number") or "").strip()
        if stored:
            codes.add(stored.upper())
    return sorted(codes)


def load_from_container() -> tuple[list[dict], Any, Any]:
    """在 bus 容器内部执行：用生产配置直连 amane，省掉导出/导入 JSON。

    返回 (记录, amane 客户端, database 实例)。
    """
    import logging

    import amane_service
    import database

    db = database.Database()
    service = amane_service.AmaneService(
        db=db, logger=logging.getLogger("fix_amane_numbers"))
    client = service.get_client()

    records: list[dict] = []
    offset = 0
    while True:
        page = client.list_media(limit=200, offset=offset)
        items = page.get("items") or []
        for item in items:
            records.append({
                "id": item.get("id"),
                "path": item.get("path") or "",
                "number": item.get("number") or "",
            })
        if len(items) < 200:
            break
        offset += 200
    return records, client, db


def known_from_db(db: Any, codes: list[str]) -> frozenset[str]:
    """查 BusTerminal 自己的 movies 表，确认哪些番号是真实存在的。"""
    known: set[str] = set()
    if not codes:
        return frozenset()
    placeholder = "%s" if getattr(db, "is_postgres", False) else "?"
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        for start in range(0, len(codes), 400):
            chunk = [c.upper() for c in codes[start:start + 400]]
            sql = (f"SELECT id FROM movies WHERE UPPER(id) IN "
                   f"({','.join([placeholder] * len(chunk))})")
            cursor.execute(sql, tuple(chunk))
            for row in cursor.fetchall():
                value = row["id"] if isinstance(row, dict) else row[0]
                known.add(str(value).upper())
    finally:
        db._release_connection(conn)
    return frozenset(known)


def main() -> int:
    ap = argparse.ArgumentParser(description="修正 amane media_files.number")
    ap.add_argument("--source", default="file", choices=("file", "container"),
                    help="file=读导出 JSON；container=在 bus 容器里直连 amane 与数据库")
    ap.add_argument("--input", default="", help="导出的 JSON（[{id,path,number}, ...]）")
    ap.add_argument("--known-file", default="", help="片库真实番号清单（每行一个，大写）")
    ap.add_argument("--base-url", default="", help="amane 地址，如 http://192.168.1.100:8000")
    ap.add_argument("--token", default="", help="amane API token")
    ap.add_argument("--token-file", default="", help="存放纯 token 文本的文件")
    ap.add_argument("--apply", action="store_true", help="真正写 amane（默认只干跑）")
    ap.add_argument("--kind", default="all", choices=("all", "fix", "fill"),
                    help="只处理错值 / 只补空值（默认两类都处理）")
    ap.add_argument("--interval", type=float, default=0.15, help="每次 PATCH 之间的间隔秒")
    args = ap.parse_args()

    client = None
    if args.source == "container":
        records, client, db = load_from_container()
        known = known_from_db(db, candidate_codes(records))
        print(f"已从 amane 读取 {len(records)} 条媒体记录；"
              f"片库校验用番号 {len(known)} 个")
    else:
        if not args.input or not args.known_file:
            raise SystemExit("--source file 需要 --input 与 --known-file")
        records = json.load(io.open(args.input, encoding="utf-8"))
        known = frozenset(
            line.strip().upper()
            for line in io.open(args.known_file, encoding="utf-8")
            if line.strip()
        )
    plan, skipped = build_plan(records, known)
    if args.kind != "all":
        plan = [item for item in plan if item["kind"] == args.kind]

    fix = [i for i in plan if i["kind"] == "fix"]
    fill = [i for i in plan if i["kind"] == "fill"]
    by_reason: dict[str, int] = {}
    for item in skipped:
        by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1

    print(f"输入 {len(records)} 条；需要改动 {len(plan)} 条"
          f"（错值修正 {len(fix)}、空值补齐 {len(fill)}）；跳过 {len(skipped)} 条")
    print(f"  跳过原因：{by_reason}")

    print("\n== 错值修正 ==")
    for item in fix:
        print(f"  id={item['id']:<6} {item['stored'] or '(空)':<14} -> {item['number']:<14}"
              f" [{item['source']}] {item['path'][-64:]}")
    print(f"\n== 空值补齐（前 20 / 共 {len(fill)} 条）==")
    for item in fill[:20]:
        print(f"  id={item['id']:<6} {'':<14} -> {item['number']:<14}"
              f" [{item['source']}] {item['path'][-64:]}")

    if not args.apply:
        print("\n这是 dry-run。加 --apply 才会写入 amane。")
        return 0
    if not plan:
        print("\n没有需要改动的记录。")
        return 0

    import urllib.error
    import urllib.request

    token = ""
    if client is None:
        if not args.base_url:
            raise SystemExit("缺少 --base-url")
        token = read_token(args.token_file, args.token)

    ok = failed = 0
    errors: dict[str, int] = {}
    for index, item in enumerate(plan, 1):
        if client is not None:
            try:
                client.patch_media(item["id"], number=item["number"])
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                if failed <= 5:
                    print(f"  [失败] id={item['id']} {exc}")
            if index % 100 == 0:
                print(f"  进度 {index}/{len(plan)} 成功 {ok} 失败 {failed}")
            time.sleep(args.interval)
            continue

        url = f"{args.base_url.rstrip('/')}/api/media/{item['id']}"
        body = json.dumps({"number": item["number"]}).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="PATCH",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                resp.read()
            ok += 1
        except urllib.error.HTTPError as exc:
            failed += 1
            key = f"HTTP {exc.code}"
            errors[key] = errors.get(key, 0) + 1
            if failed <= 5:
                print(f"  [失败] id={item['id']} {key} {exc.read().decode('utf-8', 'replace')[:120]}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
            if failed <= 5:
                print(f"  [异常] id={item['id']} {exc}")
        if index % 100 == 0:
            print(f"  进度 {index}/{len(plan)} 成功 {ok} 失败 {failed}")
        time.sleep(args.interval)

    print(f"\n完成：成功 {ok} / {len(plan)}，失败 {failed}")
    if errors:
        print(f"失败分布：{errors}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
