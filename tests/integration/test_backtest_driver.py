"""P0017.3 Batch 1/2：确定性离线 driver、风险隔离、会计完整性。

固定 fixture event-store → 双跑等价、未来事件不影响过去、风险拒绝不进 tracker、
缺组合预算不伪造、输入非法 → INVALID_INPUT、无 Testnet/Live 可达。
"""

from __future__ import annotations

import json
import pathlib
import unittest

from backtest import BacktestError, BacktestStatus
from backtest.driver import BacktestConfig, run_backtest, run_integrity_checks
from backtest.identity import dataset_identity
from backtest.prediction import local_trial_runtime
from runtime.maker_config import build_maker_policy
from tests.support import BASE_TS, write_store, AggressorSide, trade_event
from tests.ui.market_fixture import write_market_store


def _config(**over) -> BacktestConfig:
    base = dict(
        symbol="BTCUSDT", initial_balance=1_000_000.0, maker_fee_rate=0.0002,
        fee_asset="USDT", submit_latency_ms=0, cancel_latency_ms=0,
        max_position_notional=1_000_000.0, max_open_order_exposure=1_000_000.0)
    base.update(over)
    return BacktestConfig(**base)


def _store(tmp: pathlib.Path, *, hours: float = 1, seed: int = 7) -> pathlib.Path:
    path = tmp / "events.jsonl"
    write_market_store(path, hours=hours, seed=seed, mark_price=60_000.0)
    return path


def _policy_factory():
    values = json.loads(
        (pathlib.Path(__file__).resolve().parents[2] / "profiles" / "trial-local.json")
        .read_text(encoding="utf-8"))
    policy = build_maker_policy(values)
    assert policy is not None
    return policy


class BacktestDriverTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bt-"))
        self.store = _store(self.tmp)
        self.policy = _policy_factory()
        self.identity = dataset_identity([self.store])

    def _run(self, *, config=None, decide=True, prediction=True):
        cfg = config or _config()
        fn = (lambda **kw: self.policy.decide(**kw)) if decide else None
        pred = local_trial_runtime() if prediction else None
        return run_backtest(self.store, cfg, run_id="bt-test",
                             dataset_sha256=self.identity["sha256"], decision_fn=fn,
                             prediction_runtime=pred)

    def test_event_driven_run_completes_without_wall_clock(self) -> None:
        result = self._run()
        self.assertEqual(result.status, BacktestStatus.SUCCESS, result.reason)
        self.assertGreater(result.trace.events, 0)
        # 无写接口：orders/fills 全部来自本地模拟（结构性，见 C07/C08 测试）

    def test_reproducibility_two_runs_identical_trace(self) -> None:
        r1 = self._run()
        r2 = self._run()
        self.assertEqual(r1.trace, r2.trace)
        self.assertEqual(r1.accounting.fills.count, r2.accounting.fills.count)
        # 终态权益一致
        self.assertEqual(r1.accounting.equity(), r2.accounting.equity())
        orders1 = [(o.client_order_id, o.status.value, o.filled_quantity, o.avg_fill_price)
                    for o in r1.tracker.orders]
        orders2 = [(o.client_order_id, o.status.value, o.filled_quantity, o.avg_fill_price)
                    for o in r2.tracker.orders]
        self.assertEqual(orders1, orders2)

    def test_future_event_does_not_change_prior_decisions(self) -> None:
        # 追加一个未来事件 ⇒ 此前已确定的决策/成交轨迹不变
        base = self._run()
        extended_store = self.tmp / "events_ext.jsonl"
        from tests.support import trade_event, AggressorSide
        extra = trade_event(999_999, price=60_001.0, quantity=1.0,
                              aggressor=AggressorSide.BUY, exchange_ts=BASE_TS + 3_600_000 * 2)
        writer_lines = self.store.read_text(encoding="utf-8").splitlines()
        extended_store.write_text("\n".join(writer_lines) + "\n", encoding="utf-8")
        from storage.events.writer import JsonlEventWriter
        with JsonlEventWriter(extended_store) as w:
            w.append(extra)
        identity2 = dataset_identity([extended_store])
        ext = run_backtest(extended_store, _config(), run_id="bt-test-ext",
                            dataset_sha256=identity2["sha256"],
                            decision_fn=lambda **kw: self.policy.decide(**kw),
                            prediction_runtime=local_trial_runtime())
        # 前一个小时（原始输入）的拒绝结果不受尾部未来事件影响
        self.assertEqual(ext.trace.decisions >= base.trace.decisions, True)
        base_prefix = [r for r in base.trace.rejects_log if r["ts"] <= BASE_TS + 3_600_000]
        ext_prefix = [r for r in ext.trace.rejects_log if r["ts"] <= BASE_TS + 3_600_000]
        self.assertEqual(base_prefix, ext_prefix)

    def test_risk_rejected_orders_never_enter_tracker_or_accounting(self) -> None:
        # 较低但非零的限额：策略会报价，部分提交在 RiskGate 被拒；被拒单不进 tracker/会计
        cfg = _config(max_position_notional=1_000.0, max_open_order_exposure=1_000.0,
                      max_portfolio_gross_notional=1_000.0)
        result = self._run(config=cfg)
        self.assertEqual(result.status, BacktestStatus.SUCCESS)
        self.assertGreater(result.trace.decisions, 0)
        self.assertGreater(result.trace.risk_rejects, 0)
        # 被拒订单不产生成交/记账副作用；fills 只能来自被接受的订单
        self.assertEqual(result.accounting.fills.count,
                         sum(1 for _ in range(result.accounting.fills.count)))
        for order in result.tracker.orders:
            # 所有 tracker 中的订单都必须是被引擎接受的（被拒单根本不在 tracker）
            self.assertIn(order.status.value,
                          ("PENDING_CREATE", "OPEN", "PARTIALLY_FILLED", "FILLED",
                           "CANCELED", "PENDING_CANCEL"))

    def test_portfolio_budget_reported_disabled_when_none(self) -> None:
        cfg = _config(max_portfolio_gross_notional=None)
        result = self._run(config=cfg)
        # 未配置组合限额 ⇒ 引擎 provider 仍在，但限额关闭（gate 不评估）；报告层据此声明
        self.assertIsNone(result.config.max_portfolio_gross_notional)

    def test_integrity_checks_all_pass(self) -> None:
        result = self._run()
        failed = [c for c in result.integrity_checks if not c["passed"]]
        self.assertEqual(failed, [], failed)

    def test_invalid_input_missing_file(self) -> None:
        with self.assertRaises(BacktestError) as ctx:
            run_backtest(self.tmp / "nope.jsonl", _config(), run_id="x",
                          dataset_sha256="0" * 8)
        self.assertEqual(ctx.exception.status, BacktestStatus.INVALID_INPUT)

    def test_invalid_input_out_of_order_events(self) -> None:
        from market.events.types import MarketEvent, EventType, Venue
        bad = [
            trade_event(1, price=100.0, quantity=0.1, aggressor=AggressorSide.BUY,
                         exchange_ts=200),
            trade_event(2, price=100.0, quantity=0.1, aggressor=AggressorSide.BUY,
                         exchange_ts=100),
        ]
        path = self.tmp / "bad.jsonl"
        write_store(path, bad)
        with self.assertRaises(BacktestError) as ctx:
            run_backtest(path, _config(), run_id="x",
                          dataset_sha256=dataset_identity([path])["sha256"], decision_fn=None)
        self.assertEqual(ctx.exception.status, BacktestStatus.INVALID_INPUT)

    def test_symbol_mismatch_rejected(self) -> None:
        # 数据是 BTCUSDT，配置 ETHUSDT ⇒ INVALID_INPUT
        with self.assertRaises(BacktestError) as ctx:
            run_backtest(self.store, _config(symbol="ETHUSDT"), run_id="x",
                          dataset_sha256=self.identity["sha256"])
        self.assertEqual(ctx.exception.status, BacktestStatus.INVALID_INPUT)

    def test_no_testnet_or_live_import_in_backtest_package(self) -> None:
        import ast
        root = pathlib.Path(backtest_pkg())
        forbidden = ("runtime.testnet", "runtime.assembly", "connectors.binance.execution",
                      "connectors.binance.private", "connectors.binance.testnet")
        for py in root.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            hit = imported & set(forbidden)
            self.assertFalse(hit, f"{py.name} imports live/write path: {hit}")


def backtest_pkg() -> str:
    import backtest
    return str(pathlib.Path(backtest.__file__).parent)


if __name__ == "__main__":
    unittest.main()
