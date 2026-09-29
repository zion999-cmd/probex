"""`probex` 产品 CLI（P0001.10.3）：**机器优先**的只读接口。

契约：

- 只读（status / inspect / query / report / explain）；**没有** buy / sell / order / set-risk / set-leverage；
- `--json` 时 stdout 只输出稳定 JSON（Agent 不需要解析终端文本）；
- stdout = 数据，stderr = diagnostics；
- 稳定退出码：0 ok / 2 参数错误 / 10 不可用 / 11 证据不足（UNKNOWN）/ 20 被 policy·readiness 阻塞 / 30 内部错误；
- 不实现任何业务逻辑：本模块只做 HTTP 读取与格式化，绝不直接读领域内部对象、绝不调用 Binance。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

from cli.render import fact_rows, fact_text, list_text, rows

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 10
EXIT_UNKNOWN = 11
EXIT_BLOCKED = 20
EXIT_INTERNAL = 30

DEFAULT_API_URL = "http://127.0.0.1:8787"

#: command -> (endpoint, snapshot section)。section 为 None 表示整份 snapshot。
SLICE_COMMANDS: dict[str, tuple[str, str | None]] = {
    "status": ("/api/v1/status", "health"),
    "market": ("/api/v1/market", "market"),
    "prediction": ("/api/v1/prediction", "prediction"),
    "strategy": ("/api/v1/strategy", "strategy"),
    "risk": ("/api/v1/risk", "risk"),
    "orders": ("/api/v1/orders", "execution"),
    "portfolio": ("/api/v1/portfolio", "portfolio"),
    "readiness": ("/api/v1/readiness", "readiness"),
    "evidence": ("/api/v1/evidence", "evidence"),
    "snapshot": ("/api/v1/snapshot", None),
    "inspect": ("/api/v1/snapshot", None),
}
REPORT_PATH = "/api/v1/reports/run-summary"
#: P0001.11 端点（与 api.routes 的常量必须一致；由 tests/unit/test_cli.py 固定，防手写漂移）
CAPABILITIES_PATH = "/api/v1/capabilities"
METRICS_PATH = "/api/v1/metrics"
RUNS_PATH = "/api/v1/runs"
RUNS_COMPARE_PATH = "/api/v1/runs/compare"
BLOCKERS_PATH = "/api/v1/blockers"
ACTIONS_PATH = "/api/v1/actions"
ACTIONS_AUDIT_PATH = "/api/v1/actions/audit"
ASSISTANT_CONTEXT_PATH = "/api/v1/assistant/context"
REASONS_PATH = "/api/v1/reasons"
#: F-12/F-15：operational posture（只读；单一端点 + 客户端选择子话题）
OPS_PATH = "/api/v1/ops"
OPS_TOPICS = ("status", "retention", "logging", "network")
EXECUTION_SUBCOMMANDS = {
    "health": "/api/v1/execution/health",
    "limits": "/api/v1/execution/limits",
    "rate-limits": "/api/v1/execution/rate-limits",
    "latency": "/api/v1/execution/latency",
    "anomalies": "/api/v1/execution/anomalies",
    "reconciliation": "/api/v1/execution/reconciliation",
}

#: 机器可读命令表（供 `GET /api/v1/capabilities` 生成；**单一事实来源**）
COMMAND_SPEC: dict[str, str] = {}

#: 稳定退出码（人类裁决：不改动 0/2/10/11/20/30）
EXIT_CODES: dict[str, str] = {
    "0": "success",
    "2": "invalid arguments",
    "10": "product API unavailable",
    "11": "unknown / insufficient evidence",
    "20": "blocked by policy or readiness",
    "30": "internal failure",
}


class CliUnavailable(Exception):
    """API 不可用（连接失败 / 5xx / 非预期响应）。"""


def _opener(api_url: str):
    """loopback 绕过代理（本机 API 不应经过 HTTP 代理），其余走默认行为。"""
    host = urllib.parse.urlsplit(api_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def fetch(api_url: str, path: str, *, opener=None) -> dict:
    opener = opener or _opener(api_url)
    url = f"{api_url.rstrip('/')}{path}"
    try:
        with opener.open(url, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise CliUnavailable(f"{path} -> HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise CliUnavailable(f"{path} -> {type(error).__name__}") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CliUnavailable(f"{path} -> invalid JSON") from error
    if not isinstance(payload, dict):
        raise CliUnavailable(f"{path} -> unexpected payload type")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="probex", description="Probex product CLI (read-only)")
    parser.add_argument("--api-url", default=os.environ.get("PROBEX_API_URL", DEFAULT_API_URL),
                        help="Product API base URL (env PROBEX_API_URL)")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    for name in SLICE_COMMANDS:
        sub = subparsers.add_parser(name, help=f"read {name}")
        sub.add_argument("--json", action="store_true", help="emit stable JSON on stdout")
        if name == "orders":
            sub.add_argument("--active", action="store_true", help="only active orders")
        if name == "readiness":
            sub.add_argument("--explain", action="store_true", help="include evidence trace")
            sub.add_argument("--strict", action="store_true",
                             help="exit 20 when readiness is blocked (policy/readiness gate)")
        if name == "evidence":
            sub.add_argument("--decision-id", default=None, help="filter trace by decision identity")

    for name, help_text in (("blockers", "unified blockers (why nothing is happening)"),
                            ("metrics", "metric definitions (formulas and UNKNOWN conditions)"),
                            ("capabilities", "machine-readable capability manifest"),
                            ("reasons", "reason-code human explanation catalog (F-09)"),
                            ("runs", "list recorded runs")):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true")
        if name == "blockers":
            sub.add_argument("--strict", action="store_true",
                             help="exit 20 when any BLOCKING blocker is present")
        if name == "runs":
            sub.add_argument("--limit", type=int, default=None)
            sub.add_argument("--offset", type=int, default=0)
        if name == "reasons":
            sub.add_argument("code", nargs="?", default=None,
                             help="optional reason code (omit for the full catalog)")

    execution = subparsers.add_parser("execution", help="execution safety facts (read-only)")
    execution.add_argument("topic", choices=sorted(EXECUTION_SUBCOMMANDS))
    execution.add_argument("--json", action="store_true")

    ops = subparsers.add_parser("ops", help="operational posture (network/auth/logging/retention/liveness)")
    ops.add_argument("topic", choices=list(OPS_TOPICS))
    ops.add_argument("--json", action="store_true")

    actions = subparsers.add_parser("actions", help="list the action manifest (what an agent may do)")
    actions.add_argument("--json", action="store_true")

    action = subparsers.add_parser("action", help="describe or invoke a controlled action")
    action_sub = action.add_subparsers(dest="action_mode", metavar="mode")
    describe = action_sub.add_parser("describe", help="describe one action")
    describe.add_argument("action_id")
    describe.add_argument("--json", action="store_true")
    invoke = action_sub.add_parser("invoke", help="invoke an action through the Action Gateway")
    invoke.add_argument("action_id")
    invoke.add_argument("--param", action="append", default=[], help="parameter as key=value")
    invoke.add_argument("--confirm", default=None, help="confirmation id returned by a previous call")
    invoke.add_argument("--surface", default="monitor")
    invoke.add_argument("--json", action="store_true")

    run = subparsers.add_parser("run", help="inspect one run / compare two runs")
    run_sub = run.add_subparsers(dest="run_mode", metavar="mode")
    show = run_sub.add_parser("show", help="show one run")
    show.add_argument("run_id")
    show.add_argument("--json", action="store_true")
    compare = run_sub.add_parser("compare", help="compare two runs by metric name")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--json", action="store_true")

    explain = subparsers.add_parser("explain", help="explain a decision / order")
    explain.add_argument("target", choices=["decision", "order"])
    explain.add_argument("identifier")
    explain.add_argument("--json", action="store_true")

    report = subparsers.add_parser("report", help="read run reports")
    report.add_argument("target", choices=["run"])
    report.add_argument("identifier")
    report.add_argument("--format", choices=["json", "markdown"], default="json")
    report.add_argument("--json", action="store_true")
    return parser


def _emit(payload: dict, *, as_json: bool, stdout, stderr) -> None:
    if as_json:
        json.dump(payload, stdout, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        stdout.write("\n")
        return
    for key, value in payload.items():
        if isinstance(value, dict) and "known" in value:
            stdout.write(f"{key}: {fact_text(value)}\n")
        elif isinstance(value, dict):
            stdout.write(f"[{key}]\n{fact_rows(value)}\n")
        else:
            stdout.write(f"{key}: {value}\n")


def _facts_unknown(*facts: dict) -> bool:
    return all(isinstance(item, dict) and item.get("known") is False for item in facts)


def main(argv: Sequence[str] | None = None, *, stdout=None, stderr=None,
         opener=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_request:  # argparse 的 --help / 参数错误
        code = int(exit_request.code or 0)
        return EXIT_OK if code == 0 else EXIT_USAGE
    if not args.command:
        parser.print_help(file=stderr)
        return EXIT_USAGE

    api_url = args.api_url
    command = args.command
    try:
        if command == "report":
            path = f"{REPORT_PATH}?format={args.format}"
            if args.format == "markdown":
                payload = fetch(api_url, path, opener=opener)  # JSON 端点同时给出 markdown 提示
            else:
                payload = fetch(api_url, path, opener=opener)
            summary = payload.get("run_summary") or {}
            run = summary.get("run") or {}
            if args.identifier not in (run.get("run_id"), run.get("runtime", {}).get("runtime_id")):
                stderr.write(f"error: run {args.identifier!r} not found in the current run summary\n")
                return EXIT_UNKNOWN
            _emit(payload if args.json else {"run_id": run.get("run_id"), "run": run,
                                             "decision_counts": summary.get("decision_counts"),
                                             "order_counts": summary.get("order_counts")},
                  as_json=args.json, stdout=stdout, stderr=stderr)
            if not args.json:
                stdout.write(f"markdown_hint: {api_url}{REPORT_PATH}?format=markdown\n")
            return EXIT_OK

        if command in ("blockers", "metrics", "capabilities", "reasons", "runs"):
            path = {"blockers": BLOCKERS_PATH, "metrics": METRICS_PATH,
                    "capabilities": CAPABILITIES_PATH, "reasons": REASONS_PATH,
                    "runs": RUNS_PATH}[command]
            if command == "reasons" and getattr(args, "code", None):
                path = f"{REASONS_PATH}/{args.code}"
            if command == "runs":
                # F-19：列表有界；limit / offset 均交给服务端校验（安全上限在 registry）
                query = []
                if getattr(args, "limit", None):
                    query.append(f"limit={int(args.limit)}")
                if getattr(args, "offset", 0):
                    query.append(f"offset={int(args.offset)}")
                if query:
                    path = f"{path}?{'&'.join(query)}"
            payload = fetch(api_url, path, opener=opener)
            if command == "runs":
                runs = payload.get("runs") or []
                pagination = payload.get("pagination") or {}
                _emit({"count": len(runs), "pagination": pagination,
                       "runs": [{"run_id": run.get("run_id"), "status": run.get("status"),
                                 "started_at": run.get("started_at"),
                                 "config_fingerprint": (run.get("config_fingerprint") or {}).get("value")}
                                for run in runs]} if args.json else
                      {"count": str(len(runs)), "has_more": str(bool(pagination.get("has_more")))},
                      as_json=args.json, stdout=stdout, stderr=stderr)
                if not args.json:
                    for run in runs:
                        stdout.write(f"- {run.get('run_id')} {run.get('status')} "
                                     f"started_at={run.get('started_at')}\n")
                    if pagination.get("has_more"):
                        stdout.write(f"more: next_offset={pagination.get('next_offset')} "
                                     f"total={pagination.get('total')}\n")
                return EXIT_OK
            if command == "blockers":
                blockers = payload.get("blockers") or []
                _emit({"count": len(blockers), "blockers": blockers} if args.json
                      else {"count": str(len(blockers))}, as_json=args.json, stdout=stdout, stderr=stderr)
                if not args.json:
                    for blocker in blockers:
                        explanation = blocker.get("explanation") or {}
                        stdout.write(f"- [{blocker.get('severity')}] {blocker.get('owner')} "
                                     f"{blocker.get('reason_code')} ({blocker.get('source_ref')}): "
                                     f"{blocker.get('message')}\n")
                        # F-09：原始 code 保留，另附人类解释（同一 catalog）
                        stdout.write(f"    {explanation.get('title', '')} — "
                                     f"{explanation.get('explanation', '')}\n")
                        stdout.write(f"    next: {explanation.get('suggested_next_step', '')}\n")
                blocking = [b for b in blockers
                            if isinstance(b, dict) and b.get("severity") == "BLOCKING"]
                if args.strict and blocking:
                    stderr.write(f"error: {len(blocking)} BLOCKING blocker(s) present\n")
                    return EXIT_BLOCKED
                return EXIT_OK
            _emit(payload, as_json=args.json, stdout=stdout, stderr=stderr)
            return EXIT_OK

        if command == "execution":
            payload = fetch(api_url, EXECUTION_SUBCOMMANDS[args.topic], opener=opener)
            _emit(payload, as_json=args.json, stdout=stdout, stderr=stderr)
            return EXIT_OK

        if command == "ops":
            payload = fetch(api_url, OPS_PATH, opener=opener)
            ops_payload = payload.get("ops") or {}
            topic = args.topic
            if topic == "status":
                selected = {key: ops_payload.get(key) for key in
                            ("process_live", "runtime_state", "runtime_detail", "trade_readiness",
                             "trade_readiness_reasons", "execution_health", "operational_warning",
                             "ts")}
            else:
                selected = ops_payload.get(topic) or {}
            _emit({topic: selected} if args.json else {
                key: (str(value) if not isinstance(value, (dict, list)) else "...") for key, value in selected.items()},
                as_json=args.json, stdout=stdout, stderr=stderr)
            if not args.json:
                for key, value in selected.items():
                    stdout.write(f"- {key}: {value}\n")
            return EXIT_OK

        if command == "actions":
            payload = fetch(api_url, ACTIONS_PATH, opener=opener)
            entries = payload.get("actions") or []
            _emit({"count": len(entries), "actions": entries} if args.json else {"count": str(len(entries))},
                  as_json=args.json, stdout=stdout, stderr=stderr)
            if not args.json:
                for entry in entries:
                    state = "available" if entry.get("available") else (entry.get("availability") or "unavailable")
                    stdout.write(f"- {entry.get('action_id')} [{entry.get('level')}] {state}\n")
            return EXIT_OK

        if command == "action":
            mode = getattr(args, "action_mode", None)
            if mode == "describe":
                payload = fetch(api_url, ACTIONS_PATH, opener=opener)
                entry = next((item for item in payload.get("actions") or []
                              if item.get("action_id") == args.action_id), None)
                if entry is None:
                    stderr.write(f"error: unknown action {args.action_id!r}\n")
                    return EXIT_USAGE
                _emit(entry, as_json=args.json, stdout=stdout, stderr=stderr)
                return EXIT_OK
            if mode == "invoke":
                parameters: dict[str, object] = {}
                for item in args.param:
                    key, _, value = str(item).partition("=")
                    if not key:
                        stderr.write("error: --param requires key=value\n")
                        return EXIT_USAGE
                    parameters[key] = value
                body = {"parameters": parameters, "requested_by": "cli", "surface": args.surface}
                if args.confirm:
                    body["confirmation"] = args.confirm
                request = urllib.request.Request(
                    f"{api_url.rstrip('/')}{ACTIONS_PATH}/{args.action_id}", method="POST",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                try:
                    with (opener or _opener(api_url)).open(request, timeout=10) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as error:      # 409/502 也带结构化 body
                    try:
                        payload = json.loads(error.read().decode("utf-8"))
                    except Exception:  # noqa: BLE001
                        stderr.write(f"error: action failed with HTTP {error.code}\n")
                        return EXIT_INTERNAL
                except (urllib.error.URLError, OSError) as error:
                    stderr.write(f"error: product API unavailable: {type(error).__name__}\n")
                    return EXIT_UNAVAILABLE
                action_result = payload.get("action") or {}
                _emit(action_result, as_json=args.json, stdout=stdout, stderr=stderr)
                status = action_result.get("status")
                if status == "SUCCEEDED":
                    return EXIT_OK
                if status == "CONFIRMATION_REQUIRED":
                    stderr.write("error: confirmation required: "
                                 f"{action_result.get('confirmation_id')}\n")
                    return EXIT_BLOCKED
                if status == "REFUSED":
                    stderr.write(f"error: refused ({action_result.get('reason_code')})\n")
                    return EXIT_USAGE if action_result.get("reason_code") == "UNKNOWN_ACTION" else EXIT_BLOCKED
                if status == "UNKNOWN":
                    stderr.write("error: action outcome is UNKNOWN (not failed, not succeeded)\n")
                    return EXIT_UNKNOWN
                return EXIT_INTERNAL
            stderr.write("error: action requires a mode (describe | invoke)\n")
            return EXIT_USAGE

        if command == "run":
            mode = getattr(args, "run_mode", None)
            if mode == "show":
                try:
                    payload = fetch(api_url, f"{RUNS_PATH}/{args.run_id}", opener=opener)
                except CliUnavailable as error:
                    if "HTTP 404" in str(error):
                        stderr.write(f"error: unknown run {args.run_id!r}\n")
                        return EXIT_UNKNOWN
                    raise
                run = payload.get("run") or {}
                _emit(run if args.json else {"run_id": run.get("run_id"), "status": run.get("status"),
                                             "started_at": str(run.get("started_at"))},
                      as_json=args.json, stdout=stdout, stderr=stderr)
                return EXIT_OK
            if mode == "compare":
                try:
                    payload = fetch(api_url, f"{RUNS_COMPARE_PATH}?left={args.left}&right={args.right}",
                                    opener=opener)
                except CliUnavailable as error:
                    if "HTTP 404" in str(error):
                        stderr.write(f"error: unknown run in comparison {args.left!r} / {args.right!r}\n")
                        return EXIT_UNKNOWN
                    raise
                comparison = payload.get("comparison") or {}
                _emit(comparison if args.json else
                      {"left": comparison.get("left_run_id"), "right": comparison.get("right_run_id"),
                       "metrics": str(len(comparison.get("metrics") or []))},
                      as_json=args.json, stdout=stdout, stderr=stderr)
                if not args.json:
                    for metric in comparison.get("metrics") or []:
                        delta = metric.get("delta") or {}
                        stdout.write(f"- {metric.get('name')}: "
                                     f"{'UNKNOWN' if not delta.get('known') else delta.get('value')}\n")
                return EXIT_OK
            stderr.write("error: run requires a mode (show | compare)\n")
            return EXIT_USAGE

        if command == "explain":
            evidence = fetch(api_url, "/api/v1/evidence", opener=opener).get("evidence", {})
            trace = [entry for entry in evidence.get("trace", [])
                     if args.identifier in json.dumps(entry, ensure_ascii=False)]
            if not trace:
                stderr.write(f"error: no evidence entry matches {args.target} {args.identifier!r}\n")
                return EXIT_UNKNOWN
            # F-09：同一 catalog 给 trace 的 reason_code 附人类解释（原始 code 保留）
            reasons = {item.get("reason_code"): item
                       for item in (fetch(api_url, REASONS_PATH, opener=opener).get("catalog") or [])}
            explanations = {code: reasons.get(code) for code in
                            sorted({e.get("reason_code") for e in trace
                                    if isinstance(e.get("reason_code"), str)})}
            _emit({"target": args.target, "identifier": args.identifier, "trace": trace,
                   "reason_explanations": explanations}
                  if args.json else {"matches": str(len(trace))},
                  as_json=args.json, stdout=stdout, stderr=stderr)
            if not args.json:
                for entry in trace:
                    stdout.write(f"- {entry.get('stage')}: {entry.get('outcome')}\n")
                    code = entry.get("reason_code")
                    if code and explanations.get(code):
                        stdout.write(f"    {code}: {explanations[code].get('title', '')} — "
                                     f"{explanations[code].get('explanation', '')}\n")
            return EXIT_OK

        entry = SLICE_COMMANDS.get(command)
        if entry is None:
            stderr.write(f"error: unknown command {command!r}\n")
            return EXIT_USAGE
        path, section_name = entry
        payload = fetch(api_url, path, opener=opener)
        if section_name is None:
            _emit(payload, as_json=args.json, stdout=stdout, stderr=stderr)
        else:
            section = payload.get(section_name)
            if section is None:
                stderr.write(f"error: payload has no section {section_name!r}\n")
                return EXIT_INTERNAL
            if command == "orders" and args.active:
                orders = (section.get("active_orders") or [])
                _emit({"active_orders": orders} if args.json else {"active_orders": str(len(orders))},
                      as_json=args.json, stdout=stdout, stderr=stderr)
                if not args.json:
                    for order in orders:
                        stdout.write(f"- {order.get('client_order_id')} {order.get('side')} "
                                     f"{order.get('status')} decision={fact_text_json(order.get('decision_id'))}\n")
                return EXIT_OK
            if command == "readiness":
                blockers = section.get("reasons") or []
                status = section.get("status")
                if args.explain:
                    evidence = fetch(api_url, "/api/v1/evidence", opener=opener).get("evidence", {})
                    section = {**section, "trace": evidence.get("trace", [])}
                _emit(section if args.json else section, as_json=args.json, stdout=stdout, stderr=stderr)
                if not args.json and blockers:
                    stdout.write(f"blockers:\n{list_text(blockers)}\n")
                if isinstance(status, dict) and status.get("known") is False:
                    stderr.write("error: readiness status is UNKNOWN (insufficient evidence)\n")
                    return EXIT_UNKNOWN
                if args.strict and (blockers or (isinstance(status, dict) and status.get("value") != "live_ready")):
                    stderr.write("error: readiness is blocked (policy/readiness gate)\n")
                    return EXIT_BLOCKED
                return EXIT_OK
            if command == "prediction" and _facts_unknown(section.get("request_id", {})):
                _emit(section, as_json=args.json, stdout=stdout, stderr=stderr)
                stderr.write("error: insufficient evidence: no prediction record yet (UNKNOWN)\n")
                return EXIT_UNKNOWN
            _emit(section, as_json=args.json, stdout=stdout, stderr=stderr)
        return EXIT_OK
    except CliUnavailable as error:
        stderr.write(f"error: product API unavailable: {error}\n")
        return EXIT_UNAVAILABLE
    except KeyboardInterrupt:  # pragma: no cover - 交互场景
        stderr.write("interrupted\n")
        return EXIT_INTERNAL
    except Exception as error:  # noqa: BLE001 - 稳定退出码契约要求兜底
        stderr.write(f"error: internal failure: {type(error).__name__}: {error}\n")
        return EXIT_INTERNAL


def fact_text_json(value: object) -> str:
    if isinstance(value, dict) and "known" in value:
        return "UNKNOWN" if not value.get("known") else str(value.get("value"))
    return str(value)


def run() -> None:  # pragma: no cover - console entry
    raise SystemExit(main())


COMMAND_SPEC.update({
    **{f"execution {topic}": path for topic, path in EXECUTION_SUBCOMMANDS.items()},
    "actions": ACTIONS_PATH,
    "action describe": ACTIONS_PATH,
    "action invoke": ACTIONS_PATH,
    "blockers": BLOCKERS_PATH,
    "metrics": METRICS_PATH,
    "capabilities": CAPABILITIES_PATH,
    "reasons": REASONS_PATH,
    "runs": RUNS_PATH,
    "run show": RUNS_PATH,
    "run compare": RUNS_COMPARE_PATH,
    "explain": "/api/v1/evidence",
    "ops status": OPS_PATH,
    "ops retention": OPS_PATH,
    "ops logging": OPS_PATH,
    "ops network": OPS_PATH,
    "report run": REPORT_PATH,
})

__all__ = ["ACTIONS_AUDIT_PATH", "ACTIONS_PATH", "ASSISTANT_CONTEXT_PATH", "BLOCKERS_PATH",
           "EXECUTION_SUBCOMMANDS",
           "CAPABILITIES_PATH", "COMMAND_SPEC", "EXIT_BLOCKED", "EXIT_CODES",
           "EXIT_INTERNAL", "EXIT_OK", "EXIT_UNAVAILABLE", "EXIT_UNKNOWN", "EXIT_USAGE", "METRICS_PATH",
           "RUNS_COMPARE_PATH", "RUNS_PATH", "CliUnavailable", "build_parser", "fetch", "main", "run"]
