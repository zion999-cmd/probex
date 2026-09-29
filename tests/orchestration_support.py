"""P0001.9.7 测试脚手架：组装一个可离线驱动的 live orchestration 栈。"""

from __future__ import annotations

from dataclasses import dataclass, field

from connectors.binance.execution import (
    BinanceExecutionAdapter,
    ExecutionAuthorityContext,
    PrivateExternalFactsProvider,
)
from connectors.binance.execution.rest import BinanceExecutionRestClient
from connectors.binance.market_data.exchange_info import TradingRules
from execution.engine import ExecutionEngine
from execution.manager import OrderManager
from execution.tracker import OrderTracker
from live import LiveExecutionOrchestrator, OrchestratorConfig, OrchestratorState
from portfolio.accounting import AccountingCore
from readiness.authority import ExecutionReadinessAuthority, evidence_digest, risk_policy_fingerprint
from readiness.types import Environment, LiveReadinessScope, LiveReadinessStatus, RecoveryGeneration
from risk.gate import RiskGate
from risk.limits import RiskLimits
from risk.types import KillSwitchMode
from strategy.maker.policy import MakerPolicy
from strategy.maker.types import MakerPolicyConfig
from tests.strategy_support import maker_config as _maker_config
from tests.execution_support_live import FakeExecutionFetcher, credentials, echoing_ack, echoing_cancel
from tests.support import BASE_TS, SYMBOL

NOW = BASE_TS + 1_000
#: 测试用新增暴露预算（USDT）—— **测试值**，不是生产参数。
RISK_BUDGET_USDT = 1_000.0


def trading_rules() -> TradingRules:
    return TradingRules(
        symbol=SYMBOL,
        status="TRADING",
        tick_size=0.1,
        min_price=100.0,
        max_price=1_000_000.0,
        step_size=0.001,
        min_qty=0.001,
        max_qty=100.0,
        min_notional=50.0,
    )


def maker_config(**overrides: object) -> MakerPolicyConfig:
    """测试用 MakerPolicy 配置。

    - 复用既有测试值，但把网格/规模改成 **BTCUSDT 量级**（tick 0.1 / step 0.001 / 规模 0.001 BTC），
      以便与 adapter 的 trading rules 和测试 RiskLimits 自洽；
    - `minimum_edge_bps=0` 让**编排**测试不被策略 edge 门干扰。

    注意：这些都是**测试值**，不是生产策略参数（生产数值由人类配置）。
    """
    values: dict[str, object] = {
        "tick_size": 0.1,
        "quantity_step": 0.001,
        "min_quote_size": 0.001,
        "base_size": 0.001,
        "minimum_edge_bps": 0.0,
        "max_back_ticks": 3,
        "target_position": 0.0,
    }
    values.update(overrides)
    return _maker_config(**values)


def authority(*, ttl_ms: int = 600_000, scope: LiveReadinessScope = LiveReadinessScope.TESTNET_LIVE_READY):
    limits = RiskLimits(max_position_qty=1.0, max_daily_loss=100.0, max_drawdown_pct=0.2)
    return ExecutionReadinessAuthority(
        authority_id="auth-1",
        issued_at_ms=NOW,
        expires_at_ms=NOW + ttl_ms,
        scope=scope,
        environment=Environment.TESTNET if scope is LiveReadinessScope.TESTNET_LIVE_READY else Environment.MAINNET,
        recovery_generation=RecoveryGeneration(0, 0),
        market_generation=1,
        market_evidence_ts=NOW,
        account_snapshot_ts=NOW,
        clock_calibration_ts=NOW,
        hwm_activation_id="act-1",
        hwm_generation=1,
        risk_policy_fingerprint=risk_policy_fingerprint(limits),
        evidence_digest=evidence_digest({"x": 1}),
        readiness_status=LiveReadinessStatus.LIVE_READY,
    )


@dataclass
class FakeFacts:
    """可脚本化的外部事实 provider（真实 empty / 抛错 两种语义都支持）。"""

    orders: tuple = ()
    fills: tuple = ()
    error: Exception | None = None

    def parse_open_orders(self, symbol: str):
        if self.error is not None:
            raise self.error
        return self.orders

    def parse_recent_fills(self, symbol: str, *, since_ms=None):
        if self.error is not None:
            raise self.error
        return self.fills


