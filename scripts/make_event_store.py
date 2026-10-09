#!/usr/bin/env python3
"""生成本地事件存储（**启动工具**，不是产品能力）。

用途
----
让 REPLAY / PAPER 在**无凭据、无网络**的情况下可以直接启动：产出一个确定性的
本地 event store（真实格式：`storage/events/writer.py` 的 JSONL 契约）。

数据来源
--------
`tests/ui/market_fixture.py` —— 确定性合成行情（L2 book / aggressor trade / MARK_PRICE）。
**它是试验数据，不是真实市场证据**：不得用于任何"真实行情"结论。

边界
----
- 只读取/写入你指定的路径；不触碰 run registry、不启动 runtime、不下任何单。
- 真实行情数据需要另行录制（`storage/events/writer.py::JsonlEventWriter`），
  本脚本不提供任何交易所访问能力。

用法
----
    python3 scripts/make_event_store.py ~/.probex/local-run/events.jsonl --hours 3
    python3 scripts/make_event_store.py /tmp/x.jsonl --hours 1 --no-mark --force
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_event_store.py",
        description="生成本地 REPLAY/PAPER 用的确定性 event store（LOCAL TRIAL 试验数据）")
    parser.add_argument("path", help="event store 输出路径（JSONL）")
    parser.add_argument("--hours", type=int, default=3, help="合成时长（小时，默认 3）")
    parser.add_argument("--step-ms", type=int, default=None,
                        help="事件步长（毫秒；默认用 fixture 的默认值）")
    parser.add_argument("--seed", type=int, default=7, help="确定性随机种子（默认 7）")
    parser.add_argument("--mark-price", type=float, default=60_000.0,
                        help="MARK_PRICE 参考价（默认 60000；Risk 需要 mark 才会报价）")
    parser.add_argument("--no-mark", action="store_true",
                        help="不生成 MARK_PRICE 事件（用于验证 '无 mark ⇒ fail closed' 的路径）")
    parser.add_argument("--force", action="store_true", help="已存在也覆盖重建")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.hours <= 0:
        print("error: --hours must be a positive integer", file=sys.stderr)
        return 2
    target = pathlib.Path(args.path).expanduser()
    if target.exists() and not args.force:
        print(json.dumps({"event": "event_store_exists", "path": str(target),
                          "skipped": True, "hint": "--force to rebuild"}, ensure_ascii=False))
        return 0

    from tests.ui.market_fixture import write_market_store

    target.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {"hours": args.hours, "seed": args.seed,
                                "mark_price": None if args.no_mark else args.mark_price}
    if args.step_ms is not None:
        kwargs["step_ms"] = args.step_ms
    events, expected = write_market_store(target, **kwargs)          # type: ignore[arg-type]
    print(json.dumps({"event": "event_store_written", "path": str(target),
                      "events": events, "expected_market_events": expected,
                      "hours": args.hours, "mark_price": None if args.no_mark else args.mark_price,
                      "data_source": "tests/ui/market_fixture.py (synthetic LOCAL TRIAL data)",
                      "note": "synthetic data: not real market evidence"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
