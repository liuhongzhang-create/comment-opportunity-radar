#!/usr/bin/env python3
"""抓取抖音作品评论，输出成分析流程直接能吃的 CSV。

用法（需要带 Playwright 的解释器，即 bash tools/setup-collector.sh 建好的 .venv）：

    python3 tools/radar-collect.py doctor                 # 诊断环境、登录态、抓取通路
    python3 tools/radar-collect.py login                  # 打开浏览器扫码登录，状态会保存
    python3 tools/radar-collect.py works                  # 列出自己账号的作品
    python3 tools/radar-collect.py collect --aweme-id XXX --out comments.csv
    python3 tools/radar-collect.py collect --all --out comments.csv --max 300

抓完的 CSV 可以直接拖进网页界面（也可以单独使用）。

请注意：本工具用你自己的账号、读你自己的数据，但自动化操作本身处于抖音服务条款的
灰色地带，节奏过快可能触发风控。默认节奏已经放得很慢，请不要改成高频轮询。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import douyin  # noqa: E402


def _progress(payload: dict) -> None:
    if payload.get("type") == "progress":
        print(f"\r  已抓 {payload['count']} 条", end="", flush=True)
    elif payload.get("type") == "work":
        print(f"\n→ 开始抓作品 {payload['awemeId']}")
    elif payload.get("type") == "error":
        print(f"\n  作品 {payload['awemeId']} 失败：{payload['error']}")


def cmd_doctor(args: argparse.Namespace) -> int:
    print("=== 环境 ===")
    info = douyin.probe_environment()
    for key, value in info.items():
        print(f"  {key}: {value}")
    if not info.get("playwright"):
        print("\n请先运行：bash tools/setup-collector.sh")
        return 1

    print("\n=== 浏览器与账号 ===")
    collector = douyin.DouyinCollector(headless=False)
    try:
        collector.open_login()
        print(f"  登录态：{'已登录' if collector.logged_in() else '未登录'}")
        if collector.logged_in():
            print(f"  账号昵称：{collector.whoami() or '(未取到)'}")
        print("\n=== 匿名抓取通路（不需要登录）===")
        ids = collector.hot_video_ids(limit=1)
        print(f"  热点榜取到视频 ID：{ids}")
        if ids:
            comments = collector.collect_comments(ids[0], max_comments=30, max_scrolls=8)
            print(f"  该视频抓到评论：{len(comments)} 条")
            for comment in comments[:5]:
                print(f"    {comment.nickname} | {comment.text[:40]} | 赞{comment.digg_count}")
    except douyin.CollectorError as exc:
        print(f"  失败：{exc}")
        return 1
    finally:
        collector.stop()
    print("\n诊断结束。")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    collector = douyin.DouyinCollector(headless=False)
    try:
        collector.open_login()
        if collector.logged_in():
            print(f"已经处于登录状态：{collector.whoami() or '(昵称未取到)'}")
            return 0
        print("请在打开的浏览器窗口里用抖音 App 扫码登录……")
        if collector.wait_for_login(timeout=args.timeout):
            print(f"登录成功：{collector.whoami() or '(昵称未取到)'}")
            print(f"登录状态已保存到 {collector.profile_dir}，下次不用再扫码。")
            return 0
        print("等待超时，未检测到登录。请重试。")
        return 1
    except douyin.CollectorError as exc:
        print(f"失败：{exc}")
        return 1
    finally:
        collector.stop()


def cmd_works(args: argparse.Namespace) -> int:
    collector = douyin.DouyinCollector(headless=False)
    try:
        works = collector.list_works(limit=args.limit)
    except douyin.CollectorError as exc:
        print(f"失败：{exc}")
        return 1
    finally:
        collector.stop()
    if not works:
        print("没有取到作品。确认已登录，且账号下确实有已发布的公开作品。")
        return 1
    print(f"共 {len(works)} 个作品：")
    for work in works:
        created = douyin.format_time(work.create_time)
        print(f"  {work.aweme_id}  评论{work.comment_count:>5}  赞{work.digg_count:>6}  {created}  {work.title}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    collector = douyin.DouyinCollector(headless=False)
    works_by_id: dict[str, douyin.Work] = {}
    try:
        if args.all:
            print("先取作品列表……")
            for work in collector.list_works(limit=args.limit):
                works_by_id[work.aweme_id] = work
            targets = list(works_by_id)
        elif args.aweme_id:
            targets = list(args.aweme_id)
        else:
            print("请指定 --aweme-id，或加 --all 抓取全部作品。")
            return 2
        if not targets:
            print("没有可抓取的作品。")
            return 1

        collected: list[douyin.Comment] = []
        for aweme_id in targets:
            print(f"抓取 {aweme_id} ……")
            try:
                comments = collector.collect_comments(
                    aweme_id,
                    max_comments=args.max,
                    include_replies=args.replies,
                    on_progress=_progress,
                )
            except douyin.CollectorError as exc:
                print(f"\n  失败：{exc}")
                continue
            print(f"\r  抓取完成：{len(comments)} 条")
            collected.extend(comments)
    finally:
        collector.stop()

    deduped = douyin.dedupe_comments(collected)
    analyzable, skipped_no_text = douyin.split_analyzable(deduped)
    if skipped_no_text:
        print(f"跳过 {skipped_no_text} 条纯图片评论（没有文字，无法分析）。")
    if not analyzable:
        print("没有抓到任何可分析的评论。")
        return 1
    rows = douyin.comment_rows(analyzable, works_by_id)
    out = Path(args.out)
    out.write_text(douyin.to_csv_text(rows, douyin.CSV_HEADER), encoding="utf-8-sig")
    print(f"已写入 {out}：{len(analyzable)} 条评论")
    print("可以直接拖进网页界面分析，或配合 tools/calibrate.py 做人工标注校准。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="抓取抖音作品评论区，输出可分析的 CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="诊断环境、登录态与抓取通路")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("login", help="扫码登录并保存登录状态")
    p.add_argument("--timeout", type=float, default=240.0, help="等待扫码的秒数")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("works", help="列出自己账号的作品")
    p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_works)

    p = sub.add_parser("collect", help="抓取指定作品（或全部作品）的评论")
    p.add_argument("--aweme-id", action="append", default=[], help="作品 ID，可重复传")
    p.add_argument("--all", action="store_true", help="抓取自己账号下全部作品")
    p.add_argument("--limit", type=int, default=60, help="配合 --all 时限制作品数")
    p.add_argument("--max", type=int, default=500, help="每个作品最多抓多少条评论")
    p.add_argument("--replies", action="store_true", help="尽量包含二级回复（实验特性）")
    p.add_argument("--out", default="douyin-comments.csv", help="输出文件路径")
    p.set_defaults(func=cmd_collect)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
