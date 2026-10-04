"""P0001.15 §G 架构守卫：connector / instrument semantics 的依赖边界。

结构性地保证：

- Strategy **不** import Binance connector / venue implementation；
- Risk **不** import Binance connector；
- Product **不**拥有 connector business logic（不 import connectors）；
- ExecutionEngine **不**直接 import Binance implementation；
- MarketFeedProvider 不持有 `PaperBroker`；
- Instrument semantics 不由 venue 名称推导（instrument domain 不 import venue / connectors）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _roots(path: Path) -> set[str]:
    return {module.split(".")[0] for module in _imports(path)}


class ConnectorBoundaryGuardTest(unittest.TestCase):
    def test_strategy_does_not_import_venue_implementations(self) -> None:
        for path in sorted((PROJECT_ROOT / "strategy").rglob("*.py")):
            with self.subTest(module=path.name):
                roots = _roots(path)
                self.assertNotIn("connectors", roots)
                self.assertNotIn("venue", roots)

    def test_risk_does_not_import_venue_implementations(self) -> None:
        for path in sorted((PROJECT_ROOT / "risk").rglob("*.py")):
            with self.subTest(module=path.name):
                roots = _roots(path)
                self.assertNotIn("connectors", roots)
                self.assertNotIn("venue", roots)

    def test_product_does_not_import_connectors(self) -> None:
        for path in sorted((PROJECT_ROOT / "product").rglob("*.py")):
            with self.subTest(module=path.name):
                self.assertNotIn("connectors", _roots(path))

    def test_execution_engine_does_not_import_binance_implementation(self) -> None:
        engine = PROJECT_ROOT / "execution" / "engine.py"

        for module in _imports(engine):
            with self.subTest(module=module):
                self.assertFalse(module.startswith("connectors.binance.execution"),
                                 f"ExecutionEngine must depend on the connector seam, got {module}")
        source = engine.read_text(encoding="utf-8")
        self.assertNotIn('venue == "binance"', source)
        self.assertNotIn("mode == 'paper'", source)

    def test_execution_layer_does_not_import_connectors(self) -> None:
        for path in sorted((PROJECT_ROOT / "execution").rglob("*.py")):
            with self.subTest(module=path.name):
                self.assertNotIn("connectors", _roots(path))

    def test_instrument_domain_has_no_venue_or_connector_dependency(self) -> None:
        """§3：instrument semantics 只描述事实，不得由 venue 名称/实现推导（结构上不依赖它们）。"""
        for path in sorted((PROJECT_ROOT / "domain").rglob("*.py")):
            with self.subTest(module=path.name):
                roots = _roots(path)
                self.assertNotIn("connectors", roots)
                self.assertNotIn("venue", roots)
                self.assertNotIn("execution", roots)

    def test_venue_contracts_do_not_import_venue_implementations(self) -> None:
        for name in ("contracts.py", "health.py", "identity.py", "reference_price.py"):
            path = PROJECT_ROOT / "venue" / name
            with self.subTest(module=name):
                self.assertNotIn("connectors", _roots(path))

    def test_market_feed_provider_does_not_own_execution(self) -> None:
        """§G：feed 不拥有 execution（结构检查：不 import、不持有 broker/manager 字段）。"""
        provider = PROJECT_ROOT / "runtime" / "provider.py"
        roots = _roots(provider)

        self.assertNotIn("execution", roots)
        self.assertNotIn("connectors", roots)
        tree = ast.parse(provider.read_text(encoding="utf-8"))
        assigned = {target.attr for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Attribute)}
        assigned |= {node.attr for node in ast.walk(tree)
                     if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute)}
        for forbidden in ("paper_broker", "paper_manager"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, assigned)

    def test_only_one_paper_broker_construction_site(self) -> None:
        sites = []
        for path in sorted((PROJECT_ROOT / "runtime").rglob("*.py")):
            if "PaperBroker(" in path.read_text(encoding="utf-8"):
                sites.append(path.name)

        self.assertEqual(sites, ["assembly.py"])

    def test_market_data_connector_contract_has_no_execution_surface(self) -> None:
        """§7：market connector 不得拥有 execution 能力。"""
        source = (PROJECT_ROOT / "venue" / "contracts.py").read_text(encoding="utf-8")
        market_block = source.split("class MarketDataConnector(Protocol):")[1].split("class PrivateExecutionConnector")[0]

        for forbidden in ("def submit", "def cancel", "def open_orders", "def recent_fills"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, market_block)

    def test_no_venue_gateway_that_merges_both_responsibilities(self) -> None:
        """人类裁决 4：不得做一个万能 VenueGateway 把职责揉在一起（只在生产代码里检查）。"""
        production = ("domain", "venue", "connectors", "execution", "risk", "strategy", "product",
                      "runtime", "market", "portfolio", "prediction", "storage", "api", "actions",
                      "assistant", "execution_safety", "live", "reports", "cli")
        for root in production:
            for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
                with self.subTest(module=f"{root}/{path.name}"):
                    self.assertNotIn("class VenueGateway", path.read_text(encoding="utf-8"))

    def test_paper_market_connector_has_no_execution_state(self) -> None:
        source = (PROJECT_ROOT / "connectors" / "paper" / "market_connector.py").read_text(encoding="utf-8")

        for forbidden in ("PaperBroker", "submit", "OrderManager"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
