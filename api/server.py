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

from api.routes import REPORT_PATH, ROUTES
from product.serialization import snapshot_to_jsonable
from product.service import ProductService

UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
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
                                                       REPORT_PATH])})
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
