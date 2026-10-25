"""回测直接消费 P0017.1 共享市场数据集目录（D-055 批次1 切片3，集成等价性）。

- 文件 event-store 与数据集目录两种输入产出等价结果（事件数 / 成交数 / 订单数 / 会计事实）；
- 多标的数据集按 `<venue>/<symbol>/<kind>` 自动发现；
- execute_backtest 端到端接受目录输入（SUCCESS + 产物）；
- 双跑可复现（run_id 只取决于输入内容）。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from backtest.dataset_source import load_records
from backtest.driver import BacktestConfig, run_backtest
from backtest.identity import identity_for_input
from backtest.multi_driver import MultiBacktestConfig, run_multi_backtest
from backtest.prediction import local_trial_runtime
from backtest.runner import execute_backtest
from market.events.types import EventType
from runtime.maker_config import build_maker_policy
from storage.events.codec import encode_event
from storage.events.reader import JsonlEventReader
from storage.market_dataset import MarketDatasetWriter
from tests.ui.market_fixture import write_market_store

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
KIND_FOR_EVENT = {
    EventType.BOOK_SNAPSHOT: "book",
    EventType.BOOK_DELTA: "book",
    EventType.TRADE: "trade",
    EventType.MARK_PRICE: "mark",
}


def write_dataset_from_store(store: pathlib.Path, root: pathlib.Path,
                                *, symbols: tuple[str, ...] | None = None) -> None:
    """按 store 到达顺序为每个 symbol 编全局采集序号（跨 kind），同毫秒可复现真实顺序。"""
    writers: dict[tuple[str, str], MarketDatasetWriter] = {}
    counters: dict[str, int] = {}
    try:
        for record in JsonlEventReader(store):
            event = record.event
            if symbols is not None and event.symbol not in symbols:
                continue
            ordinal = counters.get(event.symbol, 0)
            counters[event.symbol] = ordinal + 1
            kind = KIND_FOR_EVENT[event.event_type]
            key = (event.symbol, kind)
            writer = writers.get(key)
            if writer is None:
                writer = MarketDatasetWriter(
                    root, venue=event.venue.value, symbol=event.symbol, kind=kind,
                    source="event-store-import")
                writers[key] = writer
            writer.append(exchange_ts=int(event.exchange_ts),
                            receive_ts=int(event.receive_ts),
                            payload=encode_event(event), ordinal=ordinal)
    finally:
        for writer in writers.values():
            writer.close()


def _single_config() -> BacktestConfig:
    return BacktestConfig(
        symbol="BTCUSDT", initial_balance=1_000_000.0, maker_fee_rate=0.0002,
        fee_asset="USDT", submit_latency_ms=0, cancel_latency_ms=0,
        max_position_notional=1_000_000.0, max_open_order_exposure=1_000_000.0)


def _policy():
    values = json.loads((PROJECT_ROOT / "profiles" / "trial-local.json").read_text(encoding="utf-8"))
    policy = build_maker_policy(values)
    assert policy is not None
    return policy


class DatasetInputEquivalenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-ds-e2e-"))
        self.store = self.tmp / "events.jsonl"
        write_market_store(self.store, hours=1, seed=7, mark_price=60_000.0)
        self.dataset_dir = self.tmp / "dataset"
        write_dataset_from_store(self.store, self.dataset_dir, symbols=("BTCUSDT",))

    def test_single_symbol_dataset_and_file_equivalent(self) -> None:
        cfg = _single_config()
        policy = _policy()
        ident_file = identity_for_input(self.store)
        ident_dir = identity_for_input(self.dataset_dir)
        self.assertNotEqual(ident_file["sha256"], ident_dir["sha256"])  # 编码不同，身份不同
        common = dict(run_id="bt-eq", decision_fn=lambda **kw: policy.decide(**kw),
                      prediction_runtime=local_trial_runtime())
        file_result = run_backtest(self.store, cfg, dataset_sha256=ident_file["sha256"], **common)
        dir_result = run_backtest(self.dataset_dir, cfg, dataset_sha256=ident_dir["sha256"], **common)

        self.assertEqual(file_result.trace.events, dir_result.trace.events)
        self.assertEqual(file_result.status, dir_result.status)
        self.assertEqual(file_result.trace.submits, dir_result.trace.submits)
        self.assertEqual(file_result.trace.fills, dir_result.trace.fills)
        self.assertEqual(len(file_result.accounting.fills.fills),
                          len(dir_result.accounting.fills.fills))
        self.assertAlmostEqual(file_result.accounting.equity(), dir_result.accounting.equity(), places=6)
        self.assertAlmostEqual(file_result.accounting.balance, dir_result.accounting.balance, places=6)

    def test_dataset_loader_returns_all_events_sorted(self) -> None:
        records = load_records(self.dataset_dir)
        timestamps = [r.event.exchange_ts for r in records]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(len(records), len(tuple(JsonlEventReader(self.store))))

    def test_execute_backtest_writes_artifacts_for_dataset_dir(self) -> None:
        values = json.loads(
            (PROJECT_ROOT / "profiles" / "backtest-default.json").read_text(encoding="utf-8"))
        values["backtest.symbol"] = "BTCUSDT"
        values["backtest.seed"] = 7
        config_path = self.tmp / "config.json"
        config_path.write_text(json.dumps(values), encoding="utf-8")
        out = self.tmp / "out"
        summary = execute_backtest(dataset=self.dataset_dir, config_path=config_path,
                                     out_dir=out, prediction=True)
        self.assertEqual(summary["status"], "SUCCESS")
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["dataset"]["files"])
        self.assertTrue(any(str(p).endswith("data.jsonl")
                              for p in manifest["dataset"]["files"]))
        # 双跑：同内容数据集 ⇒ 同 run_id
        summary_2 = execute_backtest(dataset=self.dataset_dir, config_path=config_path,
                                        out_dir=self.tmp / "out2", prediction=True)
        self.assertEqual(summary["run_id"], summary_2["run_id"])


class MultiDatasetInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-multi-ds-"))
        self.store = self.tmp / "events.jsonl"
        # 复用多标的夹具的构造方式
        from tests.integration.test_multi_symbol_backtest import (
            _build_symbol_events, _merge_time_order)
        from tests.support import write_store

        btc = _build_symbol_events("BTCUSDT", base_price=60_000, tick=0.1, steps=120,
                                     seed=11, mark_price=60_000.0)
        eth = _build_symbol_events("ETHUSDT", base_price=3_000, tick=0.01, steps=120,
                                     seed=22, mark_price=3_000.0)
        write_store(self.store, _merge_time_order(btc, eth))
        self.dataset_dir = self.tmp / "dataset"
        write_dataset_from_store(self.store, self.dataset_dir,
                                   symbols=("BTCUSDT", "ETHUSDT"))

    def _config(self) -> MultiBacktestConfig:
        return MultiBacktestConfig(
            symbol="BTCUSDT",
            symbols=("BTCUSDT", "ETHUSDT"), initial_balance=10_000_000.0,
            maker_fee_rate=0.0002, fee_asset="USDT", submit_latency_ms=0,
            cancel_latency_ms=0, max_position_notional=10_000_000.0,
            max_portfolio_gross_notional=10_000_000.0,
            max_open_order_exposure=10_000_000.0,
            tick_size_by_symbol={"BTCUSDT": 0.1, "ETHUSDT": 0.01},
            quantity_step_by_symbol={"BTCUSDT": 0.0001, "ETHUSDT": 0.0001})

    def test_multi_symbol_dataset_and_file_equivalent(self) -> None:
        from tests.integration.test_multi_symbol_backtest import _maker_values

        values_btc = _maker_values(0.1, 0.0001)
        values_eth = _maker_values(0.01, 0.0001)
        policies = {}
        for sym, values in (("BTCUSDT", values_btc), ("ETHUSDT", values_eth)):
            policy = build_maker_policy(values)
            assert policy is not None
            policies[sym] = policy
        cfg = self._config()

        def _decide_for(policy):
            def decide(**kw):
                return policy.decide(**kw)
            return decide

        decisions = {s: _decide_for(policies[s]) for s in ("BTCUSDT", "ETHUSDT")}
        common = dict(run_id="bt-multi-eq", decision_fn=decisions,
                      prediction_runtime=local_trial_runtime())
        file_result = run_multi_backtest(
            self.store, cfg, dataset_sha256=identity_for_input(self.store)["sha256"], **common)
        dir_result = run_multi_backtest(
            self.dataset_dir, cfg,
            dataset_sha256=identity_for_input(
                self.dataset_dir, symbols=("BTCUSDT", "ETHUSDT"))["sha256"], **common)
        self.assertEqual(file_result.trace.events, dir_result.trace.events)
        self.assertEqual(file_result.trace.fills, dir_result.trace.fills)
        self.assertEqual(file_result.trace.submits, dir_result.trace.submits)


if __name__ == "__main__":
    unittest.main()
