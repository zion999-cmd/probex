"""P0001.11 §1 / 裁决 C：Config Provenance 契约。"""

from __future__ import annotations

import unittest

from product.provenance import (
    PROVENANCE_SCHEMA_VERSION,
    ConfigEntry,
    ConfigProvenanceError,
    ConfigSource,
    build_config_snapshot,
    config_fingerprint,
    resolve_config,
    secret_entry,
)
from product.types import SCHEMA_VERSION, Fact


def entry(name: str, value: object, source: ConfigSource = ConfigSource.FILE) -> ConfigEntry:
    return ConfigEntry(name=name, source=source, value=Fact.of(value))


class ProvenanceResolutionTest(unittest.TestCase):
    def test_precedence_is_cli_env_file_constructor(self) -> None:
        resolved = resolve_config([
            entry("max_position_qty", 1.0, ConfigSource.CONSTRUCTOR),
            entry("max_position_qty", 2.0, ConfigSource.FILE),
            entry("max_position_qty", 3.0, ConfigSource.ENV),
            entry("max_position_qty", 4.0, ConfigSource.CLI),
        ])

        self.assertEqual(len(resolved), 1)
        self.assertIs(resolved[0].source, ConfigSource.CLI)
        self.assertEqual(resolved[0].value.value, 4.0)

    def test_no_implicit_default_is_created(self) -> None:
        """裁决 C：业务值仍不得有隐式默认 —— 没有候选项就是没有。"""
        resolved = resolve_config([entry("symbol", "BTCUSDT")])

        self.assertEqual([item.name for item in resolved], ["symbol"])
        self.assertIsNone(next((item for item in resolved if item.name == "max_daily_loss"), None))

    def test_duplicate_same_source_is_refused(self) -> None:
        with self.assertRaises(ConfigProvenanceError):
            resolve_config([entry("symbol", "BTCUSDT"), entry("symbol", "ETHUSDT")])

    def test_resolution_order_is_deterministic(self) -> None:
        first = resolve_config([entry("b", 2), entry("a", 1)])
        second = resolve_config([entry("a", 1), entry("b", 2)])

        self.assertEqual([item.name for item in first], ["a", "b"])
        self.assertEqual([item.name for item in second], ["a", "b"])


class ProvenanceSecretTest(unittest.TestCase):
    def test_secret_entries_only_carry_reference_names(self) -> None:
        secret = secret_entry("binance_api_key", source=ConfigSource.ENV, secret_ref="env:BINANCE_API_KEY")

        self.assertTrue(secret.sensitive)
        self.assertEqual(secret.secret_ref, "env:BINANCE_API_KEY")
        self.assertFalse(secret.value.known)
        self.assertIsNone(secret.value.value)

    def test_sensitive_value_is_never_stored(self) -> None:
        with self.assertRaises(ConfigProvenanceError):
            ConfigEntry(name="binance_api_key", source=ConfigSource.ENV, value=Fact.of("s3cr3t"),
                        secret_ref="env:BINANCE_API_KEY", sensitive=True)

    def test_secret_ref_must_be_a_reference_not_a_value(self) -> None:
        with self.assertRaises(ConfigProvenanceError):
            ConfigEntry(name="x", source=ConfigSource.ENV, value=Fact.unknown("sensitive value is never stored"),
                        secret_ref="s3cr3t-value", sensitive=True)

    def test_snapshot_exposes_refs_but_never_values(self) -> None:
        snapshot = build_config_snapshot(
            config_id="cfg-1",
            entries=[entry("symbol", "BTCUSDT"),
                     secret_entry("api_key", source=ConfigSource.ENV, secret_ref="env:BINANCE_API_KEY")],
            created_at=1_000,
        )

        self.assertEqual(snapshot.secret_refs(), ("env:BINANCE_API_KEY",))
        payload = [item.value.value for item in snapshot.entries if item.sensitive]
        self.assertEqual(payload, [None])


class ProvenanceFingerprintTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_order_independent(self) -> None:
        first = config_fingerprint(resolve_config([entry("a", 1), entry("b", 2)]))
        second = config_fingerprint(resolve_config([entry("b", 2), entry("a", 1)]))

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_fingerprint_changes_with_values_sources_and_secret_refs(self) -> None:
        base = config_fingerprint(resolve_config([entry("a", 1)]))
        changed_value = config_fingerprint(resolve_config([entry("a", 2)]))
        changed_source = config_fingerprint(resolve_config([entry("a", 1, ConfigSource.ENV)]))
        with_secret = config_fingerprint(resolve_config(
            [entry("a", 1), secret_entry("k", source=ConfigSource.ENV, secret_ref="env:K")]))

        self.assertNotEqual(base, changed_value)
        self.assertNotEqual(base, changed_source)
        self.assertNotEqual(base, with_secret)

    def test_fingerprint_covers_schema_versions(self) -> None:
        import product.provenance as provenance

        original = provenance.PROVENANCE_SCHEMA_VERSION
        try:
            provenance.PROVENANCE_SCHEMA_VERSION = "99"
            mutated = config_fingerprint(resolve_config([entry("a", 1)]))
        finally:
            provenance.PROVENANCE_SCHEMA_VERSION = original
        self.assertNotEqual(mutated, config_fingerprint(resolve_config([entry("a", 1)])))
        self.assertEqual(original, "1")
        self.assertEqual(SCHEMA_VERSION, "5")

    def test_snapshot_records_id_and_sources(self) -> None:
        snapshot = build_config_snapshot(
            config_id="cfg-9",
            entries=[entry("symbol", "BTCUSDT"), entry("threshold", 5.0, ConfigSource.ENV)],
            created_at=42,
        )

        self.assertEqual(snapshot.config_id, "cfg-9")
        self.assertEqual(snapshot.sources()["ENV"], 1)
        self.assertEqual(snapshot.sources()["FILE"], 1)
        self.assertEqual(snapshot.created_at, 42)
        self.assertEqual(snapshot.fingerprint, config_fingerprint(snapshot.entries))
