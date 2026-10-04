"""Venue Integration（P0001.15 §6–§19、§11–§12）：描述「通过谁、怎么执行」与 reference price 契约。"""

from __future__ import annotations

from venue.contracts import (MarketDataConnector, MarketDataFacts, MarketTimestamps,
                             PrivateExecutionConnector, ReferencePriceSource,
                             SubmissionClassification, classify_submission)
from venue.health import (ConnectionState, ConnectorHealth, MarketConnectorHealth,
                          PrivateConnectorHealth)
from venue.identity import (VenueEnvironment, VenueError, VenueIdentity, VenueType, binance_venue,
                            environment_for_mode, paper_venue, venue_for_mode)
from venue.reference_price import (REFERENCE_PRICE_MARK_UNAVAILABLE, REFERENCE_PRICE_SOURCE_NOT_CONFIGURED,
                                   MarkPriceFact, MarkPriceReferenceSource, ReferencePrice,
                                   ReferencePriceError, ReferencePriceProvider)

__all__ = [
    "ConnectionState", "ConnectorHealth", "MarketConnectorHealth", "MarketDataConnector", "MarketDataFacts",
    "MarketTimestamps", "MarkPriceFact", "MarkPriceReferenceSource", "PrivateConnectorHealth",
    "PrivateExecutionConnector", "REFERENCE_PRICE_MARK_UNAVAILABLE", "REFERENCE_PRICE_SOURCE_NOT_CONFIGURED",
    "ReferencePrice", "ReferencePriceError", "ReferencePriceProvider", "ReferencePriceSource",
    "SubmissionClassification", "VenueEnvironment", "VenueError", "VenueIdentity", "VenueType",
    "binance_venue", "classify_submission", "environment_for_mode", "paper_venue", "venue_for_mode",
]
