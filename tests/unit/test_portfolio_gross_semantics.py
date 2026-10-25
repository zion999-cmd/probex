"""人类审查 P2（2026-10-24）：组合 projected gross 的产品语义守卫。

`committed/projected gross` 是**保守上界**（持仓名义 + 在途订单最坏情况名义；
反手非 reduce-only 单按整单计入），不是"当前真实组合敞口"。
任何 API/UI 可见文案都不得把它表述成真实当前敞口。

本测试钉死：
1. `PORTFOLIO_NOTIONAL_LIMIT` catalog 解释（zh/en）必须显式标注 projected/上界 +
   "非当前真实敞口"；
2. 静态扫描 product/ api/ ui/：凡提到 gross 的人读字符串必须带 projected/committed/
   upper-bound 限定词；禁止"当前真实组合敞口/current real gross"类肯定表述。
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

from product.reason_catalog import lookup

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# 只识别"组合 gross 名义/敞口"语义；gross_profit/gross_loss（Profit Factor 会计术语）不属此列
_NEIGHBOR = r"notional|exposure|portfolio|敞口|名义|组合"
PORTFOLIO_GROSS_RE = re.compile(
    rf"(?:gross[^.\n]{{0,40}}(?:{_NEIGHBOR}))|(?:(?:{_NEIGHBOR})[^.\n]{{0,40}}gross)",
    re.IGNORECASE)
QUALIFIERS = ("projected", "committed", "upper bound", "worst-case", "pending",
              "limit", "上界", "在途", "最坏", "限额", "预算")

# 肯定式误述（出现即失败；带否定语境的同一短语见下）
FORBIDDEN_PATTERNS = (
    re.compile(r"current\s+real\s+(?:portfolio\s+)?gross", re.IGNORECASE),
    re.compile(r"real[- ]?time\s+gross", re.IGNORECASE),
    re.compile(r"actual\s+(?:portfolio\s+)?gross", re.IGNORECASE),
    # "the current real portfolio exposure" 只允许出现在 "not the ..." 否定语境
    re.compile(r"(?<!the )current real portfolio exposure", re.IGNORECASE),
    re.compile(r"(?<!非当前)真实组合(?:总)?(?:gross|敞口|名义)"),
    re.compile(r"(?<!非)当前真实组合(?:总)?(?:敞口|名义)"),
)


def _python_literals(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield path, node.value


def _js_literals(path: pathlib.Path):
    text = path.read_text(encoding="utf-8")
    # 去掉模板插值里的代码，只保留字面文本
    text = re.sub(r"\$\{[^}]*\}", " ", text)
    for match in re.finditer(r"(['\"`])(?:(?!\1).)*\1", text, re.DOTALL):
        yield path, match.group(0)[1:-1]


def _human_facing_literals():
    # 产品读模型 / API / 报告的所有 Python 字符串字面量
    for directory in ("product", "api", "reports"):
        base = REPO_ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            yield from _python_literals(path)
    # UI（含 .html 文本节点：按属性/标签切分后只看可见文本较复杂，这里整文件抽取引号串）
    for path in (REPO_ROOT / "ui").rglob("*"):
        if path.suffix in (".js", ".mjs"):
            yield from _js_literals(path)
        elif path.suffix == ".html":
            yield path, path.read_text(encoding="utf-8")


class PortfolioGrossSemanticsTest(unittest.TestCase):
    def test_catalog_labels_projected_gross_as_upper_bound_not_real_exposure(self) -> None:
        entry = lookup("PORTFOLIO_NOTIONAL_LIMIT")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertIn("projected", entry.explanation)
        self.assertIn("上界", entry.explanation)
        self.assertIn("非当前真实", entry.explanation)
        self.assertIn("upper bound", entry.explanation_en.lower())
        self.assertIn("projected", entry.explanation_en.lower())
        self.assertIn("not the current real", entry.explanation_en.lower())

    def test_every_gross_label_is_qualified_as_projection_or_bound(self) -> None:
        offenders = []
        for path, literal in _human_facing_literals():
            if PORTFOLIO_GROSS_RE.search(literal):
                if not any(q in literal.lower() for q in QUALIFIERS):
                    offenders.append((path, literal))
        self.assertEqual(offenders, [], f"unqualified gross labels: {offenders[:5]}")

    def test_no_surface_affirms_gross_as_current_real_exposure(self) -> None:
        hits = []
        for path, literal in _human_facing_literals():
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(literal):
                    hits.append((path, literal.strip()[:120], pattern.pattern))
        self.assertEqual(hits, [], f"gross mislabelled as real exposure: {hits[:5]}")


if __name__ == "__main__":
    unittest.main()
