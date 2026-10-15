"""标题翻译质量抽检（只读，不写库）。

目的：在开启 `TRANSLATION_BACKFILL_ENABLED` 之前，用真实待翻标题量化验证
「占位保护 + Hunyuan-MT 7B + 质量闸门」这条链路的实际表现 —— 通过率、
闸门拦截率与拦截原因分布，并导出样本供人工核对。

设计成在部署环境内跑（同时能访问数据库与 Ollama），
走的是与线上完全相同的代码路径，不做任何模拟。

用法：
    python3 sample_translation_quality.py --count 200 --out /tmp/report.json
Ollama 地址与模型可用 --ollama-url / --ollama-model 指定，
默认读环境变量 OLLAMA_BASE_URL / OLLAMA_MODEL。
输出：
    /tmp/report.json  —— 机器可读报告（含样本译文）
    stdout            —— 汇总
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter

sys.path.insert(0, "/app")

from database import Database  # noqa: E402
from ollama_translator import OllamaTranslator  # noqa: E402
from translation_guard import TitleGuard, is_low_quality  # noqa: E402

# 覆盖不同源站/长度：随机采样已足够，此处只按长度分档统计
LENGTH_BUCKETS = ((0, 20, "短"), (20, 40, "中"), (40, 80, "长"), (80, 9999, "超长"))


def bucket(length: int) -> str:
    for low, high, label in LENGTH_BUCKETS:
        if low <= length < high:
            return label
    return "超长"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--out", default="/tmp/translation_sampling.json")
    ap.add_argument("--seed", type=int, default=20261011)
    # Ollama 地址与模型从环境变量读，便于换机器时不改代码
    ap.add_argument("--ollama-url", default=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    ap.add_argument("--ollama-model", default=os.getenv("OLLAMA_MODEL", "hunyuan-mt-7b"))
    args = ap.parse_args()

    db = Database()
    names = db.get_actress_names_for_translation_guard()
    print(f"演员名（占位保护用）：{len(names)} 条")

    rows = db.fetch_all(
        "SELECT m.original_title FROM movies m "
        "WHERE m.original_title ~ '[ぁ-んァ-ヶ]' "
        "AND NOT EXISTS (SELECT 1 FROM translations t WHERE t.original_text = m.original_title) "
        "LIMIT 40000"
    )
    pool = [r[0] for r in rows if r and r[0]]
    print(f"待翻候选池：{len(pool)} 条")

    random.seed(args.seed)
    sample = random.sample(pool, min(args.count, len(pool)))

    guard = TitleGuard(names)
    translator = OllamaTranslator(
        args.ollama_url,
        args.ollama_model,
        timeout=300,
        batch_size=1,
        guard=guard,
        strict_quality_gate=False,  # 这里要拿到不合格译文以便分析原因，闸门逻辑照走
    )

    passed, rejected = [], []
    reason_counter = Counter()
    protected_counter = Counter()
    bucket_pass = Counter()
    bucket_total = Counter()
    started = time.time()

    for index, title in enumerate(sample, 1):
        bucket_total[bucket(len(title))] += 1
        protected, mapping = guard.protect(title)
        if mapping:
            protected_counter["含艺人名"] += 1

        out = translator.translate_batch([title])
        raw = out.get(title, "")
        leftovers = guard.leftover_tokens(raw, mapping) if mapping else []
        reason = is_low_quality(title, raw, leftover_tokens=leftovers, allowed=mapping.values())

        if reason:
            reason_counter[reason] += 1
            rejected.append({"title": title, "reason": reason, "raw": raw[:160]})
        else:
            passed.append({"title": title, "translated": raw[:160]})
            bucket_pass[bucket(len(title))] += 1

        if index % 25 == 0:
            print(f"  进度 {index}/{len(sample)} 通过 {len(passed)} 拦下 {len(rejected)}")

    elapsed = time.time() - started
    report = {
        "count": len(sample),
        "passed": len(passed),
        "rejected": len(rejected),
        "pass_rate": round(len(passed) / max(len(sample), 1), 4),
        "elapsed_seconds": round(elapsed, 1),
        "seconds_per_item": round(elapsed / max(len(sample), 1), 3),
        "protected_titles": dict(protected_counter),
        "reject_reasons": dict(reason_counter),
        "by_length": {
            label: {
                "total": bucket_total[label],
                "passed": bucket_pass[label],
                "rate": round(bucket_pass[label] / max(bucket_total[label], 1), 4),
            }
            for _, _, label in LENGTH_BUCKETS
        },
        "samples_passed": passed[:40],
        "samples_rejected": rejected[:20],
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print("\n================ 抽检汇总 ================")
    print(f"样本 {report['count']} 条：通过 {report['passed']} / 拦下 {report['rejected']}"
          f"（通过率 {report['pass_rate'] * 100:.1f}%）")
    print(f"平均耗时 {report['seconds_per_item']} 秒/条 → 103 万条约 "
          f"{1032516 * report['seconds_per_item'] / 86400:.1f} 天")
    print(f"命中占位保护（含艺人名）：{report['protected_titles']}")
    print(f"拦截原因分布：{report['reject_reasons'] or '无'}")
    print("按标题长度：")
    for label, stat in report["by_length"].items():
        if stat["total"]:
            print(f"  {label:<4} {stat['passed']}/{stat['total']} = {stat['rate'] * 100:.0f}%")
    print(f"\n报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())