"""P0001.4.2 单元测试：System One wire 构造 + typed answers 解析。

覆盖 SC-2（4 horizon × 5 分类 Choice）、SC-3（4 个 Noul 落 [0,1]）、SC-4（Noul 无 confidence 不伪造）、
SC-6（未知字段不污染上层）。
"""

from __future__ import annotations

import json
import unittest

from prediction.errors import PredictionInvalidResponseError, PredictionParseError
from prediction.parsing.market_v1 import parse_jev_prediction
from prediction.parsing.systemone import parse_systemone_response
from prediction.schema.market_v1 import (
    FUTURE_RETURN_HORIZONS_MS,
    PROBABILITY_QUESTIONS,
    QUESTION_SCHEMA_VERSION,
    build_jev_payload_json,
)
from prediction.systemone_wire import (
    BINARY_QUESTION_HORIZON_MS,
    BINARY_QUESTION_WIRE_IDS,
    BUY_ADVERSE_SELECTION_QUESTION,
    BUY_FILL_QUESTION,
    SELL_ADVERSE_SELECTION_QUESTION,
    SELL_FILL_QUESTION,
    SYSTEMONE_API_KEY_ENV,
    SYSTEMONE_BASE_URL,
    SYSTEMONE_MODEL_ALIAS,
    SYSTEMONE_PATH,
    build_adverse_selection_question,
    build_fill_question,
    build_future_return_question,
    build_questions,
    build_systemone_body,
    build_systemone_state,
    future_return_question_id,
)
from prediction.types import FUTURE_RETURN_CATEGORIES
from tests.fakes import response_payload
from tests.stub_server import choice_answer, noul_answer, systemone_answers, systemone_envelope
from tests.support import warm_market_states

THRESHOLD_BPS = 5.0


def _body(state=None, **overrides: object) -> dict[str, object]:
    state = state if state is not None else warm_market_states(1)[0]
    payload_json = build_jev_payload_json(state)
    body = build_systemone_body(
        payload_json,
        horizons_ms=FUTURE_RETURN_HORIZONS_MS,
        adverse_selection_threshold_bps=THRESHOLD_BPS,
        **overrides,  # type: ignore[arg-type]
    )
    return json.loads(body)


class WireContractTest(unittest.TestCase):
    def test_fixed_endpoint_and_alias(self) -> None:
        self.assertEqual(SYSTEMONE_BASE_URL, "https://openrouter.ai/api")
        self.assertEqual(SYSTEMONE_PATH, "/v1/systemone")
        self.assertEqual(SYSTEMONE_MODEL_ALIAS, "jev-1.13")
        self.assertEqual(SYSTEMONE_API_KEY_ENV, "OPENROUTER_API_KEY")
        self.assertEqual(BINARY_QUESTION_HORIZON_MS, 5_000)

    def test_future_return_question_ids(self) -> None:
        self.assertEqual(
            [future_return_question_id(horizon) for horizon in FUTURE_RETURN_HORIZONS_MS],
            ["market_5s", "market_15s", "market_30s", "market_60s"],
        )

    def test_choice_question_shape(self) -> None:
        question = build_future_return_question(15_000)
        self.assertEqual(question["type"], "choice")
        self.assertIn("15 seconds", str(question["instructions"]))
        self.assertEqual(set(question["criteria"]), set(FUTURE_RETURN_CATEGORIES))  # type: ignore[arg-type]

    def test_noul_questions_carry_threshold_and_horizon(self) -> None:
        adverse = build_adverse_selection_question("buy", threshold_bps=THRESHOLD_BPS, horizon_ms=5_000)
        self.assertEqual(adverse["type"], "noul")
        self.assertIn("5.0 bps", str(adverse["instructions"]))
        self.assertIn("best bid", str(adverse["instructions"]))
        criteria = adverse["criteria"]
        self.assertEqual(set(criteria), {"true", "false"})  # type: ignore[arg-type]

        sell = build_adverse_selection_question("sell", threshold_bps=THRESHOLD_BPS, horizon_ms=5_000)
        self.assertIn("best ask", str(sell["instructions"]))
        self.assertIn("higher", str(sell["instructions"]))

        fill = build_fill_question("buy", horizon_ms=5_000)
        self.assertEqual(fill["type"], "noul")
        self.assertIn("filled within the next 5 seconds", str(fill["instructions"]))

    def test_invalid_question_arguments_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_adverse_selection_question("both", threshold_bps=5.0, horizon_ms=5_000)
        for bad in (0, -1, True):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    build_adverse_selection_question("buy", threshold_bps=bad, horizon_ms=5_000)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            build_fill_question("both", horizon_ms=5_000)

    def test_question_set_is_four_choice_plus_four_noul(self) -> None:
        questions = build_questions(horizons_ms=FUTURE_RETURN_HORIZONS_MS, adverse_selection_threshold_bps=THRESHOLD_BPS)

        self.assertEqual(len(questions), 8)
        types = sorted(question["type"] for question in questions.values())  # type: ignore[misc]
        self.assertEqual(types, ["choice"] * 4 + ["noul"] * 4)
        self.assertEqual(
            sorted(questions),
            [
                "buy_adverse_selection",
                "buy_fill",
                "market_15s",
                "market_30s",
                "market_5s",
                "market_60s",
                "sell_adverse_selection",
                "sell_fill",
            ],
        )


