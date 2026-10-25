"""P0017.3 回测入口编排（CLI 与库共用；核心逻辑在 driver，不在 CLI）。

`execute_backtest`：校验输入 → 数据集身份 → 构造配置 → run_backtest → 写产物。
失败也会产出诊断 summary/report（状态非 SUCCESS），退出码由调用方决定。
"""

from __future__ import annotations

import json
from pathlib import Path

from backtest import BacktestError, BacktestStatus
from backtest.driver import BacktestConfig, run_backtest
from backtest.multi_driver import MultiBacktestConfig, run_multi_backtest
from backtest.identity import identity_for_input, run_identity
from backtest.report import write_artifacts
from backtest.prediction import local_trial_runtime
from runtime.maker_config import build_maker_policy

REQUIRED_CONFIG_KEYS = (
    "backtest.symbol",
    "backtest.initial_balance",
    "backtest.maker_fee_rate",
    "backtest.fee_asset",
    "backtest.submit_latency_ms",
    "backtest.cancel_latency_ms",
)


def _base_config_kwargs(values: dict[str, object]) -> dict:
    def num(key: str, default=None):
        if key not in values:
            return default
        return float(values[key])

    return dict(
        symbol=str(values["backtest.symbol"]),
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
        max_book_age_ms=(int(values["risk.max_book_age_ms"])
                           if "risk.max_book_age_ms" in values else None),
        seed=(int(values["backtest.seed"]) if "backtest.seed" in values else None),
        funding_enabled=bool(values.get("backtest.funding_enabled", False)),
        adverse_selection_threshold_bps=num("prediction.adverse_selection_threshold_bps"),
        bucket_edge_bps=num("evaluation.bucket_edge_bps"),
        bucket_strong_bps=num("evaluation.bucket_strong_bps"),
        taker_slippage_bps=num("simulation.taker_slippage_bps"),
        taker_fee_rate=num("backtest.taker_fee_rate"),
    )


def load_config(values: dict[str, object]) -> BacktestConfig:
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in values]
    if missing:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"missing required backtest config keys: {missing}")
    return BacktestConfig(**_base_config_kwargs(values))


def load_multi_config(values: dict[str, object]) -> "MultiBacktestConfig":
    """多标的配置：`backtest.symbols` 为有序标的列表；缺省回退 [backtest.symbol]。"""
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in values]
    if missing:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"missing required backtest config keys: {missing}")
    raw = values.get("backtest.symbols")
    if isinstance(raw, str):
        symbols = tuple(s.strip() for s in raw.split(",") if s.strip())
    elif isinstance(raw, (list, tuple)):
        symbols = tuple(str(s) for s in raw)
    else:
        symbols = (str(values["backtest.symbol"]),)
    if len(set(symbols)) != len(symbols):
        raise BacktestError(BacktestStatus.INVALID_INPUT, "duplicate backtest.symbols")

    def _table(prefix: str) -> dict[str, float]:
        raw_table = values.get(prefix)
        if isinstance(raw_table, dict):
            return {str(k): float(v) for k, v in raw_table.items()}
        return {}

    return MultiBacktestConfig(
        **_base_config_kwargs(values), symbols=symbols,
        tick_size_by_symbol=_table("backtest.tick_size_by_symbol"),
        quantity_step_by_symbol=_table("backtest.quantity_step_by_symbol"))


def load_bar_config(values: dict[str, object], mode: str) -> "BarBacktestConfig":
    """bar 回测配置（成交模式必填且受信；其余与普通回测一致）。"""
    from backtest.bar_driver import BAR_EXECUTION_MODES, BarBacktestConfig

    if mode not in BAR_EXECUTION_MODES:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"invalid backtest.bar_mode {mode!r}, expected {BAR_EXECUTION_MODES}")
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in values]
    if missing:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"missing required backtest config keys: {missing}")
    base = _base_config_kwargs(values)
    return BarBacktestConfig(**base, bar_mode=mode)


