"""预测复盘 / outcome evaluation（人类 2026-10-24 授权批次3）。

把已接受的 `PredictionRecord` 与 **t+horizon 的真实 mid 变化**对账。
只做确定性度量；不调用 provider、不联网、不改变任何预测/交易状态。

度量口径（显式、可审计；不发明五分类数值边界）
-------------------------------------------------
- **方向（不需要 bucket 数值边界）**：把五分类折叠成 down/flat/up 概率，
  实际方向按 mid 收益的**严格符号**判定（r<0 down / r=0 flat / r>0 up）：
  - `direction_hit`：折叠后最大概率方向 == 实际方向；
  - `brier_sign`：三类符号 Brier（Σ(p−I)²，值域 [0,2]，越小越好）。
- **adverse-selection noul**（二进制）：阈值复用既有的
  `adverse_selection_threshold_bps`（provider 必填参数，非本模块新造数值）：
  sell-AS 事件 = 5s 后 mid 下跌超过阈值；buy-AS 对称。报告 Brier 与命中率。
- **五分类 argmax 命中率 / 多类 Brier**：仅当显式给出 bucket bps 边界时计算
  （`bucket_thresholds_bps`；人类长期未决项②，缺省 ⇒ None，不猜）。
- horizon 对齐：取**首个 ts ≥ t+horizon** 的 mid 观测（bisect）；
  窗口之后没有观测 ⇒ 该样本不评估（UNKNOWN，不补）。
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field

from prediction.types import PredictionRecord

AS_HORIZON_MS = 5_000


@dataclass(frozen=True, slots=True)
class HorizonOutcome:
    horizon_ms: int
    samples: int
    direction_hit_rate: float | None
    brier_sign_mean: float | None
    #: 折叠方向概率均值（审计用）
    mean_prob_up: float
    mean_prob_down: float


@dataclass(frozen=True, slots=True)
class BinaryOutcome:
    name: str
    samples: int
    hit_rate: float | None
    brier_mean: float | None
    threshold_bps: float


@dataclass(frozen=True, slots=True)
class FiveClassOutcome:
    horizon_ms: int
    samples: int
    argmax_hit_rate: float | None
    brier_mean: float | None


@dataclass(frozen=True, slots=True)
class PredictionEvaluation:
    """一次复盘的完整结果（不可变）。字段 None = 证据不足/未配置，绝不填 0 伪装。"""

    predictions_seen: int
    horizons: tuple[HorizonOutcome, ...]
    adverse_selection: tuple[BinaryOutcome, ...] = ()
    five_class: tuple[FiveClassOutcome, ...] = ()
    notes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "predictions_seen": self.predictions_seen,
            "horizons": [asdict(h) for h in self.horizons],
            "adverse_selection": [asdict(b) for b in self.adverse_selection],
            "five_class": [asdict(f) for f in self.five_class],
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class PredictionEvaluator:
    """收集 (预测, 当时 mid) 与后续 mid 观测，结束时产出评估。"""

    adverse_selection_threshold_bps: float | None = None
    #: (down_edge_bps, strong_edge_bps)：r≤-strong 强跌，-down..down flat，其余对称
    bucket_thresholds_bps: tuple[float, float] | None = None
    _mids: list[tuple[int, float]] = field(default_factory=list)
    _samples: list[tuple[PredictionRecord, float]] = field(default_factory=list)

    def observe_mid(self, ts_ms: int, mid: float) -> None:
        if not isinstance(ts_ms, int) or not isinstance(mid, (int, float)) or mid <= 0:
            return
        if self._mids and ts_ms < self._mids[-1][0]:
            raise ValueError("mid observations must be non-decreasing in time")
        self._mids.append((ts_ms, float(mid)))

    def add_prediction(self, record: PredictionRecord, mid: float) -> None:
        if not isinstance(mid, (int, float)) or mid <= 0:
            return
        self._samples.append((record, float(mid)))

    # ------------------------------------------------------------------ 计算

    def _mid_at_or_after(self, ts_ms: int) -> float | None:
        timestamps = [item[0] for item in self._mids]
        index = bisect.bisect_left(timestamps, ts_ms)
        if index >= len(self._mids):
            return None
        return self._mids[index][1]

    def evaluate(self) -> PredictionEvaluation:
        notes: list[str] = []
        if self.bucket_thresholds_bps is None:
            notes.append("five-class scoring unavailable: bucket bps thresholds not configured")
        if self.adverse_selection_threshold_bps is None:
            notes.append("adverse-selection scoring unavailable: threshold bps not configured")

        by_horizon: dict[int, dict[str, list[float]]] = {}
        as_sell: list[tuple[float, bool]] = []
        as_buy: list[tuple[float, bool]] = []
        five: dict[int, dict[str, list[float]]] = {}

        for record, mid_t in self._samples:
            t = int(record.as_of)
            for distribution in record.prediction.future_return:
                horizon = int(distribution.horizon_ms)
                later = self._mid_at_or_after(t + horizon)
                if later is None:
                    continue
                return_bps = (later / mid_t - 1.0) * 10_000.0
                p_up = distribution.up + distribution.strong_up
                p_down = distribution.down + distribution.strong_down
                p_flat = distribution.flat
                realized = 0 if return_bps == 0.0 else (1 if return_bps > 0 else -1)
                predicted = max((p_up, 1), (p_flat, 0), (p_down, -1),
                                  key=lambda item: (item[0], item[1]))[1]
                brier = ((p_up - (1.0 if realized > 0 else 0.0)) ** 2
                          + (p_flat - (1.0 if realized == 0 else 0.0)) ** 2
                          + (p_down - (1.0 if realized < 0 else 0.0)) ** 2)
                bucket = by_horizon.setdefault(
                    horizon, {"hit": [], "brier": [], "p_up": [], "p_down": []})
                bucket["hit"].append(1.0 if predicted == realized else 0.0)
                bucket["brier"].append(brier)
                bucket["p_up"].append(p_up)
                bucket["p_down"].append(p_down)

                if horizon == AS_HORIZON_MS and self.adverse_selection_threshold_bps is not None:
                    threshold = float(self.adverse_selection_threshold_bps)
                    sell_event = return_bps < -threshold
                    buy_event = return_bps > threshold
                    sell_p = float(record.prediction.sell_adverse_selection)
                    buy_p = float(record.prediction.buy_adverse_selection)
                    as_sell.append((sell_p, sell_event))
                    as_buy.append((buy_p, buy_event))

                if self.bucket_thresholds_bps is not None:
                    edge, strong = self.bucket_thresholds_bps
                    categories = (distribution.strong_down, distribution.down,
                                    distribution.flat, distribution.up, distribution.strong_up)
                    realized_cat = _bucket_index(return_bps, edge, strong)
                    probs = (distribution.strong_down, distribution.down,
                              distribution.flat, distribution.up, distribution.strong_up)
                    fbucket = five.setdefault(horizon, {"hit": [], "brier": []})
                    fbucket["hit"].append(1.0 if probs.index(max(probs)) == realized_cat else 0.0)
                    fbucket["brier"].append(sum((p - (1.0 if i == realized_cat else 0.0)) ** 2
                                                 for i, p in enumerate(probs)))

        horizons_out: list[HorizonOutcome] = []
        for horizon in sorted(by_horizon):
            data = by_horizon[horizon]
            horizons_out.append(HorizonOutcome(
                horizon_ms=horizon, samples=len(data["hit"]),
                direction_hit_rate=sum(data["hit"]) / len(data["hit"]),
                brier_sign_mean=sum(data["brier"]) / len(data["brier"]),
                mean_prob_up=sum(data["p_up"]) / len(data["p_up"]),
                mean_prob_down=sum(data["p_down"]) / len(data["p_down"])))

        def _binary(name: str, samples: list[tuple[float, bool]]) -> BinaryOutcome | None:
            if not samples or self.adverse_selection_threshold_bps is None:
                return None
            return BinaryOutcome(
                name=name, samples=len(samples),
                hit_rate=sum(1.0 for p, event in samples if (p >= 0.5) == event) / len(samples),
                brier_mean=sum((p - (1.0 if event else 0.0)) ** 2
                                 for p, event in samples) / len(samples),
                threshold_bps=float(self.adverse_selection_threshold_bps))

        as_outcomes = tuple(x for x in (_binary("sell_adverse_selection", as_sell),
                                          _binary("buy_adverse_selection", as_buy)) if x is not None)
        five_out: list[FiveClassOutcome] = []
        for horizon in sorted(five):
            data = five[horizon]
            five_out.append(FiveClassOutcome(
                horizon_ms=horizon, samples=len(data["hit"]),
                argmax_hit_rate=sum(data["hit"]) / len(data["hit"]),
                brier_mean=sum(data["brier"]) / len(data["brier"])))

        return PredictionEvaluation(
            predictions_seen=len(self._samples),
            horizons=tuple(horizons_out),
            adverse_selection=as_outcomes,
            five_class=tuple(five_out),
            notes=tuple(notes))


def _bucket_index(return_bps: float, edge: float, strong: float) -> int:
    """五分类下标：0 strong_down / 1 down / 2 flat / 3 up / 4 strong_up（边界含等号）。"""
    if return_bps <= -strong:
        return 0
    if return_bps <= -edge:
        return 1
    if return_bps < edge:
        return 2
    if return_bps < strong:
        return 3
    return 4


__all__ = [
    "AS_HORIZON_MS",
    "PredictionEvaluator",
    "PredictionEvaluation",
]
