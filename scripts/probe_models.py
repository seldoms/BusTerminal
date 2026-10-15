#!/usr/bin/env python3
"""对 OpenAI 兼容端点上的全部模型做谜语测活 + 测速排序。

背景（2026-10-11）：发现一个聚合网关（152 个模型，含 30+ 个 `our-free-model/*` 免费档），
要评估哪些模型能吃下 103 万条日译中的积压。

测活方式按要求用**谜语**：比"能不能返回 200"严格得多——它同时验证了
① 是否真的在做推理、② 是否会漏掉题目里的条件、③ 首 token 延迟与吞吐。
翻译任务真正在意的是「短文本、格式受控、延迟低」，谜语恰好覆盖这三点。

用法：
    python scripts/probe_models.py --base-url http://host:port/v1 --token-file tok.txt
    python scripts/probe_models.py --prefix our-free-model --concurrency 12
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import io
import json
import time
import urllib.error
import urllib.request

# 谜语要「只有一个答案 + 必须用条件推理」，答对才说明模型真在推理而不是模板匹配。
RIDDLES = [
    ("我有两个头，一个在左一个在右，猜一个汉字。", "吕"),
    ("什么东西越洗越脏？打一自然现象", "水"),
    ("小明的爸爸有三个儿子，大儿子叫大毛，二儿子叫二毛，三儿子叫什么？", "小明"),
]

SYSTEM = (
    "你是一个只输出答案的问答程序。用户问什么你都只回答答案本身，"
    "不要解释、不要客套、不要复述题目。谜语只输出那一个词。"
)


def load_token(token_file: str | None, token: str) -> str:
    if token_file:
        token = io.open(token_file, encoding="utf-8").read().strip()
    return (token or "").strip().strip('"').strip()


def probe_one(base_url: str, token: str, model: str, timeout: int = 45) -> dict:
    """对一个模型跑完所有谜语，返回正确率与耗时统计。"""
    url = base_url.rstrip("/") + "/chat/completions"
    correct, total, latency, tokens_out, first_token = 0, 0, [], 0, 0.0
    error = ""
    for question, answer in RIDDLES:
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": question},
            ],
            "temperature": 0,
            "max_tokens": 16,
        }).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}"
            break
        except Exception as exc:  # noqa: BLE001
            error = type(exc).__name__
            break
        elapsed = time.time() - started
        latency.append(elapsed)
        usage = payload.get("usage") or {}
        tokens_out += int(usage.get("completion_tokens") or 0)
        choice = (payload.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        if not first_token:
            first_token = elapsed
        total += 1
        if answer in text:
            correct += 1
    return {
        "model": model,
        "correct": correct,
        "total": total,
        "alive": bool(total) and not error,
        "avg_latency": round(sum(latency) / len(latency), 2) if latency else None,
        "tokens": tokens_out,
        "tok_per_s": round(tokens_out / sum(latency), 1) if latency and sum(latency) else None,
        "error": error,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--prefix", default="", help="只测这个前缀的模型")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--limit", type=int, default=0, help="最多测多少个模型（0=不限）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    token = load_token(args.token_file, args.token)
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        models = [m["id"] for m in (json.loads(resp.read()).get("data") or [])]
    if args.prefix:
        models = [m for m in models if m.startswith(args.prefix)]
    if args.limit:
        models = models[: args.limit]
    print(f"待测模型 {len(models)} 个，并发 {args.concurrency}\n")

    started = time.time()
    results = []
    done = 0
    with futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        jobs = {
            pool.submit(probe_one, args.base_url, token, model, args.timeout): model
            for model in models
        }
        for future in futures.as_completed(jobs):
            done += 1
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001
                row = {"model": jobs[future], "correct": 0, "total": 0,
                       "alive": False, "avg_latency": None, "tokens": 0,
                       "tok_per_s": None, "error": type(exc).__name__}
            results.append(row)
            flag = "OK " if row["alive"] and row["correct"] == row["total"] else (
                "ok " if row["alive"] else "DEAD")
            print(f"[{done:>3}/{len(models)}] {flag} {row['model']:<52} "
                  f"{row['correct']}/{row['total']} "
                  f"{row['avg_latency'] or '-'}s {row['error']}")

    alive = [r for r in results if r["alive"]]
    perfect = sorted(
        [r for r in alive if r["correct"] == r["total"]],
        key=lambda r: (r["avg_latency"] or 99),
    )
    print(f"\n测活 {len(alive)}/{len(results)}，全对 {len(perfect)} 个，"
          f"耗时 {time.time() - started:.0f}s")
    print("\n===== 全对模型按首响速度排序（可吃翻译任务）=====")
    for row in perfect[:30]:
        print(f"  {row['avg_latency']:>6.2f}s  {row['tok_per_s'] or 0:>6} tok/s  {row['model']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"results": results, "perfect": perfect}, handle,
                      ensure_ascii=False, indent=2)
        print(f"\n明细已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())