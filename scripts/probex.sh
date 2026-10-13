#!/usr/bin/env bash
#
# Probex 本地唯一启动/停止工具（一个文件）。
#
# 边界（重要）：
#   - start 只支持 PAPER / REPLAY。REPLAY=观察模式（零写）；PAPER=只写本地适配器，不对真实交易所下单。
#   - 不做任何真实下单；TESTNET/LIVE 需要独立人类授权与验收流程（见 docs/RUNBOOK.md §8）。
#   - stop 只停止**已知 probex 入口**的进程，端口被别的程序占用时拒绝执行（不误杀）。
#
# 用法（三条常用）：
#   scripts/probex.sh start [--mode paper|replay] [--port N] [-d] [其它选项]
#   scripts/probex.sh stop  [--port N | --all]
#   scripts/probex.sh status [--port N]
#
# 其它：
#   scripts/probex.sh list                     列出正在运行的实例（PID / 端口 / 时长）
#   scripts/probex.sh data <path.jsonl> [--hours N] [--force]
#   scripts/probex.sh help
#
# 兼容旧名：up = start；demo = start --mode paper --port 8897
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_LOCAL_DIR="${HOME}/.probex/local-run"

help() { sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

need_python() { command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 2; }; }

# ---------------------------------------------------------------- 进程识别（stop / list 共用）
# 只认已知 probex 入口；避免误杀占用同端口的其它服务。
probex_cmdline() { ps -o command= -p "$1" 2>/dev/null || true; }

# 展示用：去掉解释器绝对路径前缀，只留可读的入口与参数
probex_cmdline_short() {
  probex_cmdline "$1" | sed -E 's|^[^ ]*/[Pp]ython[^ ]* ||' | cut -c1-90
}

is_probex_process() {
  local cmd; cmd="$(probex_cmdline "$1")"
  case "$cmd" in
    *runtime.assembly*|*local_paper_demo.py*|*"cli.main"*live-paper*|*"cli/main.py"*live-paper*) return 0 ;;
    *) return 1 ;;
  esac
}

# 注意：无监听时 lsof 退出码非 0；在 `set -e -o pipefail` 下必须 `|| true`，
# 否则 `pid=$(pid_on_port N)` 会让整个脚本静默退出。
pid_on_port() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }

# macOS 上 `-p X -i` 是 OR 关系，必须加 `-a`（AND），否则会列出该进程所有打开的文件。
ports_of() {
  lsof -nP -a -p "$1" -iTCP -sTCP:LISTEN 2>/dev/null \
    | awk 'NR>1 {print $9}' | sed 's/.*://' | sort -u | paste -sd, - || true
}

probex_pids() {
  local pid
  for pid in $(pgrep -f 'runtime\.assembly|local_paper_demo\.py|cli\.main.*live-paper' 2>/dev/null || true); do
    [[ "$pid" == "$$" ]] && continue
    is_probex_process "$pid" && echo "$pid"
  done
}

pidfile_for() { echo "${DEFAULT_LOCAL_DIR}/probex-${1}.pid"; }

# ---------------------------------------------------------------- list
cmd_list() {
  local pids; pids="$(probex_pids)"
  if [[ -z "$pids" ]]; then echo "没有正在运行的 probex 实例。"; return 0; fi
  printf '%-8s %-12s %-14s %s\n' PID 端口 运行时长 命令
  local pid
  for pid in $pids; do
    printf '%-8s %-12s %-14s %s\n' "$pid" "$(ports_of "$pid" | sed 's/^$/—/')" \
      "$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')" \
      "$(probex_cmdline_short "$pid")"
  done
}

# ---------------------------------------------------------------- stop
# 优雅优先：SIGINT（assembly 与 demo 都按 Ctrl-C 语义处理）→ SIGTERM → 最后才 SIGKILL（如实标注非优雅）。
stop_one() {
  local pid="$1" port_hint="${2:-—}" waited=0 started=$SECONDS
  if ! is_probex_process "$pid"; then
    echo "拒绝停止 PID ${pid}：命令行不匹配已知 probex 入口（避免误杀其它服务）" >&2
    echo "  实际命令：$(probex_cmdline "$pid" | cut -c1-120)" >&2
    return 3
  fi
  echo "→ 停止 PID ${pid}（端口 ${port_hint}）：先 SIGINT（优雅，会 finalize run）"
  kill -INT "$pid" 2>/dev/null || true
  while kill -0 "$pid" 2>/dev/null; do
    sleep 0.5; waited=$((waited + 1))
    if [[ "$waited" -eq 20 ]]; then
      echo "  10s 未退出 → SIGTERM"
      kill -TERM "$pid" 2>/dev/null || true
    fi
    if [[ "$waited" -ge 40 ]]; then
      echo "  仍未退出 → SIGKILL（**非优雅**：该 run 可能未 finalize）" >&2
      kill -KILL "$pid" 2>/dev/null || true
      break
    fi
  done
  local wait_secs=$((SECONDS - started))
  if kill -0 "$pid" 2>/dev/null; then echo "  ✗ 未停止：PID ${pid}" >&2; return 1; fi
  echo "  ✓ 已停止 PID ${pid}（用时 ${wait_secs}s）"
  return 0
}

