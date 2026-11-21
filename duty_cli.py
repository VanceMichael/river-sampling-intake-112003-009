"""值班收件命令行：连上监测站、机箱无线页和调度台，持续给出能证明的采样。

    python3 duty_cli.py watch --source http://127.0.0.1:8040 --step 09:10:00 09:12:00 09:14:00
    python3 duty_cli.py watch --source http://127.0.0.1:8040 --every 30 --until 10:00:00
    python3 duty_cli.py now   --source http://127.0.0.1:8040 --event STORM7 --json
    python3 duty_cli.py report --source http://127.0.0.1:8040 --event STORM7

``watch`` 会把演练服务的时钟按给定时刻依次前拨（或按 --every 秒真实等待后前拨），
每拨一次收一轮资料再打印；``now`` 只读当前时刻，不拨钟。
"""
from __future__ import annotations

import json
import sys
import time
from argparse import ArgumentParser

from duty_desk import Desk, Source, TransportError, seconds, show, summarize


def build(args) -> Desk:
    return Desk(Source(args.source, timeout=args.timeout))


def emit(desk: Desk, event: str | None, as_json: bool) -> None:
    report = desk.report(event)
    if as_json:
        # 每轮一行，便于下游逐条读；轮次之间不会拼成非法 JSON。
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    else:
        print(summarize(report))
        print()


def run_times(args) -> int:
    desk = build(args)
    try:
        desk.tick(pull=True)
        emit(desk, args.event, args.json)
        for moment in args.at:
            desk.tick(moment)
            emit(desk, args.event, args.json)
    except (TransportError, ValueError) as error:
        print(f"收件失败：{error}", file=sys.stderr)
        return 2
    return 0


def run_every(args) -> int:
    desk = build(args)
    limit = seconds(args.until)
    try:
        desk.tick(pull=True)
        emit(desk, args.event, args.json)
        while seconds(desk.server_time) < limit:
            time.sleep(args.every)
            current = seconds(desk.source.now())
            if current >= limit:
                break
            desk.tick(show(current))
            emit(desk, args.event, args.json)
    except (TransportError, ValueError) as error:
        print(f"收件失败：{error}", file=sys.stderr)
        return 2
    return 0


def run_now(args) -> int:
    desk = build(args)
    try:
        desk.tick(pull=True)
        emit(desk, args.event, args.json)
    except (TransportError, ValueError) as error:
        print(f"收件失败：{error}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="http://127.0.0.1:8040", help="演练服务地址")
    parser.add_argument("--timeout", type=float, default=2.0)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (("watch", "按给定时刻依次前拨并出报告"),
                            ("now", "只读当前时刻出报告"),
                            ("report", "与 now 相同，输出值班员按事件查询的格式")):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--event", default=None, help="只看某一涨水事件")
        item.add_argument("--json", action="store_true")
        if name == "watch":
            item.add_argument("--step", dest="at", nargs="+", default=[],
                              help="要前拨到的时刻，如 09:10:00 09:12:00")
            item.add_argument("--every", type=float, default=None,
                              help="按真实秒数等待后再前拨")
            item.add_argument("--until", default="10:00:00")

    args = parser.parse_args(argv)
    if args.command == "watch":
        if getattr(args, "every", None):
            return run_every(args)
        return run_times(args)
    return run_now(args)


if __name__ == "__main__":
    raise SystemExit(main())