@dataclass
class OrchestrationStack:
    """引擎 + adapter + policy + orchestrator（全部离线、确定性）。"""

    orchestrator: LiveExecutionOrchestrator
    engine: ExecutionEngine
    adapter: BinanceExecutionAdapter
    tracker: OrderTracker
    accounting: AccountingCore
    transport: FakeExecutionFetcher
    facts: FakeFacts
    config: OrchestratorConfig = field(default=None)  # type: ignore[assignment]

    @classmethod
    def build(
        cls,
        *,
        responses: dict | None = None,
        execution_enabled: bool = True,
        allow_write: bool = True,
        authority_context: ExecutionAuthorityContext | None = None,
        facts: FakeFacts | None = None,
        local_status_from_tracker: bool = True,
        policy_config: MakerPolicyConfig | None = None,
    ) -> "OrchestrationStack":
        # 默认用**回显** handler：多单场景下响应必须指向本单（否则会被正确判为 UNKNOWN）
        transport = FakeExecutionFetcher(
            responses={"POST": echoing_ack, "DELETE": echoing_cancel, **(responses or {})}
        )
        rest = BinanceExecutionRestClient(
            credentials=credentials(), fetcher=transport, base_url="https://demo-fapi.binance.com",
            clock=lambda: NOW,
        )
        tracker = OrderTracker(session_id="s1")
        facts_provider = facts if facts is not None else FakeFacts()
        adapter = BinanceExecutionAdapter(
            rest=rest,
            environment=Environment.TESTNET,
            authority_provider=lambda: (authority_context or context()),
            rules_provider=trading_rules,
            symbol=SYMBOL,
            local_status_provider=(lambda cid: (tracker.order(cid).status if tracker.order(cid) else None))
            if local_status_from_tracker
            else None,
            private_read=facts_provider,
        )
        manager = OrderManager(tracker=tracker, adapter=adapter)
        accounting = AccountingCore(initial_balance=10_000.0)
        accounting.update_mark_price(SYMBOL, 60_000.0, timestamp=BASE_TS)
        engine = ExecutionEngine(
            accounting=accounting,
            gate=RiskGate(RiskLimits(max_position_qty=1.0, max_open_order_exposure=5_000.0, max_daily_loss=100.0)),
            manager=manager,
            book_healthy=True,
        )
        config = OrchestratorConfig(
            symbol=SYMBOL, environment=Environment.TESTNET,
            execution_enabled=execution_enabled, allow_write=allow_write,
        )
        policy = MakerPolicy(config=policy_config or maker_config())
        orchestrator = LiveExecutionOrchestrator(
            config=config,
            engine=engine,
            adapter=adapter,
            policy=policy,
            authority_provider=lambda: (authority_context or context()),
            # **测试预算**（生产数值由人类配置；orchestrator 只透传）
            risk_budget_provider=lambda _snapshot: RISK_BUDGET_USDT,
            clock=lambda: NOW,
        )
        stack = cls(
            orchestrator=orchestrator, engine=engine, adapter=adapter, tracker=tracker,
            accounting=accounting, transport=transport, facts=facts_provider, config=config,
        )
        if execution_enabled and allow_write:
            orchestrator.mark_ready()
        elif execution_enabled:
            orchestrator.mark_ready()
        else:
            orchestrator.mark_ready()
        return stack


def context(**overrides: object) -> ExecutionAuthorityContext:
    values: dict[str, object] = {
        "authority": authority(),
        "recovery_generation": RecoveryGeneration(0, 0),
        "market_generation": 1,
        "hwm_activation_id": "act-1",
        "hwm_generation": 1,
        "kill_switch_mode": KillSwitchMode.NORMAL,
    }
    values.update(overrides)
    return ExecutionAuthorityContext(**values)  # type: ignore[arg-type]


__all__ = [
    "FakeFacts",
    "RISK_BUDGET_USDT",
    "NOW",
    "OrchestrationStack",
    "authority",
    "context",
    "maker_config",
    "trading_rules",
]