cmd_stop() {
  local port="" all=0
  while [[ $# -gt 0 ]]; do case "$1" in
    --port) port="${2:?needs value}"; shift 2 ;;
    --all) all=1; shift ;;
    -h|--help|help) help; exit 0 ;;
    *) echo "unknown: $1" >&2; exit 2 ;;
  esac; done
  if [[ "$all" == 1 && -n "$port" ]]; then echo "error: --port 与 --all 不能同时使用" >&2; exit 2; fi

  if [[ "$all" == 1 ]]; then
    local pids rc=0 pid; pids="$(probex_pids)"
    [[ -z "$pids" ]] && { echo "没有正在运行的 probex 实例。"; return 0; }
    for pid in $pids; do stop_one "$pid" "$(ports_of "$pid")" || rc=$?; done
    return "$rc"
  fi

  port="${port:-8899}"
  local pid pidfile; pidfile="$(pidfile_for "$port")"
  pid="$(pid_on_port "$port")"
  if [[ -z "$pid" && -f "$pidfile" ]]; then
    # 端口未监听但 pid 文件存在：按文件里的 PID 兜底（例如启动失败残留）
    local recorded; recorded="$(cat "$pidfile" 2>/dev/null || true)"
    [[ -n "$recorded" ]] && kill -0 "$recorded" 2>/dev/null && pid="$recorded"
  fi
  if [[ -z "$pid" ]]; then
    rm -f "$pidfile" 2>/dev/null || true
    echo "端口 ${port} 上没有 probex 在运行（可能已停止）。"
    return 0
  fi
  local rc=0
  stop_one "$pid" "$port" || rc=$?
  [[ "$rc" == 0 ]] && rm -f "$pidfile" 2>/dev/null || true
  return "$rc"
}

