"""订单参数归一（P0001.9.7.2）：price / quantity 的 Decimal 量化与 **ownership**。

背景（实测）：策略提案价以 IEEE-754 float 表达，`Decimal(repr(83958.20000000001))` 这类值在序列化后
会超出交易所 `tickSize` 精度，被交易所按 precision 拒绝；而执行 adapter 依 D-048 **不自动 round**
（拒绝隐式修正）。于是产生一个**归属未定义**的空白：到底谁负责把价格/数量变成 tick/step 精确值。

本模块把 ownership 明确为：

    执行边界（execution domain）**显式配置**的归一化步骤拥有该职责；
    adapter 默认仍然不做任何隐式 round（D-048 不变），只有被显式注入 `OrderNormalizer` 时才归一。

规则：

- 一律用 `Decimal`（绝不用 float 做量化算术），输入用 `str(value)` 转换以避免二进制噪声；
- 舍入模式**必须显式给出**（不设默认值）：这是业务选择，不能由实现方猜测；
- 归一化结果同时给出十进制字符串形式（供 REST 请求直接序列化，不经过 float 往返）；
- 归一化会**移动**价格，因此调用方必须在 `adjusted` 为 True 时自行判断是否仍满足业务意图
  （例如 post-only 是否仍为被动价）；本模块只做数学，不判断业务。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation, localcontext

from execution.types import ExecutionError


class OrderNormalizationError(ExecutionError):
    """归一化输入的契约错误（缺失 / 非有限 / 非正 / 规则不可用）。"""


#: 允许的舍入模式（**必须显式选择**；不提供默认值）
ALLOWED_ROUNDING = (ROUND_UP, ROUND_DOWN, ROUND_HALF_UP)


def _decimal(value: object, *, name: str) -> Decimal:
    """把输入转成 Decimal。float 走 `str()` 以避免二进制展开噪声；拒绝 NaN/Inf。"""
    if isinstance(value, bool):
        raise OrderNormalizationError(f"{name} must be a number, got bool")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - 防御
            raise OrderNormalizationError(f"{name} is not a decimal string: {value!r}") from exc
    else:
        raise OrderNormalizationError(f"{name} must be an int/float/str/Decimal, got {type(value).__name__}")
    if not result.is_finite():
        raise OrderNormalizationError(f"{name} must be finite, got {result}")
    if result <= 0:
        raise OrderNormalizationError(f"{name} must be > 0, got {result}")
    return result


def _quantize(value: Decimal, *, step: Decimal, rounding: str) -> Decimal:
    """把 value 量化到 step 的整数倍（Decimal 精确算术）。"""
    if step <= 0:
        raise OrderNormalizationError(f"quantization step must be > 0, got {step}")
    with localcontext() as ctx:
        ctx.prec = 60
        multiples = (value / step).quantize(Decimal(1), rounding=rounding)
        return multiples * step


@dataclass(frozen=True, slots=True)
class NormalizedOrderParams:
    """归一化结果（**不是**裸 tuple）：同时保留 Decimal 与字符串两种精确形式。"""

    price: Decimal
    quantity: Decimal
    price_text: str
    quantity_text: str
    #: 归一化是否改变了输入值（None 表示没有可比对的原始输入）
    adjusted: bool


@dataclass(frozen=True, slots=True)
class OrderNormalizer:
    """把 price / quantity 量化到交易所 tick/step 的归一化器（**显式配置**才启用）。

    `price_rounding` / `quantity_rounding` 必须显式给出：
    - 价格通常用 `ROUND_DOWN`（买方）/`ROUND_UP`（卖方）保持被动，或 `ROUND_HALF_UP`；
    - 数量常用 `ROUND_DOWN`（不超过风险预算）。
    本类不做这些业务选择，只按传入模式执行。
    """

    tick_size: Decimal
    step_size: Decimal
    price_rounding: str
    quantity_rounding: str

    def __post_init__(self) -> None:
        for name in ("tick_size", "step_size"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise OrderNormalizationError(f"OrderNormalizer.{name} must be a Decimal")
            if not value.is_finite() or value <= 0:
                raise OrderNormalizationError(f"OrderNormalizer.{name} must be finite and > 0, got {value}")
        for name in ("price_rounding", "quantity_rounding"):
            value = getattr(self, name)
            if value not in ALLOWED_ROUNDING:
                raise OrderNormalizationError(
                    f"OrderNormalizer.{name} must be one of {ALLOWED_ROUNDING}, got {value!r} "
                    "(rounding is a business decision and must be explicit)"
                )

    def normalize(self, *, price: object, quantity: object) -> NormalizedOrderParams:
        """归一化并返回精确形式；任一输入非法 ⇒ 抛错（绝不静默修正）。"""
        raw_price = _decimal(price, name="price")
        raw_quantity = _decimal(quantity, name="quantity")
        new_price = _quantize(raw_price, step=self.tick_size, rounding=self.price_rounding)
        new_quantity = _quantize(raw_quantity, step=self.step_size, rounding=self.quantity_rounding)
        if new_price <= 0:
            raise OrderNormalizationError(
                f"price {raw_price} quantized to {new_price} with tick {self.tick_size} "
                f"and rounding {self.price_rounding}"
            )
        if new_quantity <= 0:
            raise OrderNormalizationError(
                f"quantity {raw_quantity} quantized to {new_quantity} with step {self.step_size} "
                f"and rounding {self.quantity_rounding}"
            )
        return NormalizedOrderParams(
            price=new_price,
            quantity=new_quantity,
            price_text=_plain(new_price),
            quantity_text=_plain(new_quantity),
            adjusted=(new_price != raw_price or new_quantity != raw_quantity),
        )


def _plain(value: Decimal) -> str:
    """Decimal → 普通十进制字符串（无科学计数法），可直接放进 REST 查询串。"""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


__all__ = [
    "ALLOWED_ROUNDING",
    "NormalizedOrderParams",
    "OrderNormalizationError",
    "OrderNormalizer",
]