class SystemOneBodyTest(unittest.TestCase):
    def test_body_has_model_state_and_questions(self) -> None:
        body = _body()
        self.assertEqual(sorted(body), ["model", "questions", "state"])
        self.assertEqual(body["model"], SYSTEMONE_MODEL_ALIAS)
        self.assertEqual(len(body["questions"]), 8)  # type: ignore[arg-type]

    def test_state_is_domain_payload_without_questions(self) -> None:
        state = warm_market_states(1)[0]
        payload = json.loads(build_jev_payload_json(state))
        built = build_systemone_state(build_jev_payload_json(state))

        self.assertNotIn("questions", built)
        for key in payload:
            if key == "questions":
                continue
            with self.subTest(key=key):
                self.assertEqual(built[key], payload[key])
        self.assertEqual(built["market_state_hash"], payload["market_state_hash"])

    def test_body_is_canonical_json(self) -> None:
        state = warm_market_states(1)[0]
        raw = build_systemone_body(
            build_jev_payload_json(state),
            horizons_ms=FUTURE_RETURN_HORIZONS_MS,
            adverse_selection_threshold_bps=THRESHOLD_BPS,
        )
        self.assertEqual(raw, json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def test_no_threshold_no_questions(self) -> None:
        with self.assertRaises(TypeError):
            build_systemone_body(  # type: ignore[call-arg]
                build_jev_payload_json(warm_market_states(1)[0]),
                horizons_ms=FUTURE_RETURN_HORIZONS_MS,
            )

    def test_invalid_inputs_rejected(self) -> None:
        state_payload = build_jev_payload_json(warm_market_states(1)[0])
        with self.assertRaises(ValueError):
            build_systemone_state("{not json")
        with self.assertRaises(ValueError):
            build_systemone_state("[1,2,3]")
        with self.assertRaises(ValueError):
            build_systemone_body(state_payload, model="", horizons_ms=FUTURE_RETURN_HORIZONS_MS, adverse_selection_threshold_bps=1.0)
        with self.assertRaises(ValueError):
            build_systemone_body(state_payload, horizons_ms=(), adverse_selection_threshold_bps=1.0)


class TypedAnswersParserTest(unittest.TestCase):
    def _parse(self, raw: str, **overrides: object):
        return parse_systemone_response(raw, horizons_ms=FUTURE_RETURN_HORIZONS_MS, **overrides)  # type: ignore[arg-type]

    def test_valid_response_maps_to_domain_answers(self) -> None:
        answers = self._parse(systemone_envelope())

        domain = answers.domain_answers
        self.assertEqual(domain["question_schema_version"], QUESTION_SCHEMA_VERSION)
        self.assertEqual(
            sorted(domain),
            [
                "buy_adverse_selection",
                "buy_fill_probability",
                "confidence",
                "future_return",
                "question_schema_version",
                "sell_adverse_selection",
                "sell_fill_probability",
            ],
        )
        self.assertEqual(sorted(domain["future_return"]), ["15s", "30s", "5s", "60s"])  # type: ignore[index]

    def test_sc2_every_horizon_carries_the_full_five_class_distribution(self) -> None:
        probabilities = {"strong_down": 0.1, "down": 0.2, "flat": 0.4, "up": 0.2, "strong_up": 0.1}
        answers = self._parse(systemone_envelope(systemone_answers(choice_probabilities=probabilities)))

        for horizon, buckets in answers.domain_answers["future_return"].items():  # type: ignore[union-attr]
            with self.subTest(horizon=horizon):
                self.assertEqual(set(buckets), set(FUTURE_RETURN_CATEGORIES))
                self.assertAlmostEqual(sum(buckets.values()), 1.0)

    def test_sc2_distribution_survives_the_existing_strict_parser(self) -> None:
        # domain answers 必须能被既有 parser 直接消费（这正是「不改 Runtime」的关键）
        answers = self._parse(systemone_envelope())
        prediction = parse_jev_prediction(json.dumps(answers.domain_answers))

        distribution = prediction.distribution(5_000)
        assert distribution is not None
        self.assertAlmostEqual(distribution.probability_sum, 1.0)
        self.assertEqual(len(prediction.future_return), 4)

    def test_sc3_noul_probabilities_map_to_the_four_binary_questions(self) -> None:
        answers = self._parse(systemone_envelope(systemone_answers(noul_probability=0.81)))

        for question in PROBABILITY_QUESTIONS:
            with self.subTest(question=question):
                value = answers.domain_answers[question]
                self.assertIsInstance(value, float)
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)
                self.assertEqual(value, 0.81)

    def test_sc3_wire_ids_map_to_the_correct_domain_questions(self) -> None:
        self.assertEqual(
            BINARY_QUESTION_WIRE_IDS,
            {
                "buy_adverse_selection": BUY_ADVERSE_SELECTION_QUESTION,
                "sell_adverse_selection": SELL_ADVERSE_SELECTION_QUESTION,
                "buy_fill_probability": BUY_FILL_QUESTION,
                "sell_fill_probability": SELL_FILL_QUESTION,
            },
        )

    def test_sc4_noul_has_no_confidence_and_none_is_fabricated(self) -> None:
        raw = systemone_envelope(
            {
                **systemone_answers(),
                BUY_FILL_QUESTION: {"type": "noul", "noul": 0.5},  # 无 confidence 字段
            }
        )
        answers = self._parse(raw)
        prediction = parse_jev_prediction(json.dumps(answers.domain_answers))

        # provider_confidence 只来自最近 horizon 的 Choice；Noul 不参与
        self.assertEqual(prediction.provider_confidence, 0.42)
        self.assertIsNotNone(prediction.derived_confidence)

    def test_sc4_missing_choice_confidence_yields_no_provider_confidence(self) -> None:
        raw = systemone_envelope(systemone_answers(confidence=None))
        answers = self._parse(raw)
        self.assertNotIn("confidence", answers.domain_answers)

        prediction = parse_jev_prediction(json.dumps(answers.domain_answers))
        self.assertIsNone(prediction.provider_confidence)
        self.assertIsNotNone(prediction.derived_confidence)

    def test_sc4_noul_confidence_is_never_used(self) -> None:
        raw = systemone_envelope(
            {**systemone_answers(), BUY_FILL_QUESTION: {"type": "noul", "noul": 0.2, "confidence": 0.99}}
        )
        answers = self._parse(raw)
        prediction = parse_jev_prediction(json.dumps(answers.domain_answers))

        self.assertEqual(answers.domain_answers["buy_fill_probability"], 0.2)
        self.assertEqual(prediction.provider_confidence, 0.42)  # 仍来自 Choice

    def test_sc6_unknown_fields_do_not_pollute_domain_answers(self) -> None:
        raw = systemone_envelope(
            {
                **systemone_answers(
                    overrides={
                        BUY_FILL_QUESTION: {"type": "noul", "noul": 0.4, "advice": "BUY", "reasoning": "because"},
                        future_return_question_id(5_000): {
                            **choice_answer(),
                            "explanation": "prose",
                            "probabilities": {
                                "strong_down": 0.0,
                                "down": 0.0,
                                "flat": 1.0,
                                "up": 0.0,
                                "strong_up": 0.0,
                                "sideways": 0.0,
                            },
                        },
                    }
                )
            },
            extra={"system_fingerprint": "fp-1", "service_tier": "default", "reasoning": {"hidden": True}},
        )
        answers = self._parse(raw)

        self.assertEqual(
            sorted(answers.domain_answers),
            [
                "buy_adverse_selection",
                "buy_fill_probability",
                "confidence",
                "future_return",
                "question_schema_version",
                "sell_adverse_selection",
                "sell_fill_probability",
            ],
        )
        self.assertEqual(sorted(answers.domain_answers["future_return"]["5s"]), sorted(FUTURE_RETURN_CATEGORIES))  # type: ignore[index]
        surface = json.dumps(answers.domain_answers)
        for leaked in ("advice", "reasoning", "explanation", "sideways", "fingerprint"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, surface)

    def test_evidence_fields_are_extracted(self) -> None:
        answers = self._parse(
            systemone_envelope(
                model="typesafe/jev-1.13-20260917",
                provider="TypeSafe",
                response_id="gen-dec-abc",
                usage={"input_tokens": 277, "output_tokens": 20, "cost": 1.1634e-05, "unknown": "x"},
            )
        )

        self.assertEqual(answers.resolved_model, "typesafe/jev-1.13-20260917")
        self.assertEqual(answers.provider, "TypeSafe")
        self.assertEqual(answers.response_id, "gen-dec-abc")
        assert answers.usage is not None
        self.assertEqual(answers.usage.input_tokens, 277)
        self.assertEqual(answers.usage.output_tokens, 20)
        self.assertAlmostEqual(answers.usage.cost or 0.0, 1.1634e-05)

    def test_missing_or_partial_usage_is_tolerated(self) -> None:
        raw = systemone_envelope(usage={})
        answers = self._parse(raw)
        assert answers.usage is not None
        self.assertIsNone(answers.usage.input_tokens)
        self.assertIsNone(answers.usage.cost)

        no_usage = json.dumps({k: v for k, v in json.loads(systemone_envelope()).items() if k != "usage"})
        self.assertIsNone(self._parse(no_usage).usage)

    def test_missing_evidence_fields_become_none(self) -> None:
        raw = json.dumps({"answers": systemone_answers()})
        answers = self._parse(raw)
        self.assertIsNone(answers.resolved_model)
        self.assertIsNone(answers.provider)
        self.assertIsNone(answers.response_id)


