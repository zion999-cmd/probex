"""Venue identity（P0001.15 §6）：`venue_id` / `venue_type` / `environment` 三个正交维度。

不要把 environment 与 venue 混为一个字段：PAPER venue 只能是 PAPER 环境；Binance 可以是 TESTNET 或 LIVE。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from product.types import RuntimeMode


class VenueError(ValueError):
    """venue 契约错误。"""


class VenueType(Enum):
    """venue 实现类别（不是环境）。"""

    PAPER = "paper"
    BINANCE = "binance"


class VenueEnvironment(Enum):
    """venue 所处的环境（不是 venue 实现）。"""

    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


#: 每个 venue type 允许的环境（定义性约束，不含业务数值）。
_ENVIRONMENTS_BY_TYPE: dict[VenueType, frozenset[VenueEnvironment]] = {
    VenueType.PAPER: frozenset({VenueEnvironment.PAPER}),
    VenueType.BINANCE: frozenset({VenueEnvironment.TESTNET, VenueEnvironment.LIVE}),
}


@dataclass(frozen=True, slots=True)
class VenueIdentity:
    """正式 venue identity。"""

    venue_id: str
    venue_type: VenueType
    environment: VenueEnvironment

    def __post_init__(self) -> None:
        if not isinstance(self.venue_id, str) or not self.venue_id:
            raise VenueError("VenueIdentity.venue_id must be a non-empty string")
        if not isinstance(self.venue_type, VenueType):
            raise VenueError("VenueIdentity.venue_type must be a VenueType")
        if not isinstance(self.environment, VenueEnvironment):
            raise VenueError("VenueIdentity.environment must be a VenueEnvironment")
        allowed = _ENVIRONMENTS_BY_TYPE[self.venue_type]
        if self.environment not in allowed:
            raise VenueError(
                f"venue_type {self.venue_type.value} cannot run in environment {self.environment.value} "
                f"(allowed: {sorted(item.value for item in allowed)})")

    @property
    def is_paper(self) -> bool:
        return self.venue_type is VenueType.PAPER

    def view(self) -> dict[str, object]:
        return {
            "venue_id": self.venue_id,
            "venue_type": self.venue_type.value,
            "environment": self.environment.value,
        }


def paper_venue(venue_id: str = "paper") -> VenueIdentity:
    """PAPER venue（唯一合法环境 = PAPER）。"""
    return VenueIdentity(venue_id=venue_id, venue_type=VenueType.PAPER, environment=VenueEnvironment.PAPER)


def binance_venue(*, environment: VenueEnvironment, venue_id: str = "binance") -> VenueIdentity:
    """Binance venue（TESTNET / LIVE）。"""
    return VenueIdentity(venue_id=venue_id, venue_type=VenueType.BINANCE, environment=environment)


def environment_for_mode(mode: RuntimeMode) -> VenueEnvironment:
    """runtime mode → venue environment。

    `REPLAY` 是**回放**（没有真实交易所写路径），其执行环境按 PAPER 记；`TESTNET` / `LIVE` 一一对应。
    """
    if not isinstance(mode, RuntimeMode):
        raise VenueError("mode must be a RuntimeMode")
    if mode is RuntimeMode.REPLAY:
        return VenueEnvironment.PAPER
    if mode is RuntimeMode.PAPER:
        return VenueEnvironment.PAPER
    if mode is RuntimeMode.TESTNET:
        return VenueEnvironment.TESTNET
    return VenueEnvironment.LIVE


def venue_for_mode(mode: RuntimeMode, *, venue_id: str = "paper") -> VenueIdentity:
    """按 runtime mode 解析 venue identity（REPLAY/PAPER ⇒ paper venue）。"""
    environment = environment_for_mode(mode)
    if environment is VenueEnvironment.PAPER:
        return paper_venue(venue_id)
    return binance_venue(environment=environment, venue_id="binance")


__all__ = [
    "VenueEnvironment", "VenueError", "VenueIdentity", "VenueType", "binance_venue", "environment_for_mode",
    "paper_venue", "venue_for_mode",
]
