#!/bin/bash

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BOT_DIR="$SCRIPT_DIR"
MODULES_DIR="$BOT_DIR/modules"
LOG_FILE="$BOT_DIR/tgbot.log"
VENV_DIR="$BOT_DIR/venv"
REQUIREMENTS_FILE="$BOT_DIR/requirements.txt"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 获取bot进程ID
get_bot_pid() {
    ps -ef | grep "[p]ython3.*modules/main.py" | awk '{print $2}'
}

# 检查并创建虚拟环境
setup_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${YELLOW}虚拟环境不存在，正在创建...${NC}"
        python3 -m venv "$VENV_DIR"

        if [ $? -ne 0 ]; then
            echo -e "${RED}✗ 创建虚拟环境失败${NC}"
            echo "请确保已安装 python3-venv:"
            echo "  Ubuntu/Debian: sudo apt install python3-venv"
            echo "  CentOS/RHEL: sudo yum install python3-venv"
            exit 1
        fi

        echo -e "${GREEN}✓ 虚拟环境创建成功${NC}"
    fi
}

# 安装依赖
install_dependencies() {
    echo -e "${YELLOW}正在检查并安装依赖...${NC}"

    # 激活虚拟环境
    source "$VENV_DIR/bin/activate"

    # 升级 pip
    echo "升级 pip..."
    pip install --upgrade pip -q

    # 安装依赖
    if [ -f "$REQUIREMENTS_FILE" ]; then
        echo "安装 requirements.txt 中的依赖..."
        pip install -r "$REQUIREMENTS_FILE"

        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✓ 依赖安装成功${NC}"
        else
            echo -e "${RED}✗ 依赖安装失败${NC}"
            exit 1
        fi
    else
        echo -e "${RED}错误: requirements.txt 文件不存在${NC}"
        exit 1
    fi
}

# 初始化环境（首次设置）
init_environment() {
    echo "=== 初始化雾萌娘 Telegram Bot 环境 ==="
    echo ""

    # 创建虚拟环境
    setup_venv

    # 安装依赖
    install_dependencies

    # 检查 .env 文件
    if [ ! -f "$BOT_DIR/.env" ]; then
        echo ""
        echo -e "${YELLOW}警告: .env 文件不存在${NC}"

        if [ -f "$BOT_DIR/.env.example" ]; then
            echo "是否要从 .env.example 创建 .env 文件? (y/n)"
            read -r response
            if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
                cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
                echo -e "${GREEN}✓ 已创建 .env 文件${NC}"
                echo -e "${YELLOW}请编辑 .env 文件并配置必要的环境变量${NC}"
                echo "  nano $BOT_DIR/.env"
            fi
        else
            echo -e "${RED}错误: .env.example 文件也不存在${NC}"
        fi
    fi

    echo ""
    echo -e "${GREEN}✓ 环境初始化完成！${NC}"
    echo ""
    echo "下一步:"
    echo "  1. 配置 .env 文件中的必要参数"
    echo "  2. 运行数据库迁移: alembic upgrade head"
    echo "  3. 启动 bot: $0 start"
}

# 启动bot
start_bot() {
    echo "=== 雾萌娘 Telegram Bot 启动脚本 ==="
    echo "Bot 目录: $BOT_DIR"

    # 检查是否已经在运行
    OLD_PID=$(get_bot_pid)
    if [ ! -z "$OLD_PID" ]; then
        echo -e "${YELLOW}Bot已在运行 (PID: $OLD_PID)${NC}"
        echo "如需重启，请使用: $0 restart"
        exit 1
    fi

    # 确保目录存在
    if [ ! -d "$BOT_DIR" ]; then
        echo -e "${RED}错误: Bot目录不存在: $BOT_DIR${NC}"
        exit 1
    fi

    # 确保 modules 目录存在
    if [ ! -d "$MODULES_DIR" ]; then
        echo -e "${RED}错误: modules 目录不存在: $MODULES_DIR${NC}"
        exit 1
    fi

    # 确保 main.py 存在
    if [ ! -f "$MODULES_DIR/main.py" ]; then
        echo -e "${RED}错误: main.py 不存在: $MODULES_DIR/main.py${NC}"
        exit 1
    fi

    # 检查虚拟环境
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${YELLOW}虚拟环境不存在${NC}"
        echo "是否要初始化环境? (y/n)"
        read -r response
        if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
            init_environment
            echo ""
        else
            echo -e "${RED}无法启动: 需要虚拟环境${NC}"
            echo "请运行: $0 init"
            exit 1
        fi
    fi

    # 激活虚拟环境
    echo "激活虚拟环境..."
    source "$VENV_DIR/bin/activate"

    # 检查 .env 文件
    if [ ! -f "$BOT_DIR/.env" ]; then
        echo -e "${RED}错误: .env 文件不存在！${NC}"
        echo "请先创建 .env 文件并配置必要的环境变量"
        echo "可以参考 .env.example 文件"
        exit 1
    fi

    # 切换到 modules 目录
    cd $MODULES_DIR

    # 启动bot并记录日志
    echo "正在启动bot..."
    echo "日志文件: $LOG_FILE"
    nohup python3 -u main.py > $LOG_FILE 2>&1 &

    # 获取新进程PID
    NEW_PID=$!
    echo "Bot已启动 (PID: $NEW_PID)"

    # 检查进程是否成功启动
    sleep 2
    if ps -p $NEW_PID > /dev/null; then
        echo -e "${GREEN}✓ Bot运行正常${NC}"
        echo ""
        echo "查看日志: tail -f $LOG_FILE"
        echo "停止bot: $0 stop"
        echo "查看状态: $0 status"
    else
        echo -e "${RED}✗ 错误: Bot启动失败${NC}"
        echo "请查看日志文件: $LOG_FILE"
        exit 1
    fi
}

