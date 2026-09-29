"""产品层（P0001.10）：只组合既有 Owner 的事实，不拥有交易真相。"""

from product.serialization import SerializationError, snapshot_to_json, snapshot_to_jsonable, to_jsonable
from product.service import ProductService
from product.snapshot import SystemSnapshot
from product.types import (
    SCHEMA_VERSION,
    UNKNOWN_NOT_AVAILABLE,
    UNKNOWN_NOT_PROVIDED,
    EvidenceView,
    ExecutionView,
    Fact,
    HealthView,
    MarketView,
    OrderView,
    PortfolioView,
    PredictionView,
    ReadinessView,
    RiskView,
    RuntimeIdentity,
    RuntimeMode,
    StrategyView,
    TraceEntry,
)

__all__ = [
    "SCHEMA_VERSION",
    "UNKNOWN_NOT_AVAILABLE",
    "UNKNOWN_NOT_PROVIDED",
    "EvidenceView",
    "ExecutionView",
    "Fact",
    "HealthView",
    "MarketView",
    "OrderView",
    "PortfolioView",
    "PredictionView",
    "ProductService",
    "ReadinessView",
    "RiskView",
    "RuntimeIdentity",
    "RuntimeMode",
    "SerializationError",
    "StrategyView",
    "SystemSnapshot",
    "TraceEntry",
    "snapshot_to_json",
    "snapshot_to_jsonable",
    "to_jsonable",
]