class TypedAnswersParserFailureTest(unittest.TestCase):
    def _assert_invalid(self, raw: str, expected: type[Exception] = PredictionInvalidResponseError) -> None:
        with self.assertRaises(expected):
            parse_systemone_response(raw, horizons_ms=FUTURE_RETURN_HORIZONS_MS)

    def test_empty_and_malformed_bodies(self) -> None:
        # 空响应 = provider 契约违反 → INVALID_RESPONSE；语法错误/非对象 = PARSE_ERROR
        self._assert_invalid("", PredictionInvalidResponseError)
        self._assert_invalid("   ", PredictionInvalidResponseError)
        self._assert_invalid("not json", PredictionParseError)
        self._assert_invalid("[1,2,3]", PredictionParseError)
        self._assert_invalid("42", PredictionParseError)

    def test_missing_answers_object(self) -> None:
        self._assert_invalid(json.dumps({"id": "x", "model": "m"}))

    def test_missing_question_answer(self) -> None:
        answers = systemone_answers()
        del answers[BUY_FILL_QUESTION]
        self._assert_invalid(systemone_envelope(answers))

    def test_missing_horizon_answer(self) -> None:
        answers = systemone_answers()
        del answers[future_return_question_id(30_000)]
        self._assert_invalid(systemone_envelope(answers))

    def test_wrong_answer_type(self) -> None:
        # Choice 问题返回 noul → 拒绝
        raw = systemone_envelope(
            {**systemone_answers(), future_return_question_id(5_000): noul_answer()}
        )
        self._assert_invalid(raw)
        # Noul 问题返回 choice → 拒绝
        raw = systemone_envelope({**systemone_answers(), BUY_FILL_QUESTION: choice_answer()})
        self._assert_invalid(raw)
        # 未知 type（Score 不在本阶段契约）→ 拒绝
        raw = systemone_envelope({**systemone_answers(), BUY_FILL_QUESTION: {"type": "score", "score": 0.5}})
        self._assert_invalid(raw)

    def test_choice_requires_probabilities(self) -> None:
        raw = systemone_envelope(
            {**systemone_answers(), future_return_question_id(5_000): {"type": "choice", "choice": "flat"}}
        )
        self._assert_invalid(raw)

    def test_missing_category_is_rejected(self) -> None:
        # 故意构造缺一个 bucket 的 Choice 答案（绕过测试 helper 的完整性检查）
        incomplete = {"type": "choice", "choice": "flat",
                      "probabilities": {"strong_down": 0.0, "down": 0.0, "flat": 0.5, "up": 0.5},
                      "confidence": 0.3}
        raw = systemone_envelope({**systemone_answers(), future_return_question_id(5_000): incomplete})
        self._assert_invalid(raw)

    def test_probability_sum_violation_is_rejected(self) -> None:
        raw = systemone_envelope(
            systemone_answers(
                choice_probabilities={"strong_down": 0.5, "down": 0.5, "flat": 0.5, "up": 0.0, "strong_up": 0.0}
            )
        )
        self._assert_invalid(raw)

    def test_probability_sum_within_tolerance_is_accepted(self) -> None:
        raw = systemone_envelope(
            systemone_answers(
                choice_probabilities={
                    "strong_down": 0.05,
                    "down": 0.15,
                    "flat": 0.6 + 1e-9,
                    "up": 0.15,
                    "strong_up": 0.05,
                }
            )
        )
        answers = parse_systemone_response(raw, horizons_ms=FUTURE_RETURN_HORIZONS_MS)
        self.assertEqual(len(answers.domain_answers["future_return"]), 4)  # type: ignore[arg-type]

    def test_out_of_range_and_non_numeric_probabilities_are_rejected(self) -> None:
        for value in (-0.1, 1.5, "0.5", None, True):
            with self.subTest(value=value):
                raw = systemone_envelope({**systemone_answers(), BUY_FILL_QUESTION: {"type": "noul", "noul": value}})
                self._assert_invalid(raw)

    def test_non_finite_probabilities_are_rejected(self) -> None:
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                raw = systemone_envelope().replace('"noul":0.73', f'"noul":{literal}', 1)
                self._assert_invalid(raw)

    def test_duplicate_keys_are_rejected(self) -> None:
        raw = (
            '{"model":"typesafe/x","answers":{"buy_fill":{"type":"noul","noul":0.5},'
            '"buy_fill":{"type":"noul","noul":0.6}}}'
        )
        self._assert_invalid(raw)

    def test_choice_label_must_be_a_known_category(self) -> None:
        raw = systemone_envelope(
            {**systemone_answers(), future_return_question_id(5_000): choice_answer(choice="sideways")}
        )
        self._assert_invalid(raw)


