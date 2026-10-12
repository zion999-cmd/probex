"""P0017.3 回测入口编排（CLI 与库共用；核心逻辑在 driver，不在 CLI）。

`execute_backtest`：校验输入 → 数据集身份 → 构造配置 → run_backtest → 写产物。
失败也会产出诊断 summary/report（状态非 SUCCESS），退出码由调用方决定。
"""

from __future__ import annotations

import json
from pathlib import Path

from backtest import BacktestError, BacktestStatus
from backtest.driver import BacktestConfig, run_backtest
from backtest.identity import dataset_identity, run_identity
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


def load_config(values: dict[str, object]) -> BacktestConfig:
    missing = [k for k in REQUIRED_CONFIG_KEYS if k not in values]
    if missing:
        raise BacktestError(BacktestStatus.INVALID_INPUT,
                              f"missing required backtest config keys: {missing}")

    def num(key: str, default=None, required=False):
        if key not in values:
            if required:
                raise BacktestError(BacktestStatus.INVALID_INPUT, f"missing {key}")
            return default
        return float(values[key])

    cfg = BacktestConfig(
        symbol=str(values["backtest.symbol"]),
        initial_balance=num("backtest.initial_balance", required=True),
        maker_fee_rate=num("backtest.maker_fee_rate", required=True),
        fee_asset=str(values["backtest.fee_asset"]),
        submit_latency_ms=int(num("backtest.submit_latency_ms", 0.0)),
        cancel_latency_ms=int(num("backtest.cancel_latency_ms", 0.0)),
        max_position_notional=num("risk.max_position_notional"),
        max_portfolio_gross_notional=num("risk.max_portfolio_gross_notional"),
        max_open_order_exposure=num("risk.max_open_order_exposure"),
        max_daily_loss=num("risk.max_daily_loss"),
        max_drawdown_pct=num("risk.max_drawdown_pct"),
        max_book_age_ms=(int(values["risk.max_book_age_ms"])
                           if "risk.max_book_age_ms" in values else None),
        seed=(int(values["backtest.seed"]) if "backtest.seed" in values else None))
    return cfg


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

    identity = dataset_identity([dataset])
    cfg = load_config(values)
    run_id, input_hash = run_identity(
        code_version=_git_commit() or "unknown",
        dataset_sha256=identity["sha256"], config=cfg.canonical(), seed=cfg.seed)

    policy = build_maker_policy(values)
    decision_fn = (None if policy is None
                     else (lambda **kw: policy.decide(**kw)))
    pred = local_trial_runtime() if prediction else None

    result = run_backtest(dataset, cfg, run_id=run_id,
                           dataset_sha256=identity["sha256"],
                           decision_fn=decision_fn, prediction_runtime=pred)
    paths = write_artifacts(result, out_dir=out_dir,
                              dataset_files=[dataset.name])
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
