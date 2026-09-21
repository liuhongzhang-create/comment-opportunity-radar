#!/usr/bin/env python3
"""Measure real accuracy on your own comments — do this before trusting the radar.

Jev's own documentation says English is the primary training language and that
CJK input is "handled but not equally well", and that literal-minded reading
makes sarcasm a known weak spot. Comment sections are exactly where sarcasm
lives, so the default thresholds are a starting point, not a calibrated answer.

This script runs your labelled comments through the same code path the UI uses,
then reports:

  * overall intent accuracy and a confusion matrix
  * per-class precision / recall, so you can see which intent is weakest
  * accuracy and coverage at each confidence threshold, which is how you pick
    a review_confidence value instead of guessing 0.45
  * priority accuracy, when your CSV also carries a 真实优先级 column

Usage:
    export TYPESAFE_API_KEY=sk-...
    python3 tools/calibrate.py --csv tools/sample_comments.csv

    # check the file parses without spending any quota
    python3 tools/calibrate.py --csv my_labeled.csv --dry-run

CSV format (header row required):
    评论,真实意图[,真实优先级]
    怎么买？可以发链接吗,purchase,高

Accepted 真实意图 values: purchase / objection / content_request / complaint /
casual, or their Chinese labels 购买咨询 / 成交顾虑 / 内容需求 / 投诉 / 普通互动.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app  # noqa: E402  (import after sys.path is prepared)

LABEL_ALIASES = {
    "购买咨询": "purchase",
    "购买": "purchase",
    "成交顾虑": "objection",
    "顾虑": "objection",
    "内容需求": "content_request",
    "投诉": "complaint",
    "普通互动": "casual",
    "普通": "casual",
    "无": "casual",
}

PRIORITY_ALIASES = {
    "高": "高",
    "中": "中",
    "低": "低",
    "人工复核": "人工复核",
    "high": "高",
    "medium": "中",
    "low": "低",
    "review": "人工复核",
}


def normalize_label(raw: str) -> str:
    value = (raw or "").strip()
    return LABEL_ALIASES.get(value, value.lower())


def load_dataset(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} 是空文件")
        text_field = next((name for name in reader.fieldnames if name and "评论" in name), None)
        if text_field is None:
            text_field = reader.fieldnames[0]
        intent_field = next(
            (name for name in reader.fieldnames if name and "意图" in name), None
        )
        if intent_field is None:
            raise SystemExit(f"{path} 缺少「真实意图」列，当前列：{reader.fieldnames}")
        priority_field = next(
            (name for name in reader.fieldnames if name and "优先级" in name), None
        )

        records = []
        for number, row in enumerate(reader, start=2):
            text = (row.get(text_field) or "").strip()
            if not text:
                continue
            label = normalize_label(row.get(intent_field, ""))
            if not label:
                raise SystemExit(f"第 {number} 行没有标注真实意图")
            record = {"text": text, "intent": label, "line": number}
            if priority_field:
                raw_priority = (row.get(priority_field) or "").strip()
                record["priority"] = PRIORITY_ALIASES.get(raw_priority, "")
            records.append(record)
    if not records:
        raise SystemExit(f"{path} 里没有可用的评论行")
    return records


def accuracy_report(records, results, target, min_coverage):
    by_index = {item["source_index"]: item for item in results}
    pairs = []
    for record in records:
        item = by_index.get(record["line"] - 2)
        if item is None or item.get("error"):
            continue
        pairs.append((record, item))

    failed = len(records) - len(pairs)
    total = len(pairs)
    if not total:
        print("没有任何一行成功返回，无法评估。")
        return

    correct = sum(1 for record, item in pairs if item["intent"] == record["intent"])
    print(f"\n样本 {len(records)} 条 · 成功 {total} 条 · 失败 {failed} 条")
    print(f"意图准确率（全部成功样本）：{correct}/{total} = {correct / total:.1%}")

    labels = sorted({record["intent"] for record, _ in pairs} | {item["intent"] for _, item in pairs})
    print("\n混淆矩阵（行=人工标注，列=模型判定）")
    width = max(len(label) for label in labels) + 2
    print(" " * width + "".join(label.rjust(width) for label in labels))
    for expected in labels:
        counts = Counter(
            item["intent"] for record, item in pairs if record["intent"] == expected
        )
        line = expected.ljust(width) + "".join(str(counts.get(label, 0)).rjust(width) for label in labels)
        if counts.get(expected, 0) == 0 and sum(counts.values()):
            line += "   <- 该类全部判错"
        print(line)

    print("\n每类表现")
    print(f"{'意图'.ljust(width)}{'精确率'.rjust(10)}{'召回率'.rjust(10)}{'样本'.rjust(8)}")
    for label in labels:
        predicted = [(record, item) for record, item in pairs if item["intent"] == label]
        actual = [(record, item) for record, item in pairs if record["intent"] == label]
        hit = sum(1 for record, item in predicted if record["intent"] == label)
        precision = hit / len(predicted) if predicted else float("nan")
        recall = hit / len(actual) if actual else float("nan")
        print(
            f"{label.ljust(width)}{precision:>10.1%}{recall:>10.1%}{len(actual):>8}"
        )

    print("\n置信度阈值扫描（决定有多少行需要人工复核）")
    print(f"{'阈值'.rjust(8)}{'自动放行'.rjust(10)}{'覆盖率'.rjust(10)}{'放行准确率'.rjust(12)}{'放行中判错'.rjust(12)}")
    recommendation = None
    scan = [step / 20 for step in range(4, 19)]  # 0.20 .. 0.90
    for threshold in scan:
        auto = [(record, item) for record, item in pairs if item["intentConfidence"] >= threshold]
        if not auto:
            continue
        hit = sum(1 for record, item in auto if item["intent"] == record["intent"])
        coverage = len(auto) / total
        precision = hit / len(auto)
        flag = ""
        if coverage >= min_coverage and precision >= target and recommendation is None:
            recommendation = threshold
            flag = "  <- 建议"
        print(
            f"{threshold:>8.2f}{len(auto):>10}{coverage:>10.1%}{precision:>12.1%}{len(auto) - hit:>12}{flag}"
        )
    print(f"\n目标：自动放行部分的准确率 ≥ {target:.0%}，且覆盖率 ≥ {min_coverage:.0%}")
    if recommendation is None:
        print("没有阈值能同时满足目标。要么降低目标，要么把更多样本标成人工复核。")
    else:
        print(f"建议 review_confidence = {recommendation:.2f}（在页面「高级设置」里填写）")
        if recommendation == scan[0]:
            lowest = min(item["intentConfidence"] for _, item in pairs)
            print(
                f"  但这批样本里没有低置信度条目（最低 {lowest:.2f}），"
                f"扫描区间的最低档就已经达标，"
                "说明这个数只是扫描下界，不是真的分界点。"
            )
            print("  想拿到可用的阈值，样本里必须包含反话、讽刺、中英混排、纯表情这类模糊表达。")

    priority_pairs = [(record, item) for record, item in pairs if record.get("priority")]
    if priority_pairs:
        hit = sum(1 for record, item in priority_pairs if item.get("priority") == record["priority"])
        print(f"\n回复优先级准确率：{hit}/{len(priority_pairs)} = {hit / len(priority_pairs):.1%}")

    mistakes = [(record, item) for record, item in pairs if item["intent"] != record["intent"]]
    if mistakes:
        print(f"\n判错的 {len(mistakes)} 条（用来看模型到底错在哪，再决定怎么改 instructions）")
        for record, item in mistakes[:40]:
            confidence = item["intentConfidence"]
            print(
                f"  行{record['line']:>4}  应为 {record['intent']:<16} 判为 {item['intent']:<16} "
                f"置信度 {confidence:.2f}  {item['text'][:44]}"
            )

    uncertain = sorted(pairs, key=lambda pair: pair[1]["intentConfidence"])[:10]
    print("\n模型最不确定的 10 条（这些最可能是反话或语义模糊）")
    for record, item in uncertain:
        print(f"  置信度 {item['intentConfidence']:.2f}  标注 {record['intent']:<16} 判定 {item['intent']:<16} {item['text'][:44]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="用真实标注数据校准评论商机雷达的阈值")
    parser.add_argument("--csv", required=True, help="带标注的 CSV 文件")
    parser.add_argument("--api-key", default=os.environ.get("TYPESAFE_API_KEY", ""))
    parser.add_argument("--model", default=app.DEFAULT_MODEL)
    parser.add_argument("--concurrency", type=int, default=app.DEFAULT_CONCURRENCY)
    parser.add_argument("--target", type=float, default=0.90, help="自动放行部分的目标准确率")
    parser.add_argument("--min-coverage", type=float, default=0.50, help="可接受的最低覆盖率")
    parser.add_argument("--dry-run", action="store_true", help="只检查 CSV，不调用 API")
    args = parser.parse_args()

    path = Path(args.csv)
    if not path.is_file():
        raise SystemExit(f"找不到文件：{path}")
    records = load_dataset(path)
    print(f"读取 {path}：{len(records)} 条标注评论")
    print("类别分布：" + ", ".join(f"{k}={v}" for k, v in sorted(Counter(r['intent'] for r in records).items())))

    if args.dry_run:
        print("\n--dry-run：未调用 API。")
        return

    if not args.api_key:
        raise SystemExit("缺少 API Key：请设置 TYPESAFE_API_KEY 或传 --api-key")

    rows = app.normalize_rows(
        [{"text": record["text"], "source_index": index} for index, record in enumerate(records)]
    )
    options = app.resolve_options({"model": args.model, "concurrency": args.concurrency})

    done = 0
    started = time.time()

    def progress(_item):
        nonlocal done
        done += 1
        print(f"\r  已分析 {done}/{len(rows)}", end="", flush=True)

    results = app.analyze_rows(args.api_key, rows, options, on_result=progress)
    print(f"\r  已分析 {done}/{len(rows)}，用时 {time.time() - started:.1f}s")

    used = Counter(item.get("model", "") for item in results if item.get("model"))
    if used:
        print("实际应答模型：" + ", ".join(f"{name}×{count}" for name, count in used.items()))
        if len(used) > 1:
            print("  警告：一次评测里出现了多个模型版本，结果不可比。")

    errors = [item for item in results if item.get("error")]
    for item in errors[:5]:
        print(f"  失败样本：{item['error']}")
    if len(errors) > 5:
        print(f"  另有 {len(errors) - 5} 条失败")

    accuracy_report(records, results, args.target, args.min_coverage)


if __name__ == "__main__":
    main()
