"""P0001.17：`LOCAL_TRIAL` 试验 prediction provider 的契约测试（人类裁决 2026-10-04）。

验证：确定性、可审计规则、严格合法响应、fail-closed、**仅本地模式可用**（TESTNET/LIVE 拒绝）。
"""

from __future__ import annotations

import asyncio
import json
import unittest

from prediction.parsing.market_v1 import parse_jev_prediction
from prediction.providers.local_trial import (LOCAL_TRIAL_MODEL_ID, LOCAL_TRIAL_PROVIDER_ID,
                                              LocalTrialInputError, LocalTrialProvider)
from prediction.schema.market_v1 import (QUESTION_SCHEMA_VERSION, build_jev_payload,
                                         market_state_hash)
from prediction.types import PredictionRequest
from tests.support import warm_market_states


def request_with(payload: dict[str, object]) -> PredictionRequest:
    from market.events.types import Venue

    state = warm_market_states(1)[-1]
    return PredictionRequest(
        sequence=1, request_id="req-1", venue=Venue.BINANCE, symbol="BTCUSDT",
        as_of=state.time.as_of_exchange_ts, market_state_hash=market_state_hash(state),
        feature_schema_version=state.feature_schema_version,
        question_schema_version=QUESTION_SCHEMA_VERSION, horizons_ms=(5_000, 15_000, 30_000, 60_000),
        created_at=state.time.as_of_exchange_ts, expires_at=state.time.as_of_exchange_ts + 60_000,
        payload_json=json.dumps(payload, separators=(",", ":"), sort_keys=True))


def canonical_payload() -> dict[str, object]:
    return dict(build_jev_payload(warm_market_states(1)[-1]))


class LocalTrialProviderTest(unittest.TestCase):
    def test_response_is_strict_parser_valid_and_marked_as_trial(self) -> None:
        provider = LocalTrialProvider()
        response = asyncio.run(provider.predict(request_with(canonical_payload())))

        self.assertEqual(response.provider, LOCAL_TRIAL_PROVIDER_ID)
        self.assertEqual(response.model, LOCAL_TRIAL_MODEL_ID)
        prediction = parse_jev_prediction(response.raw_response)          # 既有 strict parser
        self.assertEqual(len(prediction.future_return), 4)
        for record in prediction.future_return:
            self.assertAlmostEqual(record.probability_sum, 1.0, places=6)
        self.assertTrue(0.0 <= prediction.buy_adverse_selection <= 1.0)
        self.assertTrue(0.0 <= prediction.buy_fill_probability <= 1.0)

    def test_deterministic_for_identical_input(self) -> None:
        provider = LocalTrialProvider()
        first = asyncio.run(provider.predict(request_with(canonical_payload())))
        second = asyncio.run(provider.predict(request_with(canonical_payload())))
        self.assertEqual(first.raw_response, second.raw_response)

    def test_one_sided_flow_shifts_mass_toward_that_side(self) -> None:
        provider = LocalTrialProvider()

        up = canonical_payload()
        up["flow"] = {"normalized_ofi_5s": 1.0}
        up["depth"] = {"l1_imbalance": 1.0}
        up["price"] = {"mid": 100.0, "spread": 1.0, "microprice": 101.0}
        bullish = parse_jev_prediction(asyncio.run(provider.predict(request_with(up))).raw_response)

        down = canonical_payload()
        down["flow"] = {"normalized_ofi_5s": -1.0}
        down["depth"] = {"l1_imbalance": -1.0}
        down["price"] = {"mid": 100.0, "spread": 1.0, "microprice": 99.0}
        bearish = parse_jev_prediction(asyncio.run(provider.predict(request_with(down))).raw_response)

        self.assertGreater(bullish.future_return[-1].strong_up, bearish.future_return[-1].strong_up)
        self.assertGreater(bearish.future_return[-1].strong_down, bullish.future_return[-1].strong_down)

    def test_audit_log_records_rule_inputs(self) -> None:
        provider = LocalTrialProvider()
        asyncio.run(provider.predict(request_with(canonical_payload())))
        audit = provider.audit_log()
        self.assertEqual(len(audit), 1)
        self.assertIn("signal", audit[0])
        self.assertIn("return_used", audit[0]["inputs"])
        self.assertIn(60_000, audit[0]["horizon_weight"])

    def test_fail_closed_without_required_facts(self) -> None:
        cases: dict[str, dict[str, object]] = {}
        no_price = canonical_payload(); no_price.pop("price"); cases["price"] = no_price
        no_returns = canonical_payload(); no_returns["returns"] = {}; cases["returns"] = no_returns
        # 规则：imbalance **或** flow 至少有一个（单独缺一仍可用；两者都缺 ⇒ fail closed）
        neither = canonical_payload(); neither.pop("depth"); neither.pop("flow"); cases["depth+flow"] = neither
        for name, payload in cases.items():
            with self.subTest(dropped=name):
                provider = LocalTrialProvider()
                with self.assertRaises(LocalTrialInputError):
                    asyncio.run(provider.predict(request_with(payload)))
                self.assertEqual(provider.audit_log(), ())                # 失败 ⇒ 不产生审计记录

    def test_missing_return_with_other_facts_present_still_fails_closed(self) -> None:
        provider = LocalTrialProvider()
        payload = canonical_payload()
        payload["returns"] = {}
        with self.assertRaises(LocalTrialInputError):
            asyncio.run(provider.predict(request_with(payload)))

    def test_provider_is_refused_outside_local_modes(self) -> None:
        """TESTNET/LIVE 不得使用该 provider（composition root 必须拒绝）。"""
        from product.provenance import ConfigEntry, ConfigSource
        from product.types import Fact, RuntimeMode
        from runtime.assembly import AssemblyError, ProductRuntime, RuntimeProfile

        for mode in (RuntimeMode.TESTNET, RuntimeMode.LIVE):
            with self.subTest(mode=mode.value):
                entries = (ConfigEntry(name="prediction.provider", source=ConfigSource.FILE,
                                       value=Fact.of("local_trial")),
                           ConfigEntry(name="prediction.timeout_ms", source=ConfigSource.FILE, value=Fact.of(1000)),
                           ConfigEntry(name="prediction.ttl_ms", source=ConfigSource.FILE, value=Fact.of(60_000)))
                with self.assertRaises(AssemblyError):
                    ProductRuntime(profile=RuntimeProfile(symbol="BTCUSDT", config_entries=entries, mode=mode))


if __name__ == "__main__":
    unittest.main()