# ---------------------------------------------------------------- status
cmd_status() {
  local port=8899
  while [[ $# -gt 0 ]]; do case "$1" in
    --port) port="${2:?needs value}"; shift 2 ;; *) echo "unknown: $1" >&2; exit 2 ;;
  esac; done
  local api="http://127.0.0.1:${port}"
  python3 - "$api" <<'PY'
import json, os, sys, urllib.request

base = sys.argv[1].rstrip("/")
headers = {}
token = os.environ.get("PROBEX_API_TOKEN")
if token: headers["Authorization"] = f"Bearer {token}"


def get(path: str) -> dict:
    request = urllib.request.Request(f"{base}{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def known(field: object) -> object:
    return field.get("value") if isinstance(field, dict) and field.get("known") else "UNKNOWN"


try:
    snap = get("/api/v1/snapshot")
except OSError:
    print(f"runtime 未响应：{base}（是否已启动？）"); raise SystemExit(1)

runtime, health, venue = snap.get("runtime", {}), snap.get("health", {}), snap.get("venue", {})
portfolio = snap.get("portfolio", {})
print(f"mode={runtime.get('mode')} symbol={runtime.get('symbol')}"
      f" state={known(health.get('runtime_state'))} run_id={known(health.get('runtime_run_id'))}")
print(f"venue={known(venue.get('venue_id'))} environment={known(venue.get('environment'))}"
      f" instrument={known(snap.get('instrument', {}).get('instrument_id'))}")
print(f"market healthy={known(health.get('market_healthy'))} tradeable={known(health.get('market_tradeable'))}"
      f" data_ts={known(runtime.get('data_timestamp'))}")
print(f"equity={known(portfolio.get('equity'))} position={known(portfolio.get('position_qty'))}"
      f" prediction={known(snap.get('prediction', {}).get('provider'))}")
PY
  echo; echo "== blockers =="; python3 -m cli --api-url "http://127.0.0.1:${port}" blockers 2>&1 | head -15
  echo; echo "== runs =="; python3 -m cli --api-url "http://127.0.0.1:${port}" runs --limit 5 2>&1 | head -8
}

# ---------------------------------------------------------------- data
cmd_data() {
  [[ $# -ge 1 ]] || { echo "usage: probex.sh data <path.jsonl> [--hours N] [--force]" >&2; exit 2; }
  local path="$1"; shift
  python3 "${REPO_ROOT}/scripts/make_event_store.py" "$path" "$@"
}

# ---------------------------------------------------------------- start
cmd_start() {
  local mode=paper port="" hours=3 symbol=BTCUSDT local_dir="$DEFAULT_LOCAL_DIR"
  local event_store="" run_dir="" config_file="${REPO_ROOT}/profiles/trial-local.json" passthrough=()
  local market_source="event-store" background=0 out=""
  while [[ $# -gt 0 ]]; do case "$1" in
    --mode) mode="${2:?needs value}"; shift 2 ;;
    --port) port="${2:?needs value}"; shift 2 ;;
    --hours) hours="${2:?needs value}"; shift 2 ;;
    --symbol) symbol="${2:?needs value}"; shift 2 ;;
    --local-dir) local_dir="${2:?needs value}"; shift 2 ;;
    --event-store) event_store="${2:?needs value}"; shift 2 ;;
    --run-dir) run_dir="${2:?needs value}"; shift 2 ;;
    --config-file) config_file="${2:?needs value}"; shift 2 ;;
    --market-source) market_source="${2:?needs value}"; shift 2 ;;
    -d|--background) background=1; shift ;;
    -h|--help|help) help; exit 0 ;;
    --) shift; passthrough=("$@"); break ;;
    *) echo "unknown: $1" >&2; exit 2 ;;
  esac; done

  mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]')"
  case "$mode" in
    paper) port="${port:-8899}" ;; replay) port="${port:-8898}" ;;
    *) echo "error: --mode must be paper or replay (got '$mode')" >&2
       echo "       TESTNET/LIVE 需要独立授权：docs/RUNBOOK.md §8" >&2; exit 2 ;;
  esac
  need_python
  [[ -f "$config_file" ]] || { echo "error: config not found: $config_file" >&2; exit 2; }

  # 端口占用必须给出可执行的下一步（不再让用户去猜 / 手动 kill）
  local busy; busy="$(pid_on_port "$port")"
  if [[ -n "$busy" ]]; then
    echo "error: 端口 ${port} 已被占用（PID ${busy}）" >&2
    echo "       停止它：scripts/probex.sh stop --port ${port}" >&2
    echo "       另起一个：scripts/probex.sh start --port $((port + 1))" >&2
    exit 2
  fi
  mkdir -p "$local_dir"
  [[ -n "$event_store" ]] || event_store="${local_dir}/events.jsonl"
  [[ -n "$run_dir" ]] || run_dir="${local_dir}/runs"
  if [[ "$market_source" == "event-store" && ! -f "$event_store" ]]; then
    echo "[probex] 生成 event store: $event_store (--hours $hours，合成 LOCAL TRIAL 数据)"
    python3 "${REPO_ROOT}/scripts/make_event_store.py" "$event_store" --hours "$hours"
  fi

  local args=(--mode "$mode" --symbol "$symbol" --port "$port" --run-registry-dir "$run_dir")
  if [[ "$market_source" == "event-store" ]]; then
    args+=(--config-file "$config_file" --event-store "$event_store")
  else
    args+=(--market-source "$market_source")
    echo "[probex] market-source=${market_source}（真实公网行情；只读，无需凭据）"
  fi
  args+=(${passthrough[@]+"${passthrough[@]}"})

  local pidfile; pidfile="$(pidfile_for "$port")"
  if [[ "$background" == 1 ]]; then
    out="${local_dir}/probex-${port}.log"
    echo "[probex] start mode=${mode} port=${port}（后台）日志：$out"
    nohup python3 -m runtime.assembly "${args[@]}" >"$out" 2>&1 &
    local bg=$!
    echo "$bg" > "$pidfile"
  else
    echo "[probex] start mode=${mode} port=${port} UI=http://127.0.0.1:${port}/  Ctrl-C 停止"
    python3 -m runtime.assembly "${args[@]}" &
    local bg=$!
    echo "$bg" > "$pidfile"
    # Ctrl-C / TERM ⇒ 对子进程发 SIGINT（与 assembly 的优雅关闭一致：先 finalize run）
    trap 'kill -INT '"$bg"' 2>/dev/null || true; wait '"$bg"' 2>/dev/null || true; rm -f "'"$pidfile"'"; echo "[probex] stopped"' INT TERM
  fi

  local ready=0
  for _ in $(seq 1 120); do
    if python3 -c "import sys,urllib.request;
raise SystemExit(0 if urllib.request.urlopen(sys.argv[1],timeout=2).status==200 else 1)" \
      "http://127.0.0.1:${port}/health/live" >/dev/null 2>&1; then ready=1; break; fi
    kill -0 "$bg" 2>/dev/null || break
    sleep 0.5
  done
  if [[ "$ready" == 1 ]]; then
    echo "[probex] READY → http://127.0.0.1:${port}/"
    [[ "$background" == 1 ]] && echo "[probex] 停止：scripts/probex.sh stop --port ${port}（或 stop --all）"
  else
    echo "[probex] 警告：60s 内未就绪；停止：scripts/probex.sh stop --port ${port}" >&2
  fi
  [[ "$background" == 1 ]] && return 0
  wait "$bg"
}

# ---------------------------------------------------------------- dispatch
[[ $# -ge 1 ]] || { help; exit 0; }
sub="$1"; shift
case "$sub" in
  start) cmd_start "$@" ;;
  stop) cmd_stop "$@" ;;
  status) cmd_status "$@" ;;
  list|ps) cmd_list "$@" ;;
  data) cmd_data "$@" ;;
  up) cmd_start "$@" ;;                       # 兼容旧名
  demo) cmd_start --mode paper --port 8897 "$@" ;;   # 兼容旧名（合成数据 + PAPER）
  -h|--help|help) help ;;
  *) echo "unknown subcommand: $sub" >&2; help; exit 2 ;;
esac
