"""UI module hygiene：页面不得引用未导入的共享 helper（防"文件存在但打不开"）。

起因：closure Slice 4 曾出现 Monitor 页面使用 `SURFACES` 但未 import（浏览器侧 ReferenceError），
结构性字符串测试无法发现 ⇒ 本测试做静态的"使用即需导入/定义"检查。

为避免 HTML / 文案误报，检查前先剥离注释与字符串（含模板字符串）。
"""

from __future__ import annotations

import pathlib
import re
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
UI_ROOT = PROJECT_ROOT / "ui"

SHARED_IDENTIFIERS = (
    "SURFACES", "LEGACY_PAGE_HOME", "ENDPOINTS", "fact", "factRows", "rows", "section", "table",
    "escapeHtml", "reasonCell", "fetchJson", "fetchSnapshot", "fetchOrUnavailable", "fetchRunSummary",
    "reasonCatalog", "postReplay", "BLOCKER_SECTION", "DETAIL_ROUTES", "DETAIL_PAGE_MODULES",
    "surfaceHash", "entityHash", "marketPointHash", "resolveRoute", "surfaceModule", "surfaceForHash",
    "drawHeatmap", "HEATMAP_NOTE", "featurePanels", "healthStrip", "replayControls", "tradesTable",
    "decisionTable", "executionTable", "boundsNote",
)


def _strip_literals(source: str) -> str:
    """剥离注释与字符串字面量（含模板字符串），保留代码标识符。"""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        char = source[i]
        if char == "/" and i + 1 < n and source[i + 1] == "/":
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif char == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char in ("\"", "'", "`"):
            quote = char
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _defined(source: str, name: str) -> bool:
    patterns = (
        rf"export\s+(?:async\s+)?function\s+{name}\b",
        rf"(?:export\s+)?(?:const|let|var)\s+{name}\b",
        rf"(?:async\s+)?function\s+{name}\b",
        rf"export\s*\{{[^}}]*\b{name}\b[^}}]*\}}",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def _imported(source: str, name: str) -> bool:
    for match in re.finditer(r"import\s+(.+?)\s+from\s+[\"']", source, flags=re.DOTALL):
        if re.search(rf"\b{name}\b", match.group(1)):
            return True
    return False


class UiModuleHygieneTest(unittest.TestCase):
    def test_shared_helpers_are_imported_or_defined(self) -> None:
        for path in sorted(UI_ROOT.rglob("*.js")):
            source = path.read_text(encoding="utf-8")
            code = _strip_literals(source)
            for name in SHARED_IDENTIFIERS:
                if not re.search(rf"\b{name}\b", code):
                    continue
                with self.subTest(module=str(path.relative_to(PROJECT_ROOT)), name=name):
                    self.assertTrue(_imported(source, name) or _defined(source, name),
                                    f"{path.name} uses {name} without importing/defining it")

    def test_console_polls_the_surface_and_is_interaction_safe(self) -> None:
        """锁定正文轮询契约：定时刷新 + 非破坏 refresh 优先 + 交互保护。"""
        console = (UI_ROOT / "client" / "console.js").read_text(encoding="utf-8")
        self.assertIn("SURFACE_REFRESH_MS", console)
        self.assertIn("maybeRefreshSurface", console)
        self.assertIn("surfaceInteractionPaused", console)
        # 轮询优先使用页面的非破坏 refresh，缺省才整体重渲染
        self.assertIn("module.refresh", console)
        # market 页面必须提供非破坏 refresh（保留用户画线）
        market = (UI_ROOT / "pages" / "market" / "page.js").read_text(encoding="utf-8")
        self.assertRegex(market, r"export\s+async\s+function\s+refresh\b")
        self.assertIn("liveWorkbench.refresh", market)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
