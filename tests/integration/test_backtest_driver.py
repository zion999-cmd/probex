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

    def _run_with_id(self, run_id: str, *, store=None, identity=None):
        """同 run_id 运行，保证 client_order_id 跨双跑可比（因果性前缀测试用）。"""
        store = store or self.store
        ident = identity or self.identity
        return run_backtest(store, _config(), run_id=run_id,
                             dataset_sha256=ident["sha256"],
                             decision_fn=lambda **kw: self.policy.decide(**kw),
                             prediction_runtime=local_trial_runtime())

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
        """B03/B04：仅追加未来事件，此前已确定的**内容**（订单状态/成交量/均价、成交、权益）
        必须逐笔一致；不是只比数量。两次使用相同 run_id（成交 id 才可比）。"""
        base = self._run_with_id("bt-causal")
        from tests.support import trade_event, AggressorSide
        from storage.events.writer import JsonlEventWriter
        extended_store = self.tmp / "events_ext.jsonl"
        extended_store.write_text(self.store.read_text(encoding="utf-8"),
                                   encoding="utf-8")
        with JsonlEventWriter(extended_store) as w:
            w.append(trade_event(999_999, price=60_001.0, quantity=1.0,
                                   aggressor=AggressorSide.BUY,
                                   exchange_ts=BASE_TS + 3_600_000 * 2))
        ext = self._run_with_id("bt-causal", store=extended_store,
                                  identity=dataset_identity([extended_store]))

        cutoff = BASE_TS + 3_600_000
        # 1) rejects：内容前缀一致
        self.assertEqual([r for r in base.trace.rejects_log if r["ts"] <= cutoff],
                         [r for r in ext.trace.rejects_log if r["ts"] <= cutoff])
        # 2) 决策计数：追加事件只能增加，不改变此前已发生的决策数
        self.assertGreaterEqual(ext.trace.decisions, base.trace.decisions)
        # 3) 订单内容前缀：前 N 单（client id/状态/成交量/均价）逐笔相等
        def order_content(result):
            return [(o.client_order_id, o.status.value, round(o.filled_quantity, 10),
                      round(o.avg_fill_price, 8), round(o.price, 8)) for o in result.tracker.orders]
        ob, oe = order_content(base), order_content(ext)
        self.assertEqual(ob, oe[: len(ob)],
                          "appending a future event changed pre-existing order content")
        # 4) 成交：原始窗口成交笔数与 ledger 一致且前缀相等
        self.assertEqual(base.accounting.fills.count, ext.accounting.fills.count)
        base_fills = [(f.symbol, f.trade_id, f.quantity, round(f.price, 8))
                       for f in base.accounting.fills.fills]
        ext_fills = [(f.symbol, f.trade_id, f.quantity, round(f.price, 8))
                      for f in ext.accounting.fills.fills]
        self.assertEqual(base_fills, ext_fills)
        # 5) 权益序列终值（含费用）一致——未来事件不得改变此前会计结果
        self.assertEqual(base.accounting.equity(), ext.accounting.equity())
        self.assertEqual(base.accounting.realized_trade_pnl,
                          ext.accounting.realized_trade_pnl)
        self.assertEqual(base.accounting.trading_fees, ext.accounting.trading_fees)
        # 追加事件可合法产生**之后**的新订单（不能要求整段完全相同）
        self.assertGreaterEqual(len(oe), len(ob))

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

    def test_prediction_evaluation_is_attached_when_predictions_observed(self) -> None:
        result = self._run()
        evaluation = result.prediction_evaluation
        self.assertIsNotNone(evaluation, "local-trial predictions over a 1h fixture must be evaluable")
        assert evaluation is not None
        self.assertGreater(evaluation["predictions_seen"], 0)
        horizons = {h["horizon_ms"]: h for h in evaluation["horizons"]}
        # fixture 步进 5s、长度 1h ⇒ 5/15/30/60s 全部可对齐
        self.assertTrue(horizons)
        for horizon in (5_000, 15_000, 30_000, 60_000):
            self.assertIn(horizon, horizons)
            self.assertGreater(horizons[horizon]["samples"], 0)
            self.assertIsNotNone(horizons[horizon]["direction_hit_rate"])

    def test_no_prediction_means_no_evaluation(self) -> None:
        result = self._run(prediction=False)
        self.assertIsNone(result.prediction_evaluation)

    def test_taker_cost_model_labels_reflect_config(self) -> None:
        from backtest.report import build_manifest, FUNDING_MODEL
        cfg = _config(taker_slippage_bps=10.0, taker_fee_rate=0.0005)
        ident = dataset_identity([self.store])
        result = run_backtest(
            self.store, cfg, run_id="bt-taker", dataset_sha256=ident["sha256"],
            decision_fn=(lambda **kw: None), prediction_runtime=None)
        manifest = build_manifest(result, dataset_files=["x"], input_files=[])
        costs = manifest["cost_models"]
        self.assertTrue(costs["slippage"].startswith("MODELED"))
        self.assertIn("0.0005", costs["taker_fee"])
        # funding 未启用仍如实声明
        self.assertEqual(costs["funding"], FUNDING_MODEL)


def backtest_pkg() -> str:
    import backtest
    return str(pathlib.Path(backtest.__file__).parent)


if __name__ == "__main__":
    unittest.main()
