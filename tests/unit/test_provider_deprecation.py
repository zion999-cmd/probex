"""SC-10：Chat Completions 热路径（`typesafe/jev-router`）已被正式废弃。

保留实验记录，但不得再作为热路径入口被误用。
"""

from __future__ import annotations

import unittest

import prediction
import prediction.providers
from prediction.providers import openrouter


class DeprecatedChatPathTest(unittest.TestCase):
    def test_module_is_marked_deprecated(self) -> None:
        self.assertIs(openrouter.DEPRECATED, True)
        self.assertIn("stealth/space-bunny-alpha", openrouter.DEPRECATION_REASON)
        self.assertIn("SystemOneProvider", openrouter.DEPRECATION_REASON)
        self.assertIn("DEPRECATED", openrouter.__doc__ or "")

    def test_not_exported_from_the_package_namespace(self) -> None:
        for name in ("OpenRouterTransport", "OpenRouterCall", "OPENROUTER_MODEL", "OPENROUTER_ENDPOINT"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(prediction, name), f"{name} 不应再从 prediction 包导出")
                self.assertFalse(hasattr(prediction.providers, name), f"{name} 不应再从 providers 包导出")
                self.assertNotIn(name, prediction.__all__)
                self.assertNotIn(name, prediction.providers.__all__)

    def test_hot_path_provider_is_exported(self) -> None:
        for name in ("SystemOneProvider", "SystemOneTransport", "SystemOneCall"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(prediction, name))
                self.assertIn(name, prediction.__all__)
                self.assertIn(name, prediction.providers.__all__)

    def test_experiment_record_is_kept(self) -> None:
        # 模块本身（P0001.4.1 的实验记录）必须仍在仓库中可按完整路径导入
        from prediction.providers.openrouter import OpenRouterTransport

        self.assertTrue(callable(OpenRouterTransport))

    def test_hot_path_endpoint_is_systemone_not_chat_completions(self) -> None:
        from prediction.systemone_wire import SYSTEMONE_BASE_URL, SYSTEMONE_PATH

        self.assertEqual(f"{SYSTEMONE_BASE_URL}{SYSTEMONE_PATH}", "https://openrouter.ai/api/v1/systemone")
        self.assertNotIn("chat/completions", f"{SYSTEMONE_BASE_URL}{SYSTEMONE_PATH}")


if __name__ == "__main__":
    unittest.main()
