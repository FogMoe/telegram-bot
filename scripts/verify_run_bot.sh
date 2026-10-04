#!/bin/bash
# 验证 runBot.sh 的 start / status / restart / stop 使用同一套进程识别。
#
# 在临时目录里复制 runBot.sh，用一个只会睡眠的替身 modules/main.py 代替真实 bot，
# 因此不需要 .env、数据库或 Telegram。
#
# 用法：scripts/verify_run_bot.sh（Linux / macOS）
# Windows Git Bash：VERIFY_BASH_STUB=1 scripts/verify_run_bot.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
FAILURES=0
OUTPUT=""
STATUS=0
EXTRA_PIDS=()

cleanup() {
    local pid
    if [ -f "$WORK/logs/tgbot.pid" ]; then
        kill -9 "$(cat "$WORK/logs/tgbot.pid")" 2>/dev/null
    fi
    for pid in "${EXTRA_PIDS[@]}"; do
        kill -9 "$pid" 2>/dev/null
    done
    rm -rf "$WORK"
}
trap cleanup EXIT

pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; FAILURES=$((FAILURES + 1)); }

# 运行 runBot.sh，输出和退出码放进 OUTPUT / STATUS
run() {
    OUTPUT=$(bash "$WORK/runBot.sh" "$@" 2>&1)
    STATUS=$?
}

expect_status() {
    local description="$1" expected="$2"
    shift 2
    run "$@"
    if [ "$STATUS" -eq "$expected" ]; then
        pass "$description"
    else
        fail "$description (exit $STATUS, expected $expected)"
        echo "$OUTPUT" | sed 's/^/       /'
    fi
}

bot_pid() { tr -d '[:space:]' < "$WORK/logs/tgbot.pid" 2>/dev/null; }

alive() {
    local state
    kill -0 "$1" 2>/dev/null || return 1
    state=$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' ')
    [[ "$state" != Z* ]]
}

# 最多等 10 秒让进程退出
wait_gone() {
    local pid="$1" i
    for i in $(seq 1 10); do
        alive "$pid" || return 0
        sleep 1
    done
    return 1
}

# --- 准备临时项目：真实的 runBot.sh + 替身入口 ---
mkdir -p "$WORK/modules" "$WORK/venv/bin" "$WORK/logs"
cp "$ROOT/runBot.sh" "$WORK/runBot.sh"
: > "$WORK/venv/bin/activate"
: > "$WORK/.env"
cat > "$WORK/modules/main.py" <<'PY'
import os
import signal
import sys
import time

if not os.environ.get("STUB_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

while True:
    time.sleep(1)
PY

if [ "${VERIFY_BASH_STUB:-0}" = "1" ]; then
    # Git Bash 里的原生 Windows Python 进程没有 Linux 的 PID 和命令行语义。
    # 用 bash 脚本充当 python3：命令行形态与真实进程相同（python3 -u <入口脚本>）。
    cat > "$WORK/venv/bin/python3" <<'SH'
#!/bin/bash
if [ -n "${STUB_IGNORE_TERM:-}" ]; then trap '' TERM; else trap 'exit 0' TERM; fi
while true; do sleep 1; done
SH
    chmod +x "$WORK/venv/bin/python3"
    export PATH="$WORK/venv/bin:$PATH"
fi

# --- 未启动时 ---
expect_status "status reports not running before start" 1 status
expect_status "stop is a no-op when nothing runs" 0 stop

# --- start / status ---
expect_status "start launches the bot" 0 start
FIRST_PID=$(bot_pid)
if [ -n "$FIRST_PID" ] && alive "$FIRST_PID"; then
    pass "pid file points to a live process"
else
    fail "pid file points to a live process (pid='$FIRST_PID')"
fi
expect_status "status reports running after start" 0 status
if [[ "$OUTPUT" == *"$FIRST_PID"* ]]; then
    pass "status prints the pid from the pid file"
else
    fail "status prints the pid from the pid file"
fi
expect_status "second start is refused" 1 start

# --- restart 换新进程 ---
expect_status "restart succeeds" 0 restart
SECOND_PID=$(bot_pid)
if [ -n "$SECOND_PID" ] && [ "$SECOND_PID" != "$FIRST_PID" ] && alive "$SECOND_PID"; then
    pass "restart replaced the process"
else
    fail "restart replaced the process (old=$FIRST_PID new=$SECOND_PID)"
fi
if alive "$FIRST_PID"; then
    fail "old process exited after restart"
else
    pass "old process exited after restart"
fi
expect_status "status reports running after restart" 0 status

# --- stop ---
expect_status "stop terminates the bot" 0 stop
if wait_gone "$SECOND_PID"; then
    pass "process is gone after stop"
else
    fail "process is gone after stop"
fi
if [ -f "$WORK/logs/tgbot.pid" ]; then
    fail "pid file removed after stop"
else
    pass "pid file removed after stop"
fi
expect_status "status reports not running after stop" 1 status

# --- 未运行时 restart 也要能启动 ---
expect_status "restart starts the bot when it was not running" 0 restart
expect_status "status reports running after cold restart" 0 status
expect_status "stop after cold restart" 0 stop

# --- PID 文件指向无关进程：不能被当成 bot，也不能被 stop 杀掉 ---
sleep 300 &
UNRELATED_PID=$!
EXTRA_PIDS+=("$UNRELATED_PID")
echo "$UNRELATED_PID" > "$WORK/logs/tgbot.pid"
expect_status "status ignores an unrelated pid in the pid file" 1 status
expect_status "stop leaves an unrelated process alone" 0 stop
if alive "$UNRELATED_PID"; then
    pass "unrelated process survived stop"
else
    fail "unrelated process survived stop"
fi
kill "$UNRELATED_PID" 2>/dev/null

# --- 没有 PID 文件、用旧方式（cd modules && python3 -u main.py）启动的进程（仅 Linux） ---
if [ -d /proc/self ]; then
    rm -f "$WORK/logs/tgbot.pid"
    (cd "$WORK/modules" && nohup python3 -u main.py > /dev/null 2>&1 &)
    sleep 1
    expect_status "status finds a bot started the legacy way" 0 status
    expect_status "start refuses while a legacy-style bot runs" 1 start
    expect_status "stop terminates a legacy-style bot" 0 stop
    expect_status "status reports not running after legacy stop" 1 status
else
    echo "skip - legacy process discovery needs /proc"
fi

# --- 忽略 SIGTERM 的进程在超时后被强制终止 ---
STUB_IGNORE_TERM=1 BOT_STOP_TIMEOUT=2 expect_status "start a bot that ignores SIGTERM" 0 start
STUBBORN_PID=$(bot_pid)
STUB_IGNORE_TERM=1 BOT_STOP_TIMEOUT=2 expect_status "stop force-kills after the timeout" 0 stop
if wait_gone "$STUBBORN_PID"; then
    pass "stubborn process is gone after stop"
else
    fail "stubborn process is gone after stop"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "runBot.sh verification passed"
    exit 0
fi
echo "runBot.sh verification failed: $FAILURES check(s)"
exit 1