class ProviderConstructionTest(unittest.TestCase):
    def test_threshold_is_required_and_validated(self) -> None:
        from prediction.providers.systemone import SystemOneProvider, SystemOneTransport

        transport = SystemOneTransport(api_key="k", base_url="http://127.0.0.1:1")
        with self.assertRaises(TypeError):
            SystemOneProvider(transport)  # type: ignore[call-arg]
        for bad in (0, -1, True, "5"):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    SystemOneProvider(transport, adverse_selection_threshold_bps=bad)  # type: ignore[arg-type]

    def test_defaults(self) -> None:
        from prediction.providers.systemone import SystemOneProvider, SystemOneTransport

        provider = SystemOneProvider(
            SystemOneTransport(api_key="k"), adverse_selection_threshold_bps=THRESHOLD_BPS
        )
        self.assertEqual(provider.model, SYSTEMONE_MODEL_ALIAS)
        self.assertEqual(provider.horizons_ms, FUTURE_RETURN_HORIZONS_MS)
        self.assertEqual(provider.adverse_selection_threshold_bps, THRESHOLD_BPS)

    def test_transport_configuration_validation(self) -> None:
        from prediction.providers.systemone import SystemOneTransport

        with self.assertRaises(ValueError):
            SystemOneTransport(base_url="ftp://x")
        with self.assertRaises(ValueError):
            SystemOneTransport(path="v1/systemone")
        with self.assertRaises(ValueError):
            SystemOneTransport(timeout_s=0)
        with self.assertRaises(ValueError):
            SystemOneTransport(api_key="", api_key_env="")
        transport = SystemOneTransport(api_key="k")
        self.assertEqual(transport.endpoint, "https://openrouter.ai/api/v1/systemone")


if __name__ == "__main__":
    unittest.main()