# 停止bot
stop_bot() {
    echo "=== 雾萌娘 Telegram Bot 停止脚本 ==="

    BOT_PID=$(get_bot_pid)

    if [ -z "$BOT_PID" ]; then
        echo "未发现运行中的bot进程"
        exit 0
    fi

    echo "发现bot进程 (PID: $BOT_PID)"
    echo "正在停止..."

    # 尝试优雅地停止
    kill $BOT_PID

    # 等待进程结束
    sleep 3

    # 检查是否还在运行
    if ps -p $BOT_PID > /dev/null 2>&1; then
        echo "进程未响应，强制终止..."
        kill -9 $BOT_PID
        sleep 1
    fi

    # 最终检查
    if ps -p $BOT_PID > /dev/null 2>&1; then
        echo -e "${RED}✗ 错误: 无法停止进程 $BOT_PID${NC}"
        exit 1
    else
        echo -e "${GREEN}✓ Bot已成功停止${NC}"

        # 显示最后几行日志
        if [ -f "$LOG_FILE" ]; then
            echo ""
            echo "=== 最后10行日志 ==="
            tail -n 10 "$LOG_FILE"
        fi
    fi
}

# 重启bot
restart_bot() {
    echo "=== 重启 Bot ==="
    stop_bot
    echo ""
    sleep 2
    start_bot
}

# 查看状态
status_bot() {
    echo "=== 雾萌娘 Telegram Bot 状态 ==="

    BOT_PID=$(get_bot_pid)

    if [ -z "$BOT_PID" ]; then
        echo -e "状态: ${RED}✗ 未运行${NC}"
        exit 1
    else
        echo -e "状态: ${GREEN}✓ 运行中${NC}"
        echo "PID: $BOT_PID"

        # 显示进程信息
        echo ""
        echo "进程详情:"
        ps -fp $BOT_PID

        # 检查虚拟环境
        if [ -d "$VENV_DIR" ]; then
            echo ""
            echo "虚拟环境: ✓ $VENV_DIR"
        fi

        # 显示最后几行日志
        if [ -f "$LOG_FILE" ]; then
            echo ""
            echo "=== 最后10行日志 ==="
            tail -n 10 "$LOG_FILE"
        fi
    fi
}

# 更新依赖
update_deps() {
    echo "=== 更新依赖 ==="

    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${RED}错误: 虚拟环境不存在${NC}"
        echo "请先运行: $0 init"
        exit 1
    fi

    # 检查bot是否在运行
    BOT_PID=$(get_bot_pid)
    if [ ! -z "$BOT_PID" ]; then
        echo -e "${YELLOW}警告: Bot正在运行，建议先停止${NC}"
        echo "是否继续更新? (y/n)"
        read -r response
        if [[ ! "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
            exit 0
        fi
    fi

    install_dependencies

    echo ""
    echo -e "${GREEN}✓ 依赖更新完成${NC}"
    echo "如果bot正在运行，建议重启: $0 restart"
}

# 显示帮助
show_help() {
    echo "雾萌娘 Telegram Bot 管理脚本"
    echo ""
    echo "用法: $0 [命令]"
    echo ""
    echo "命令:"
    echo "  init      初始化环境（创建虚拟环境并安装依赖）"
    echo "  start     启动bot（默认）"
    echo "  stop      停止bot"
    echo "  restart   重启bot"
    echo "  status    查看bot状态"
    echo "  update    更新依赖包"
    echo "  help      显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 init      # 首次使用，初始化环境"
    echo "  $0           # 启动bot"
    echo "  $0 start     # 启动bot"
    echo "  $0 stop      # 停止bot"
    echo "  $0 restart   # 重启bot"
    echo "  $0 status    # 查看状态"
    echo "  $0 update    # 更新依赖"
    echo ""
    echo "首次使用流程:"
    echo "  1. $0 init                           # 初始化环境"
    echo "  2. 编辑 .env 文件配置必要参数         # nano .env"
    echo "  3. 运行数据库迁移                     # alembic upgrade head"
    echo "  4. $0 start                          # 启动bot"
}

# 主逻辑
case "${1:-start}" in
    init|setup|install)
        init_environment
        ;;
    start)
        start_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        restart_bot
        ;;
    status)
        status_bot
        ;;
    update|upgrade)
        update_deps
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo -e "${RED}错误: 未知命令 '$1'${NC}"
        echo ""
        show_help
        exit 1
        ;;
esac
