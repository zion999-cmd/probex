#!/usr/bin/env bash
#
# Probex 本地唯一启动工具（一个文件，四个子命令）。
#
# 边界（重要）：
#   - up 只支持 REPLAY / PAPER。REPLAY=观察模式（零写）；PAPER=只写本地适配器，不对真实交易所下单。
#   - 不做任何真实下单；TESTNET/LIVE 需要独立人类授权与验收流程（见 docs/RUNBOOK.md §8）。
#
# 用法：
#   scripts/probex.sh up [--mode paper|replay] [--port N] [--hours N] [--local-dir DIR]
#   scripts/probex.sh demo [--port N] [--hours N]
#   scripts/probex.sh status [--port N]
#   scripts/probex.sh data <path.jsonl> [--hours N] [--force]
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_LOCAL_DIR="${HOME}/.probex/local-run"

help() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

need_python() { command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 2; }; }

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

# ---------------------------------------------------------------- demo
cmd_demo() {
  local port=8897 hours=3 out="${DEFAULT_LOCAL_DIR}/demo.json"
  while [[ $# -gt 0 ]]; do case "$1" in
    --port) port="${2:?needs value}"; shift 2 ;;
    --hours) hours="${2:?needs value}"; shift 2 ;;
    --out) out="${2:?needs value}"; shift 2 ;;
    *) echo "unknown: $1" >&2; exit 2 ;;
  esac; done
  need_python; mkdir -p "$(dirname "$out")"; cd "$REPO_ROOT"
  echo "[probex] demo port=${port} hours=${hours} out=${out}（合成 LOCAL TRIAL 数据；不下真实单）"
  exec python3 tests/ui/local_paper_demo.py "$port" "$out" "$hours"
}

# ---------------------------------------------------------------- up
cmd_up() {
  local mode=paper port="" hours=3 symbol=BTCUSDT local_dir="$DEFAULT_LOCAL_DIR"
  local event_store="" run_dir="" config_file="${REPO_ROOT}/profiles/trial-local.json" passthrough=()
  local market_source="event-store"
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
  if [[ "$market_source" == "binance-public" ]]; then
    echo "[probex] up mode=${mode} market-source=binance-public (真实公网行情；只读)"
    python3 -m runtime.assembly --mode "$mode" --symbol "$symbol" \
      --market-source binance-public --port "$port" \
      --run-registry-dir "$run_dir" \
      ${passthrough[@]+"${passthrough[@]}"} &
    local pid2=$!
    trap 'kill -TERM '$pid2' 2>/dev/null || true; wait '$pid2' 2>/dev/null || true; echo "[probex] stopped"' INT TERM
    local ready2=0
    for _ in $(seq 1 120); do
      if python3 -c "import sys,urllib.request;
raise SystemExit(0 if urllib.request.urlopen(sys.argv[1],timeout=2).status==200 else 1)" \
        "http://127.0.0.1:${port}/health/live" >/dev/null 2>&1; then ready2=1; break; fi
      kill -0 "$pid2" 2>/dev/null || break
      sleep 0.5
    done
    [[ "$ready2" == 1 ]] && echo "[probex] READY → http://127.0.0.1:${port}/" || echo "[probex] 警告：未就绪" >&2
    wait "$pid2"
    return
  fi
  mkdir -p "$local_dir"
  [[ -n "$event_store" ]] || event_store="${local_dir}/events.jsonl"
  [[ -n "$run_dir" ]] || run_dir="${local_dir}/runs"
  if [[ ! -f "$event_store" ]]; then
    echo "[probex] 生成 event store: $event_store (--hours $hours)"
    python3 "${REPO_ROOT}/scripts/make_event_store.py" "$event_store" --hours "$hours"
  fi

  echo "[probex] up mode=${mode} symbol=${symbol} UI=http://127.0.0.1:${PORT:-${port}}/  Ctrl-C 停止"
  python3 -m runtime.assembly --mode "$mode" --symbol "$symbol" \
    --config-file "$config_file" --event-store "$event_store" \
    --port "$port" --run-registry-dir "$run_dir" \
    ${passthrough[@]+"${passthrough[@]}"} &
  local pid=$!
  trap 'kill -TERM '$pid' 2>/dev/null || true; wait '$pid' 2>/dev/null || true; echo "[probex] stopped"' INT TERM

  local ready=0
  for _ in $(seq 1 120); do
    if python3 -c "import sys,urllib.request;
raise SystemExit(0 if urllib.request.urlopen(sys.argv[1],timeout=2).status==200 else 1)" \
      "http://127.0.0.1:${port}/health/live" >/dev/null 2>&1; then ready=1; break; fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  [[ "$ready" == 1 ]] && echo "[probex] READY → http://127.0.0.1:${port}/" \
    || echo "[probex] 警告：60s 内未就绪，见上方日志" >&2
  wait "$pid"
}

# ---------------------------------------------------------------- dispatch
[[ $# -ge 1 ]] || { help; exit 0; }
sub="$1"; shift
case "$sub" in
  up) cmd_up "$@" ;;
  demo) cmd_demo "$@" ;;
  status) cmd_status "$@" ;;
  data) cmd_data "$@" ;;
  -h|--help|help) help ;;
  *) echo "unknown subcommand: $sub" >&2; help; exit 2 ;;
esac
