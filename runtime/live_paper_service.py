"""P0017.5：实时多标的**模拟**交易的产品启动服务（composition root for L2）。

把 P0017.1 只读采集器（`MultiSymbolMarketCollector`）经 `CollectorBatchSource` 接到
`LiveMultiSymbolRunner`。执行侧固定为 `MultiSymbolSimulatedAdapter`（模拟，无真实下单）。

本模块不 import `runtime.testnet` 或任何 binance 写 connector（AST 守卫覆盖）；
它只负责"从配置构造对象 + SIGTERM/SIGINT 有序停止 + 周期性可观察状态"。
"""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path
from typing import TextIO

from connectors.binance.market_data.multi_collector import (
    CollectorConfig,
    MultiSymbolMarketCollector,
)
from runtime.live_collector_source import CollectorBatchSource
from runtime.live_multi_symbol import LiveMultiSymbolRunner, LiveRiskConfig
from runtime.maker_config import build_maker_policy


def _symbols(values: dict[str, object]) -> tuple[str, ...]:
    raw = values.get("live.symbols") or values.get("backtest.symbols") or values.get("market.universe.symbols")
    if isinstance(raw, str):
        syms = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    elif isinstance(raw, (list, tuple)):
        syms = tuple(str(s).upper() for s in raw)
    else:
        syms = ()
    if len(syms) < 2:
        raise ValueError("real-time multi-symbol paper requires >=2 symbols (live.symbols)")
    if len(set(syms)) != len(syms):
        raise ValueError("duplicate symbols in live.symbols")
    return syms


def _risk(values: dict[str, object]) -> LiveRiskConfig:
    def num(key: str):
        return float(values[key]) if key in values else None

    for key in ("backtest.initial_balance", "backtest.maker_fee_rate", "backtest.fee_asset"):
        if key not in values:
            raise ValueError(f"missing required config key: {key}")
    return LiveRiskConfig(
        initial_balance=float(values["backtest.initial_balance"]),
        maker_fee_rate=float(values["backtest.maker_fee_rate"]),
        fee_asset=str(values["backtest.fee_asset"]),
        submit_latency_ms=int(float(values.get("backtest.submit_latency_ms", 0))),
        cancel_latency_ms=int(float(values.get("backtest.cancel_latency_ms", 0))),
        max_position_notional=num("risk.max_position_notional"),
        max_portfolio_gross_notional=num("risk.max_portfolio_gross_notional"),
        max_open_order_exposure=num("risk.max_open_order_exposure"),
        max_daily_loss=num("risk.max_daily_loss"),
        max_drawdown_pct=num("risk.max_drawdown_pct"),
        max_book_age_ms=int(values["risk.max_book_age_ms"]) if "risk.max_book_age_ms" in values else 5_000)


def build_live_paper_runner(values: dict[str, object]) -> LiveMultiSymbolRunner:
    """从配置字典构造实时多标的模拟 runner（尚未启动；不触发网络直到 run）。"""
    symbols = _symbols(values)
    rest_base = str(values.get("market.public_rest_base", "https://fapi.binance.com"))
    ws_host = str(values.get("market.public_ws_host", "wss://fstream.binance.com"))
    max_symbols = int(values.get("market.subscription.max_symbols", 16))

    # 先做全部**不触网**的配置/风险/策略校验（fail fast，配置错误不触发连接）。
    risk = _risk(values)
    policies: dict[str, object] = {}
    tick_table = values.get("backtest.tick_size_by_symbol") or {}
    step_table = values.get("backtest.quantity_step_by_symbol") or {}
    for sym in symbols:
        sym_values = dict(values)
        if isinstance(tick_table, dict) and sym in tick_table:
            sym_values["strategy.maker.tick_size"] = tick_table[sym]
            sym_values["venue.rules.tick_size"] = str(tick_table[sym])
        if isinstance(step_table, dict) and sym in step_table:
            sym_values["strategy.maker.quantity_step"] = step_table[sym]
            sym_values["venue.rules.step_size"] = str(step_table[sym])
        policy = build_maker_policy(sym_values)
        if policy is None:
            raise ValueError(
                f"maker config incomplete for {sym}; strategy.maker.* keys are required")
        policies[sym] = policy

    prediction = None
    if values.get("live.prediction", "local_trial") == "local_trial":
        from backtest.prediction import local_trial_runtime
        prediction = local_trial_runtime()

    # 最后才构造采集源（CollectorBatchSource 会建立真实公开行情连接；模拟模式不触写路径）。
    collector = MultiSymbolMarketCollector(
        CollectorConfig(symbols=symbols, max_symbols=max_symbols,
                          rest_base=rest_base, ws_host=ws_host,
                          pump_timeout_s=float(values.get("live.pump_timeout_s", 0.5)),
                          max_messages=int(values.get("live.max_messages", 64)),
                          max_book_age_ms=int(values.get("risk.max_book_age_ms", 5_000))))
    source = CollectorBatchSource(collector)

    run_id = str(values.get("live.run_id", "live-paper"))
    return LiveMultiSymbolRunner(
        symbols=symbols, source=source, risk=risk,
        decision_fn_for=lambda s: policies[s],
        prediction_runtime=prediction, run_id=run_id)


def run_live_paper(config_path: str | Path, *, stdout: TextIO,
                    pump_interval_s: float = 0.5, status_every_s: float = 5.0,
                    install_signal_handlers: bool = True) -> int:
    """阻塞运行实时多标的模拟交易直到 SIGINT/SIGTERM；返回进程退出码。

    真实公开行情不可达（如 Binance 451）时，采集器连接阶段会抛错，退出码非 0 并如实打印，
    不伪装成已连接/已成交。
    """
    values = json.loads(Path(config_path).read_text(encoding="utf-8"))
    runner = build_live_paper_runner(values)

    # 在后台线程跑 runner 主循环，主线程负责信号与周期状态输出。
    import threading
    thread = threading.Thread(target=runner.run_forever,
                                kwargs={"pump_interval_s": pump_interval_s},
                                name="probex-live-paper", daemon=True)

    def _stop(_signum=None, _frame=None) -> None:
        runner.request_stop()

    if install_signal_handlers:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):  # 非主线程/不支持的平台
                pass

    thread.start()
    last_emit = 0.0
    try:
        while thread.is_alive():
            now = time.time()
            if now - last_emit >= status_every_s:
                stdout.write(json.dumps(runner.stats.view(), ensure_ascii=False,
                                           sort_keys=True) + "\n")
                stdout.flush()
                last_emit = now
            time.sleep(min(0.2, status_every_s))
    finally:
        runner.request_stop()
        thread.join(timeout=10.0)
    final = runner.stats.view()
    stdout.write(json.dumps(final, ensure_ascii=False, sort_keys=True) + "\n")
    stdout.flush()
    return 0 if final["status"] == "STOPPED" else 30


__all__ = ["build_live_paper_runner", "run_live_paper"]
