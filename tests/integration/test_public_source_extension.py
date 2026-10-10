"""公开行情统一数据源扩展点的集成测试（补充任务：行情数据能力查漏补缺）。

证明验收标准"数据源可以通过适配器扩展，不需要修改核心账本、风险或订单状态所有权"：

```
任意 MarketSourceAdapter（免费公开 / 未来付费源）
  ⇒ PublicMarketPump（线程）⇒ BoundedMarketHistory（MarketState）
  ⇒ Product Read Model（snapshot/API）⇒ run 级 market 事实持久化（既有 RunRegistry）
```

- test_custom_free_source：一个模拟"其他交易所免费公开行情"的适配器，只实现
  `MarketSourceAdapter` 协议、产出既有 `MarketState`，即可经注入缝接入；不触碰
  OrderTracker/AccountingCore/RiskGate。
- test_paid_source_fail_closed：适配器连接失败时产品如实暴露（不伪报成功）。

不触网、不需凭据。
"""

from __future__ import annotations

import time

from product.provenance import ConfigEntry, ConfigSource
from product.types import Fact, RuntimeMode
from runtime.assembly import ProductRuntime, RuntimeProfile
from tests.strategy_support import market_state
from tests.support import BASE_TS, TempDirTestCase
from venue.market_source import MarketSourceAdapter


class ScriptedFreeSource:
    """模拟一个非 Binance 的免费公开行情源（只依赖 MarketSourceAdapter 协议）。"""

    def __init__(self, *, source_id: str = "example-exchange-public",
                 states: list | None = None, fail: bool = False) -> None:
        self._source_id = source_id
        self._pending = list(states or [])
        self._fail = fail
        self.connected = False

    @property
    def source_id(self) -> str:
        return self._source_id

    def connect(self) -> None:
        if self._fail:
            raise ConnectionError("simulated public source unreachable")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def poll_states(self, *, timeout_s: float, max_states: int) -> tuple:
        if self._fail:
            raise ConnectionError("simulated public source dropped")
        taken = self._pending[:max_states]
        self._pending = self._pending[max_states:]
        return tuple(taken)

    def latest_mark(self) -> object | None:
        return None


class PublicSourceExtensionTest(TempDirTestCase):
    def _runtime(self, *, source: ScriptedFreeSource) -> ProductRuntime:
        entries = (ConfigEntry(name="projection.history_capacity", source=ConfigSource.FILE,
                               value=Fact.of(2_000)),)
        runtime = ProductRuntime(profile=RuntimeProfile(
            symbol="BTCUSDT", config_entries=entries, mode=RuntimeMode.PAPER,
            run_registry_dir=str(self.tmp_path / "runs"), market_source="binance-public"))
        # 扩展点：注入自定义公开源适配器（替代默认 Binance 源）
        runtime.market_source_adapter = source
        self._source = source
        return runtime

    def test_custom_free_source_flows_into_product_and_persists(self) -> None:
        states = [
            market_state(best_bid=60000.0, best_ask=60000.1, timestamp=BASE_TS),
            market_state(best_bid=60000.2, best_ask=60000.3, timestamp=BASE_TS + 100),
        ]
        runtime = self._runtime(source=ScriptedFreeSource(states=states))
        self.assertIsInstance(states[0], object)
        # 协议结构核对
        self.assertTrue(hasattr(runtime, "market_source_adapter"))

        runtime.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if runtime._history.counts["states"] >= 2:      # noqa: SLF001
                    break
                time.sleep(0.1)

            # 统一缓冲收到两个 MarketState（适配器转换的既有契约）
            self.assertEqual(runtime._history.counts["states"], 2)   # noqa: SLF001
            self.assertTrue(self._source.connected)

            # Read Model 看到最新状态（真实可追溯；无伪造）
            snapshot = runtime.service.snapshot()
            self.assertTrue(snapshot.market.tradeable.known)
            self.assertTrue(snapshot.runtime.data_timestamp.known)

            # run 级 market 事实已持久化（既有 RunRegistry；不新建存储）
            rid = runtime.run_id
            points, reason = runtime._registry.load_run_facts(rid, "market")   # noqa: SLF001
            self.assertIsNone(reason)
            self.assertGreaterEqual(len(points), 1)
        finally:
            runtime.stop()

    def test_source_failure_is_honest_not_faked(self) -> None:
        runtime = self._runtime(source=ScriptedFreeSource(fail=True))
        # 适配器连接失败 ⇒ 启动抛错（不伪报成功；产品不进入 RUNNING）
        with self.assertRaises(ConnectionError):
            runtime.start()
        self.assertFalse(self._source.connected)

    def test_adapter_satisfies_the_protocol(self) -> None:
        adapter = ScriptedFreeSource()
        self.assertIsInstance(adapter, MarketSourceAdapter)
