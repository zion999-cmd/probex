"""产品 API server（P0001.10 §2）：stdlib-only 的版本化只读 REST/JSON。

边界（提案 §控制面边界）：

- 第一版**只有读**：`GET /api/v1/...`；
- `POST /api/v1/runtime/stop` 仅**预留**：返回 501（未实现），不提供任何交易/参数/杠杆写入口；
- 任何其他非 GET ⇒ 405；
- 不 import 任何 Binance execution REST client（SC-11）；UI/API 不能绕过 MakerPolicy / RiskGate /
  ReadinessAuthority / ExecutionEngine。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from api.capabilities import build_capabilities_manifest
from api.routes import (
    CAPABILITIES_PATH,
    FACTS_PATH,
    PORTFOLIO_TIMELINE_PATH,
    MARKET_DEPTH_PATH,
    MARKET_HEALTH_PATH,
    MARKET_OVERLAYS_PATH,
    MARKET_TIMELINE_PATH,
    MARKET_TRADES_PATH,
    REPLAY_PATH,
    METRICS_PATH,
    REPORT_PATH,
    ROUTES,
    RUNS_COMPARE_PATH,
    RUNS_PATH,
)
from product.serialization import snapshot_to_jsonable
from product.service import ProductService
from product.types import SCHEMA_VERSION

UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
SCHEMA_VERSION_VALUE = SCHEMA_VERSION
UI_PATH = UI_ROOT / "app" / "index.html"
REPORT_MARKDOWN = "markdown"
REPORT_JSON = "json"

SCHEMA_VERSION_PATH = "/api/v1/schema"
SNAPSHOT_PATH = "/api/v1/snapshot"
STOP_PATH = "/api/v1/runtime/stop"
NOT_IMPLEMENTED = 501
METHOD_NOT_ALLOWED = 405
NOT_FOUND = 404


class ProductApiHandler(BaseHTTPRequestHandler):
    """只读 JSON handler。`service` 由 `create_handler(service)` 注入。"""

    service: ProductService
    server_version = "probex-product-api/1"
    sys_version = ""

    #: 不向 stderr 打裸日志（§20）；访问信息由调用方按需自行采集
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 覆盖父类签名
        return

    # ------------------------------------------------------------------ helpers

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, *, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot_dict(self) -> dict:
        return snapshot_to_jsonable(self.service.snapshot())

    def _error(self, status: int, code: str, detail: str) -> None:
        self._send_json(status, {"error": code, "detail": detail, "status": status})

    # ------------------------------------------------------------------ GET

    def _serve_ui(self, path: str) -> bool:
        """服务静态工程控制台（只读、无 build step）。返回是否已处理。"""
        if path in ("/", "/index.html"):
            target = UI_PATH
        elif path.startswith("/ui/"):
            relative = path[len("/ui/"):].lstrip("/")
            if not relative or ".." in Path(relative).parts:
                self._error(NOT_FOUND, "invalid_ui_path", path)
                return True
            target = UI_ROOT / relative
        else:
            return False
        if not target.exists() or not target.is_file():
            self._error(NOT_FOUND, "ui_not_found", str(target.name))
            return True
        suffix = target.suffix.lower()
        content_type = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8"}.get(
            suffix, "text/plain; charset=utf-8")
        self._send_text(200, target.read_text(encoding="utf-8"), content_type=content_type)
        return True

    def _serve_capabilities(self, query: str) -> None:
        """能力清单：从路由表 + CLI 注册表生成（避免手写漂移）。"""
        from cli.main import COMMAND_SPEC, EXIT_CODES

        manifest = build_capabilities_manifest(
            schema_version=SCHEMA_VERSION_VALUE,
            api_read=tuple(sorted([*ROUTES, SNAPSHOT_PATH, REPORT_PATH, METRICS_PATH, RUNS_PATH,
                                   RUNS_COMPARE_PATH, FACTS_PATH, PORTFOLIO_TIMELINE_PATH,
                                   MARKET_TIMELINE_PATH, MARKET_DEPTH_PATH, MARKET_TRADES_PATH,
                                   MARKET_HEALTH_PATH, MARKET_OVERLAYS_PATH])),
            cli_commands=COMMAND_SPEC,
            exit_codes=EXIT_CODES,
        )
        self._send_json(200, manifest)

    def _serve_metrics(self) -> None:
        """Metric Contract 定义（UI 只读定义与报告值，不得自行计算）。"""
        from reports.metrics import definitions_payload

        self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE,
                              "definitions": list(definitions_payload())})

    def _registry(self):
        registry = self.service.run_registry_view()
        if registry is None:
            self._error(503, "run_registry_unavailable",
                        "no run registry is wired (runs are UNKNOWN, not empty)")
            return None
        return registry

    def _serve_runs(self, path: str, query: str) -> None:
        """Run Registry 只读端点：列表 / 单个 / 对比。"""
        registry = self._registry()
        if registry is None:
            return
        try:
            if path == RUNS_PATH:
                records = registry.list()
                self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE,
                                      "runs": [registry._record_payload(record) for record in records]})
                return
            if path == RUNS_COMPARE_PATH:
                params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
                left, right = params.get("left", ""), params.get("right", "")
                if not left or not right:
                    self._error(400, "missing_run_ids", "left and right run ids are required")
                    return
                comparison = registry.compare(left, right)
                from product.serialization import to_jsonable

                self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE,
                                      "comparison": to_jsonable(comparison)})
                return
            run_id = path[len(RUNS_PATH) + 1:]
            record = registry.load(run_id)
            if record is None:
                self._error(NOT_FOUND, "unknown_run", run_id)
                return
            self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE,
                                  "run": registry._record_payload(record)})
        except Exception as exc:  # noqa: BLE001 - 注册表错误必须显式，不返回空壳
            self._error(503, "run_registry_error", type(exc).__name__)

    # ------------------------------------------------------------------ Market Visual Workbench

    def _workbench(self):
        """取有界展示缓冲 + 显示参数；未接线/未配置 ⇒ None（503，绝不无限加载）。"""
        history = self.service.market_history_view()
        config = self.service.projection_config_view()
        if history is None:
            self._error(503, "market_history_unavailable",
                        "no bounded market history is wired (market workbench is UNKNOWN, not empty)")
            return None
        if config is None:
            self._error(503, "projection_config_required",
                        "workbench display bounds (window/bucket/max_points/price_levels) are not configured")
            return None
        return history, config

    def _serve_market_workbench(self, path: str, query: str) -> None:
        """timeline / depth / trades / health / overlays：全部来自同一个只读缓冲投影。"""
        resolved = self._workbench()
        if resolved is None:
            return
        history, config = resolved
        from product.market_projection import project_depth, project_health, project_overlays, project_trades
        from product.market_timeline import project_timeline
        from product.serialization import to_jsonable

        if path == MARKET_TIMELINE_PATH:
            timeline = project_timeline(history.states(), bucket_ms=config.bucket_ms,
                                        max_points=config.max_points, window_ms=config.window_ms)
            payload = {"timeline": to_jsonable(timeline), "counts": history.counts,
                       "bounds": self._bounds(config)}
        elif path == MARKET_DEPTH_PATH:
            heatmap = project_depth(history.snapshots(), config=config, max_snapshots=config.max_points)
            payload = {"depth": to_jsonable(heatmap), "counts": history.counts,
                       "bounds": self._bounds(config)}
        elif path == MARKET_TRADES_PATH:
            trades = project_trades(history.trades(), max_points=config.max_points)
            payload = {"trades": to_jsonable(trades), "counts": history.counts,
                       "bounds": self._bounds(config)}
        elif path == MARKET_HEALTH_PATH:
            health = project_health(history.health(), max_segments=config.max_points)
            payload = {"health": to_jsonable(health), "counts": history.counts,
                       "bounds": self._bounds(config)}
        else:
            overlays = project_overlays(history.decisions(), history.executions(),
                                        max_points=config.max_points)
            payload = {"overlays": to_jsonable(overlays), "counts": history.counts,
                       "bounds": self._bounds(config)}
        payload["run_id"] = history.run_id
        self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE, **payload})

    @staticmethod
    def _bounds(config: object) -> dict[str, int]:
        return {"window_ms": int(config.window_ms), "bucket_ms": int(config.bucket_ms),
                "max_points": int(config.max_points), "price_levels": int(config.price_levels)}

    def _serve_run_market(self, path: str) -> None:
        """`GET /api/v1/runs/<run_id>/market`：RUN REVIEW 视图（必须有该 run 的有界历史）。"""
        run_id = path[len(f"{RUNS_PATH}/"):-len("/market")]
        resolved = self._workbench()
        if resolved is None:
            return
        history, _config = resolved
        if not run_id or history.run_id != run_id:
            self._error(NOT_FOUND, "no_recorded_market_for_run",
                        "the wired bounded history belongs to a different run (or none)")
            return
        from product.serialization import to_jsonable
        from product.market_timeline import project_timeline

        config = self.service.projection_config_view()
        timeline = project_timeline(history.states(), bucket_ms=config.bucket_ms,
                                    max_points=config.max_points, window_ms=config.window_ms)
        self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE, "run_id": run_id,
                              "timeline": to_jsonable(timeline), "counts": history.counts,
                              "bounds": self._bounds(config)})

    def _serve_replay_control(self, verb: str, body: dict) -> None:
        """local replay control：只转发意图给 REPLAY owner（绝不作用于 LIVE/TESTNET execution）。"""
        control = self.service.replay_control_view()
        if control is None:
            self._error(503, "replay_control_unavailable",
                        "no replay control is wired (only REPLAY runtimes expose one)")
            return
        try:
            if verb == "play":
                command = control.play()
            elif verb == "pause":
                command = control.pause()
            elif verb == "step":
                command = control.step(count=int(body.get("count", 1) or 1))
            elif verb == "speed":
                command = control.set_speed(float(body.get("speed")))
            elif verb == "seek":
                ordinal = body.get("ordinal")
                ts = body.get("ts")
                command = control.seek(ordinal=None if ordinal is None else int(ordinal),
                                       ts=None if ts is None else int(ts))
            else:
                self._error(NOT_FOUND, "unknown_replay_verb", verb)
                return
        except Exception as exc:  # noqa: BLE001 - 控制契约错误必须显式返回（含"非 REPLAY"拒绝）
            self._send_json(409, {"error": "replay_control_refused", "detail": str(exc),
                                  "status": 409})
            return
        from product.serialization import to_jsonable

        self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE, "command": to_jsonable(command),
                              "control": {"paused": control.paused, "speed": control.speed,
                                          "position": control.position}})

    def _serve_facts(self, path: str) -> None:
        """G2：Raw Facts drill-down（只读、有界；缺失 ⇒ 404，非法 kind ⇒ 400）。"""
        remainder = path[len(FACTS_PATH) + 1:]
        parts = remainder.split("/")
        if len(parts) != 2 or not all(parts):
            self._error(400, "invalid_fact_reference", "expected /api/v1/facts/<kind>/<identity>")
            return
        kind, identity = parts
        try:
            view = self.service.raw_facts_view(kind, identity)
        except ValueError as exc:
            self._error(400, "unsupported_fact_kind", str(exc))
            return
        if not view.available:
            self._error(NOT_FOUND, "raw_fact_unavailable", f"{kind}:{identity} is not in the current runtime")
            return
        from product.serialization import to_jsonable

        self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE, "fact": to_jsonable(view)})

    def _serve_account_timeline(self) -> None:
        """G3：equity / exposure 时间线（未接线 ⇒ 503；有界由 projection config 决定）。"""
        buffer = self.service.account_timeline_view()
        config = self.service.projection_config_view()
        if buffer is None:
            self._error(503, "account_timeline_unavailable",
                        "no bounded account timeline is wired (UNKNOWN, not empty)")
            return
        if config is None:
            self._error(503, "projection_config_required",
                        "display bounds are required for the account timeline")
            return
        from product.account_timeline import project_account_timeline
        from product.serialization import to_jsonable

        timeline = project_account_timeline(buffer.samples(), config=config)
        self._send_json(200, {"schema_version": SCHEMA_VERSION_VALUE,
                              "timeline": to_jsonable(timeline), "counts": buffer.counts,
                              "bounds": self._bounds(config), "run_id": buffer.run_id})

    def _serve_report(self, query: str) -> None:
        """只读 Run Summary（json 默认 / markdown）。"""
        try:
            summary = self.service.run_summary_view()
        except Exception as exc:  # noqa: BLE001
            self._error(503, "report_unavailable", type(exc).__name__)
            return
        if summary is None:
            self._error(503, "report_unavailable",
                        "no run summary provider is wired (report is UNKNOWN, not empty)")
            return
        from reports.json import summary_to_json, summary_to_jsonable
        from reports.markdown import summary_to_markdown

        fmt = ""
        if query:
            for part in query.split("&"):
                if part.startswith("format="):
                    fmt = part.split("=", 1)[1].lower()
        if fmt == REPORT_MARKDOWN:
            self._send_text(200, summary_to_markdown(summary), content_type="text/markdown; charset=utf-8")
            return
        self._send_json(200, {"schema_version": summary.schema_version,
                              "run_summary": summary_to_jsonable(summary),
                              "markdown_hint": f"{REPORT_PATH}?format=markdown",
                              "json": json.loads(summary_to_json(summary))})

    def do_GET(self) -> None:  # noqa: N802 - stdlib 接口
        raw_path = self.path
        path, _, query = raw_path.partition("?")
        if self._serve_ui(path):
            return
        if path == REPORT_PATH:
            self._serve_report(query)
            return
        if path in (MARKET_TIMELINE_PATH, MARKET_DEPTH_PATH, MARKET_TRADES_PATH, MARKET_HEALTH_PATH,
                    MARKET_OVERLAYS_PATH):
            self._serve_market_workbench(path, query)
            return
        if path.startswith(f"{RUNS_PATH}/") and path.endswith("/market"):
            self._serve_run_market(path)
            return
        if path == PORTFOLIO_TIMELINE_PATH:
            self._serve_account_timeline()
            return
        if path.startswith(f"{FACTS_PATH}/"):
            self._serve_facts(path)
            return
        if path == CAPABILITIES_PATH:
            self._serve_capabilities(query)
            return
        if path == METRICS_PATH:
            self._serve_metrics()
            return
        if path == RUNS_PATH or path == RUNS_COMPARE_PATH or path.startswith(f"{RUNS_PATH}/"):
            self._serve_runs(path, query)
            return
        if path in ("/", "/index.html"):
            if not UI_PATH.exists():
                self._error(NOT_FOUND, "ui_not_found", str(UI_PATH))
                return
            self._send_text(200, UI_PATH.read_text(encoding="utf-8"), content_type="text/html; charset=utf-8")
            return
        try:
            snapshot = self._snapshot_dict()
        except Exception as exc:  # noqa: BLE001 - 读不到事实时给出明确错误，绝不返回空壳
            self._error(503, "snapshot_unavailable", type(exc).__name__)
            return
        if path == SNAPSHOT_PATH:
            self._send_json(200, snapshot)
            return
        if path == SCHEMA_VERSION_PATH:
            self._send_json(200, {"schema_version": snapshot["schema_version"],
                                  "endpoints": sorted([*ROUTES, SNAPSHOT_PATH, SCHEMA_VERSION_PATH,
                                                       REPORT_PATH, CAPABILITIES_PATH, METRICS_PATH,
                                                       RUNS_PATH, RUNS_COMPARE_PATH, MARKET_TIMELINE_PATH,
                                                       MARKET_DEPTH_PATH, MARKET_TRADES_PATH,
                                                       MARKET_HEALTH_PATH, MARKET_OVERLAYS_PATH,
                                                       FACTS_PATH, PORTFOLIO_TIMELINE_PATH])})
            return
        module = ROUTES.get(path)
        if module is None:
            self._error(NOT_FOUND, "unknown_endpoint", path)
            return
        try:
            self._send_json(200, module.payload(snapshot))
        except KeyError as exc:
            self._error(503, "snapshot_section_missing", str(exc))

    # ------------------------------------------------------------------ 非读（全部拒绝）

    def do_POST(self) -> None:  # noqa: N802 - stdlib 接口
        path = self.path.split("?", 1)[0]
        if path.startswith(f"{REPLAY_PATH}/"):
            verb = path[len(REPLAY_PATH) + 1:]
            body: dict = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    parsed = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                    body = parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    self._error(400, "invalid_json_body", "request body must be JSON")
                    return
            self._serve_replay_control(verb, body)
            return
        if path == STOP_PATH:
            # 预留：本 Proposal **不**实现控制面动作（即使 stop 也必须走既有安全语义，尚未接线）
            self._send_json(NOT_IMPLEMENTED, {
                "error": "not_implemented",
                "detail": "runtime stop is reserved and not wired in P0001.10",
                "status": NOT_IMPLEMENTED,
            })
            return
        self._error(METHOD_NOT_ALLOWED, "read_only_api",
                    "P0001.10 exposes read-only endpoints; trading actions are not available")

    do_PUT = do_DELETE = do_PATCH = do_POST


def create_handler(service: ProductService):
    """把 service 绑定进 handler 类（每个 server 一个独立子类，避免全局状态）。"""
    if not isinstance(service, ProductService):
        raise TypeError("create_handler requires a ProductService")
    return type("BoundProductApiHandler", (ProductApiHandler,), {"service": service})


def create_server(service: ProductService, *, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """创建（未启动的）HTTP server；`port=0` 时由系统分配（便于测试）。"""
    return ThreadingHTTPServer((host, port), create_handler(service))


def serve(service: ProductService, *, host: str = "127.0.0.1", port: int = 0) -> None:
    """阻塞式启动（供 CLI / 本地 console 使用）。"""
    server = create_server(service, host=host, port=port)
    server.serve_forever()
