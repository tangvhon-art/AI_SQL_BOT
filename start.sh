#!/usr/bin/env bash
# =============================================================================
# AI 问数系统（AI SQL Bot）一键启动脚本
#
# 功能：
#   1. 同时启动后端（FastAPI + uvicorn，端口 8001）与前端（Vite，端口 3001）
#   2. 启动前若端口被占用，自动停止占用进程（SIGTERM → 超时 SIGKILL）后再启动，避免半启动
#   3. 保持前台运行，实时滚动显示两个服务的日志（logs/backend.log、logs/frontend.log）
#   4. 按 Ctrl+C 停止时：
#      - 先向后端 uvicorn 发送 SIGINT，触发 FastAPI shutdown 事件 → 显式停止定时任务调度线程 → 进程退出
#      - 再停止前端 Vite 进程
#      - 最后清理日志展示进程
#   5. 任一服务意外退出（如端口冲突、崩溃）时自动整体清理，不残留
#
# 用法：./start.sh   （在项目根目录执行；如需其他端口：BACKEND_PORT=9001 FRONTEND_PORT=5173 ./start.sh）
# 提示：如开启后端热重载（--reload），uvicorn 会额外派生子进程，Ctrl+C 仍可整体退出
# =============================================================================

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
LOG_DIR="$ROOT_DIR/logs"

BACKEND_PORT="${BACKEND_PORT:-8001}"    # 与 frontend/vite.config.ts 的代理目标一致
FRONTEND_PORT="${FRONTEND_PORT:-3001}"  # 与 frontend/vite.config.ts 的 dev 端口一致
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"

UVICORN_BIN="$BACKEND_DIR/.venv/bin/uvicorn"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

mkdir -p "$LOG_DIR"

# ---- 环境检查 --------------------------------------------------------------
if [[ ! -x "$UVICORN_BIN" ]]; then
    echo "[错误] 未找到后端虚拟环境 uvicorn：$UVICORN_BIN"
    echo "请先执行：cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    echo "[错误] 前端依赖未安装，请先执行：cd frontend && npm install"
    exit 1
fi

# ---- 端口释放：若被占用则先停止监听进程（SIGTERM → 超时 SIGKILL），再启动 ----
free_port() {
    local port="$1" label="$2"
    if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        return 0
    fi
    echo "[端口] ${label}端口 ${port} 被占用，正在停止占用进程..."
    local pids
    pids=$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u)
    if [[ -z "$pids" ]]; then
        return 0
    fi
    echo "$pids" | while read -r pid; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "  - 停止进程 ${pid}: $(ps -p "$pid" -o command= 2>/dev/null | tr -d '\r' | head -c 120)"
            kill -TERM "$pid" 2>/dev/null
        fi
    done
    # 等待端口释放（最多 5 秒）
    for _ in $(seq 1 10); do
        if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            echo "[端口] ${label}端口 ${port} 已释放"
            return 0
        fi
        sleep 0.5
    done
    # 仍未释放，强制终止
    echo "$pids" | while read -r pid; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "  - 强制终止进程 ${pid}"
            kill -9 "$pid" 2>/dev/null
        fi
    done
    sleep 1
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "[错误] ${label}端口 ${port} 无法释放，请手动检查："
        lsof -nP -iTCP:"$port" -sTCP:LISTEN
        exit 1
    fi
    echo "[端口] ${label}端口 ${port} 已释放"
}
free_port "$BACKEND_PORT" "后端"
free_port "$FRONTEND_PORT" "前端"

BACKEND_PID=""
FRONTEND_PID=""
TAIL_PID=""
CLEANED=0

# ---- 停止逻辑 --------------------------------------------------------------
cleanup() {
    if (( CLEANED )); then exit 0; fi
    CLEANED=1
    echo ""
    echo "[停止] 正在停止 AI 问数系统..."

    # 1) 优雅停止后端：SIGINT → uvicorn 触发 FastAPI shutdown 事件 → 停止调度线程 → 进程退出
    if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "[停止] 向后端发送 SIGINT（触发定时任务线程优雅停止）..."
        kill -INT "$BACKEND_PID" 2>/dev/null
        for _ in $(seq 1 20); do
            kill -0 "$BACKEND_PID" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$BACKEND_PID" 2>/dev/null; then
            echo "[停止] 后端 10 秒内未退出，强制终止"
            kill -9 "$BACKEND_PID" 2>/dev/null
        fi
    fi

    # 2) 停止前端 Vite（npm 收到 SIGINT 会转发给 vite 子进程）
    if [[ -n "$FRONTEND_PID" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "[停止] 停止前端 Vite..."
        kill -INT "$FRONTEND_PID" 2>/dev/null
        for _ in $(seq 1 10); do
            kill -0 "$FRONTEND_PID" 2>/dev/null || break
            sleep 0.3
        done
        if kill -0 "$FRONTEND_PID" 2>/dev/null; then
            echo "[停止] 前端 3 秒内未退出，强制终止"
            kill -9 "$FRONTEND_PID" 2>/dev/null
        fi
    fi

    # 3) 清理日志展示进程
    if [[ -n "$TAIL_PID" ]] && kill -0 "$TAIL_PID" 2>/dev/null; then
        kill -TERM "$TAIL_PID" 2>/dev/null
    fi

    echo "[停止] 全部服务已停止"
    exit 0
}
trap cleanup INT TERM

# ---- 启动 ------------------------------------------------------------------
echo "============================================================"
echo "  AI 问数系统启动中"
echo "  后端 API:   http://localhost:${BACKEND_PORT}   (接口文档 /docs)"
echo "  前端页面:   http://localhost:${FRONTEND_PORT}"
echo "  日志文件:   $BACKEND_LOG"
echo "              $FRONTEND_LOG"
echo "  按 Ctrl+C 停止（会优雅停止定时任务调度线程）"
echo "============================================================"

# 启动后端（exec 使子进程 PID 即 uvicorn 本身，Ctrl+C 时信号可直接命中）
( cd "$BACKEND_DIR" && exec "$UVICORN_BIN" app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" ) >> "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

# 启动前端（npm 收到 SIGINT 会转发给 vite）
( cd "$FRONTEND_DIR" && exec npm run dev -- --port "$FRONTEND_PORT" ) >> "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

# 实时滚动展示两个日志，保持脚本前台运行
tail -f -n 50 "$BACKEND_LOG" "$FRONTEND_LOG" &
TAIL_PID=$!

# 轮询存活：任一服务意外退出则整体清理（兼容 macOS bash 3.2，无需 wait -n）
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
    sleep 1
done

# 有服务退出，触发整体清理（若因 Ctrl+C 中断，trap 已执行 cleanup 并 exit）
cleanup
