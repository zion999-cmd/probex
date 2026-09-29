"""F-13 Config Resolver：CLI > ENV > FILE > CONSTRUCTOR，secret 只留引用名。"""

from __future__ import annotations

import unittest

from product.provenance import (ENV_CONFIG_PREFIX, ENV_SECRET_PREFIX, ConfigEntry, ConfigSource,
                                build_config_snapshot, env_config_entries, resolve_config)
from product.types import Fact


def entry(name: str, value: object, source: ConfigSource) -> ConfigEntry:
    return ConfigEntry(name=name, source=source, value=Fact.of(value))


class EnvLayerTest(unittest.TestCase):
    def test_prefixes_are_stable(self) -> None:
        self.assertEqual(ENV_CONFIG_PREFIX, "PROBEX_CONFIG_")
        self.assertEqual(ENV_SECRET_PREFIX, "PROBEX_SECRET_")

    def test_env_config_parses_json_and_names(self) -> None:
        entries = env_config_entries({
            "PROBEX_CONFIG_PROJECTION__MAX_POINTS": "150",
            "PROBEX_CONFIG_SAFETY__FLAG": "true",
            "PROBEX_CONFIG_VENUE__NOTE": "plain-text",
            "UNRELATED": "ignored",
        })
        resolved = {item.name: item.value.value for item in entries}
        self.assertEqual(resolved["projection.max_points"], 150)
        self.assertIs(resolved["safety.flag"], True)
        self.assertEqual(resolved["venue.note"], "plain-text")
        self.assertNotIn("unrelated", resolved)

    def test_env_secret_is_reference_only(self) -> None:
        entries = env_config_entries({"PROBEX_SECRET_BINANCE__API_KEY": "BINANCE_API_KEY"})
        self.assertEqual(len(entries), 1)
        secret = entries[0]
        self.assertTrue(secret.sensitive)
        self.assertFalse(secret.value.known)                       # 值永远不记录
        self.assertEqual(secret.secret_ref, "env:BINANCE_API_KEY")

    def test_env_secret_without_declared_value_uses_its_own_name(self) -> None:
        entries = env_config_entries({"PROBEX_SECRET_MY_TOKEN": ""})
        self.assertEqual(entries[0].secret_ref, "env:MY_TOKEN")


class PrecedenceTest(unittest.TestCase):
    def test_cli_beats_env_beats_file_beats_constructor(self) -> None:
        candidates = [
            entry("a", "constructor", ConfigSource.CONSTRUCTOR),
            entry("a", "file", ConfigSource.FILE),
            entry("a", "env", ConfigSource.ENV),
            entry("a", "cli", ConfigSource.CLI),
            entry("b", "file", ConfigSource.FILE),
            entry("b", "env", ConfigSource.ENV),
            entry("c", "constructor", ConfigSource.CONSTRUCTOR),
        ]
        resolved = {item.name: (item.source.value, item.value.value) for item in resolve_config(candidates)}
        self.assertEqual(resolved["a"], ("CLI", "cli"))
        self.assertEqual(resolved["b"], ("ENV", "env"))
        self.assertEqual(resolved["c"], ("CONSTRUCTOR", "constructor"))

    def test_duplicate_same_source_is_refused(self) -> None:
        with self.assertRaises(Exception):
            resolve_config([entry("a", 1, ConfigSource.FILE), entry("a", 2, ConfigSource.FILE)])

    def test_resolution_is_deterministic_and_name_sorted(self) -> None:
        candidates = [entry("z", 1, ConfigSource.FILE), entry("a", 2, ConfigSource.FILE)]
        self.assertEqual([item.name for item in resolve_config(candidates)], ["a", "z"])


class SnapshotTest(unittest.TestCase):
    def test_fingerprint_tracks_source_and_value(self) -> None:
        left = build_config_snapshot(config_id="x", created_at=1,
                                     entries=[entry("a", 1, ConfigSource.FILE)])
        right = build_config_snapshot(config_id="x", created_at=1,
                                      entries=[entry("a", 2, ConfigSource.FILE)])
        same = build_config_snapshot(config_id="y", created_at=2,
                                     entries=[entry("a", 1, ConfigSource.FILE)])
        self.assertNotEqual(left.fingerprint, right.fingerprint)
        self.assertEqual(left.fingerprint, same.fingerprint)       # fingerpint 与 id/时间无关

    def test_secret_refs_are_listed_but_values_are_not(self) -> None:
        secret = env_config_entries({"PROBEX_SECRET_BINANCE__API_KEY": "BINANCE_API_KEY"})[0]
        snapshot = build_config_snapshot(config_id="x", created_at=1,
                                         entries=[secret, entry("a", 1, ConfigSource.FILE)])
        self.assertEqual(snapshot.secret_refs(), ("env:BINANCE_API_KEY",))
        stored = snapshot.entry("binance.api_key")   # `__` -> `.`，单个 `_` 保留
        self.assertIsNotNone(stored)
        self.assertFalse(stored.value.known)
        self.assertNotIn("secret-value", str(snapshot.fingerprint))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