def execute_backtest(*, dataset: str | Path, config_path: str | Path, out_dir: str | Path,
                      prediction: bool = True) -> dict[str, object]:
    """运行一次回测并写产物，返回 summary dict（含 status/paths/result_hash）。"""
    dataset = Path(dataset)
    config_path = Path(config_path)
    out_dir = Path(out_dir)
    if not config_path.is_file():
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"config not found: {config_path}")
    try:
        values = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"config is not valid JSON: {exc}") from exc

    raw_symbols = values.get("backtest.symbols")
    if isinstance(raw_symbols, str):
        declared_symbols = tuple(s.strip() for s in raw_symbols.split(",") if s.strip())
    elif isinstance(raw_symbols, (list, tuple)):
        declared_symbols = tuple(str(s) for s in raw_symbols)
    else:
        declared_symbols = ()
    identity = identity_for_input(dataset, symbols=(declared_symbols or None))
    is_multi = bool(raw_symbols)
    cfg = load_multi_config(values) if is_multi else load_config(values)
    run_id, input_hash = run_identity(
        code_version=_git_commit() or "unknown",
        dataset_sha256=identity["sha256"], config=cfg.canonical(), seed=cfg.seed)

    multi_symbols = cfg.symbols if isinstance(cfg, MultiBacktestConfig) and cfg.symbols else ()
    is_multi = len(multi_symbols) >= 2

    policies: dict[str, object] = {}
    decision_map: object = None
    if is_multi:
        # 每个 symbol 用其 tick/step 构造独立策略；优先 per-symbol 表，其次全局 maker 键。
        for sym in multi_symbols:
            sym_values = dict(values)
            tick = cfg.tick_size_by_symbol.get(sym) if isinstance(cfg, MultiBacktestConfig) else None
            step = cfg.quantity_step_by_symbol.get(sym) if isinstance(cfg, MultiBacktestConfig) else None
            if tick is not None:
                sym_values["strategy.maker.tick_size"] = tick
            if step is not None:
                sym_values["strategy.maker.quantity_step"] = step
            policy = build_maker_policy(sym_values)
            if policy is not None:
                policies[sym] = policy
        decision_map = policies
    else:
        policy = build_maker_policy(values)
        decision_map = (None if policy is None else (lambda **kw: policy.decide(**kw)))

    bar_mode = values.get("backtest.bar_mode")
    bar_dataset_files = None
    if bar_mode is not None:
        from backtest.bar_driver import run_bar_backtest

        bar_cfg = load_bar_config(values, str(bar_mode))
        # run identity 必须包含 bar_mode/粒度（同事件数据跑 event-driven 与 bar 是不同实验）
        run_id, _input_hash = run_identity(
            code_version=_git_commit() or "unknown",
            dataset_sha256=identity["sha256"], config=bar_cfg.canonical(),
            seed=bar_cfg.seed)
        result = run_bar_backtest(
            dataset, bar_cfg, run_id=run_id, dataset_sha256=identity["sha256"],
            decision_fn=None)
        if dataset.is_dir():
            bar_dataset_files = sorted(str(p.relative_to(dataset))
                                          for p in dataset.rglob("data.jsonl"))
        else:
            bar_dataset_files = [dataset.name]
        paths = write_artifacts(result, out_dir=out_dir, dataset_files=bar_dataset_files)
        return {
            "status": result.status.value, "run_id": result.run_id,
            "reason": result.reason,
            "paths": {k: str(v) for k, v in paths.items() if k != "result_hash"},
            "result_hash": paths.get("result_hash"),
            "submits": result.trace.submits, "fills": result.trace.fills,
            "decisions": result.trace.decisions,
            "risk_rejects": result.trace.risk_rejects,
        }

    pred = local_trial_runtime() if prediction else None

    if is_multi:
        result = run_multi_backtest(
            dataset, cfg, run_id=run_id, dataset_sha256=identity["sha256"],
            decision_fn=decision_map, prediction_runtime=pred)
    else:
        result = run_backtest(dataset, cfg, run_id=run_id,
                               dataset_sha256=identity["sha256"],
                               decision_fn=decision_map, prediction_runtime=pred)
    if dataset.is_dir():
        dataset_files = sorted(str(p.relative_to(dataset))
                                for p in dataset.rglob("data.jsonl"))
    else:
        dataset_files = [dataset.name]
    paths = write_artifacts(result, out_dir=out_dir,
                              dataset_files=dataset_files)
    summary = {
        "status": result.status.value,
        "run_id": result.run_id,
        "reason": result.reason,
        "paths": {k: str(v) for k, v in paths.items() if k != "result_hash"},
        "result_hash": paths.get("result_hash"),
        "submits": result.trace.submits,
        "fills": result.trace.fills,
        "decisions": result.trace.decisions,
        "risk_rejects": result.trace.risk_rejects,
    }
    return summary


def _git_commit() -> str | None:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                               timeout=5, check=False)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["REQUIRED_CONFIG_KEYS", "execute_backtest", "load_config"]
