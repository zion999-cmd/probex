"""Instrument registry（P0001.15 §1）：把裸 symbol 解析为正式 `InstrumentSpec`。"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.instruments.model import InstrumentError, InstrumentSpec


@dataclass(frozen=True, slots=True)
class InstrumentRegistry:
    """不可变 registry：symbol ↔ instrument_id 双向唯一。"""

    instruments: tuple[InstrumentSpec, ...]
    #: 当前 runtime 使用的 instrument（由 composition root 显式注入）
    current_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.instruments, tuple) or not self.instruments:
            raise InstrumentError("InstrumentRegistry.instruments must be a non-empty tuple")
        for item in self.instruments:
            if not isinstance(item, InstrumentSpec):
                raise InstrumentError("InstrumentRegistry.instruments must contain InstrumentSpec values")
        ids = [item.instrument_id for item in self.instruments]
        symbols = [item.symbol for item in self.instruments]
        if len(set(ids)) != len(ids):
            raise InstrumentError("InstrumentRegistry.instruments must have unique instrument_id")
        if len(set(symbols)) != len(symbols):
            raise InstrumentError("InstrumentRegistry.instruments must have unique symbol")
        if self.current_id and self.current_id not in ids:
            raise InstrumentError(f"current_id {self.current_id!r} is not registered")

    def get(self, instrument_id: str) -> InstrumentSpec:
        for item in self.instruments:
            if item.instrument_id == instrument_id:
                return item
        raise InstrumentError(f"unknown instrument_id {instrument_id!r}")

    def by_symbol(self, symbol: str) -> InstrumentSpec:
        for item in self.instruments:
            if item.symbol == symbol:
                return item
        raise InstrumentError(f"unknown symbol {symbol!r} (no instrument registered; refusing to infer)")

    def optional(self, symbol: str) -> InstrumentSpec | None:
        """按 symbol 查找；未注册 ⇒ `None`（调用方应如实报告 UNKNOWN，而不是猜一个默认 instrument）。"""
        for item in self.instruments:
            if item.symbol == symbol:
                return item
        return None

    @property
    def current(self) -> InstrumentSpec:
        if not self.current_id:
            if len(self.instruments) == 1:
                return self.instruments[0]
            raise InstrumentError("registry has no current instrument; composition root must set current_id")
        return self.get(self.current_id)

    def view(self) -> list[dict[str, object]]:
        return [item.view() for item in self.instruments]


@dataclass(frozen=True, slots=True)
class InstrumentResolver:
    """把 `symbol → InstrumentSpec` 暴露给只读投影（未注册 ⇒ 如实 UNKNOWN，不推断）。"""

    registry: InstrumentRegistry | None = field(default=None)

    def for_symbol(self, symbol: object) -> InstrumentSpec | None:
        if self.registry is None or not isinstance(symbol, str) or not symbol:
            return None
        return self.registry.optional(symbol)


__all__ = ["InstrumentRegistry", "InstrumentResolver"]
