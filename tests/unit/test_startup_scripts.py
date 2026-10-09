"""启动脚本与运行手册的架构守卫（不是功能测试）。

守卫目标（防止"便利脚本"变成第二条执行路径或凭据泄漏通道）：

1. `scripts/probex.sh` 必须是 `bash` + `set -euo pipefail`，且 `bash -n` 语法通过；
2. 脚本只允许调用**既有**入口（`python3 -m runtime.assembly` / `python3 -m cli` /
   `tests/ui/local_paper_demo.py` / `scripts/make_event_store.py`）；
3. 脚本**不得**引用真实写路径（acceptance driver / 下单 / TESTNET-LIVE 模式 / 凭据变量名）；
4. up 必须拒绝非 paper/replay 模式；默认产物目录必须在仓库之外；
5. `docs/RUNBOOK.md` / `README.md` 必须存在且保留安全边界。
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "scripts"
RUNBOOK = PROJECT_ROOT / "docs" / "RUNBOOK.md"
README = PROJECT_ROOT / "README.md"
MAIN = SCRIPTS / "probex.sh"

ALLOWED_ENTRYPOINTS = (
    "python3 -m runtime.assembly",
    "python3 -m cli",
    "python3 tests/ui/local_paper_demo.py",
    "python3 scripts/make_event_store.py",
)

FORBIDDEN = (
    "testnet_acceptance_run",
    "place_order",
    "cancel_order",
    "capital.",
    "binance_api_secret",
    "binance_api_key",
    "--mode testnet",
    "--mode live",
    "PROBEX_TESTNET_ACCEPTANCE",
)


class StartupScriptsTest(unittest.TestCase):
    def test_single_entrypoint_script_is_strict_and_syntax_clean(self) -> None:
        text = MAIN.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", text)
        result = subprocess.run(["bash", "-n", str(MAIN)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"bash -n failed: {result.stderr}")
        self.assertTrue(os.access(MAIN, os.X_OK), "probex.sh must be executable")

    def test_scripts_never_touch_the_real_write_path(self) -> None:
        for script in (MAIN, SCRIPTS / "make_event_store.py"):
            lowered = script.read_text(encoding="utf-8").lower()
            for needle in FORBIDDEN:
                self.assertNotIn(needle.lower(), lowered,
                                 f"{script.name}: forbidden reference {needle!r}")

    def test_scripts_only_invoke_existing_entrypoints(self) -> None:
        text = MAIN.read_text(encoding="utf-8")
        for match in re.findall(r"python3\s+(-m\s+[\w.]+|[\w./-]+\.py)", text):
            entry = f"python3 {match}".strip()
            self.assertIn(entry, ALLOWED_ENTRYPOINTS, f"unexpected entrypoint {entry!r}")

    def test_up_refuses_non_local_modes(self) -> None:
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn("paper) port=", text)
        self.assertIn("replay) port=", text)
        self.assertIn("--mode must be paper or replay", text)

    def test_default_local_dir_is_outside_the_repository(self) -> None:
        text = MAIN.read_text(encoding="utf-8")
        self.assertIn("${HOME}/.probex/local-run", text)
        self.assertNotIn("${REPO_ROOT}/.local-run", text)

    def test_event_store_helper_uses_the_local_fixture_only(self) -> None:
        text = (SCRIPTS / "make_event_store.py").read_text(encoding="utf-8")
        self.assertIn("from tests.ui.market_fixture import write_market_store", text)
        self.assertIn("not real market evidence", text)

    def test_docs_exist_and_keep_the_safety_boundaries(self) -> None:
        self.assertTrue(RUNBOOK.is_file())
        self.assertTrue(README.is_file())
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for needle in ("LOCAL TRIAL", "PROBEX_TESTNET_CONFIRM=1", "PROBEX_TESTNET_ACCEPTANCE=1",
                       "未收口", "不要运行", "scripts/probex.sh"):
            self.assertIn(needle, runbook, f"RUNBOOK must mention {needle!r}")
        readme = README.read_text(encoding="utf-8")
        self.assertIn("docs/RUNBOOK.md", readme)
        self.assertIn("unavailable_by_design", readme)

    def test_runbook_documents_the_verified_default_ports(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for port in ("8899", "8898", "8897"):
            self.assertIn(port, runbook)


if __name__ == "__main__":
    unittest.main()
