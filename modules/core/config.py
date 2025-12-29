# Description: Configuration file for the bot
# replace with secure storage (e.g., environment variable / secrets manager)
from dotenv import load_dotenv
from pathlib import Path
from urllib.parse import quote_plus
import os

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / '.env')

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL") or "gemini-2.5-pro"

ZAI_API_KEY = os.getenv("ZAI_API_KEY") or os.getenv("ZHIPUAI_API_KEY")
ZHIPU_BASE_URL = os.getenv("ZHIPU_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL = os.getenv("ZHIPU_MODEL") or "glm-4.5-flash"
ZHIPU_TRANSLATE_MODEL = os.getenv("ZHIPU_TRANSLATE_MODEL") or ZHIPU_MODEL
ZHIPU_VISION_MODEL = os.getenv("ZHIPU_VISION_MODEL") or "glm-4.1v-thinking-flash"
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_API_ENDPOINT = os.getenv("AZURE_OPENAI_API_ENDPOINT") or os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION") or "2024-12-01-preview"
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE_OPENAI_MODEL")

def _build_azure_base_url() -> str:
    if not AZURE_OPENAI_API_ENDPOINT or not AZURE_OPENAI_DEPLOYMENT:
        return ""
    return (
        f"{AZURE_OPENAI_API_ENDPOINT.rstrip('/')}/openai/deployments/"
        f"{AZURE_OPENAI_DEPLOYMENT}"
    )

AZURE_OPENAI_BASE_URL = os.getenv("AZURE_OPENAI_BASE_URL") or _build_azure_base_url()

SUMMARY_MODEL = os.getenv("SUMMARY_MODEL") or GEMINI_MODEL
SUMMARY_FALLBACK_MODEL = os.getenv("SUMMARY_FALLBACK_MODEL") or GEMINI_FALLBACK_MODEL

JUDGE0_API_URL = "https://ce.judge0.com"
JUDGE0_API_KEY = os.getenv("JUDGE0_API_KEY")

ADMIN_USER_ID = 1002288404

MYSQL_CONFIG = {
    'host': os.getenv("MYSQL_HOST"),
    'user': os.getenv("MYSQL_USER"),
    'password': os.getenv("MYSQL_PASSWORD"),
    'database': os.getenv("MYSQL_DATABASE")
}

MYSQL_POOL_SIZE = int(os.getenv("MYSQL_POOL_SIZE", "5"))
MYSQL_MAX_OVERFLOW = int(os.getenv("MYSQL_MAX_OVERFLOW", "10"))
MYSQL_POOL_RECYCLE = int(os.getenv("MYSQL_POOL_RECYCLE", "1800"))
MYSQL_CONNECT_TIMEOUT = int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10"))

def _build_mysql_dsn() -> str:
    user = MYSQL_CONFIG.get("user") or ""
    password = MYSQL_CONFIG.get("password") or ""
    host = MYSQL_CONFIG.get("host") or "localhost"
    database = MYSQL_CONFIG.get("database") or ""
    port = os.getenv("MYSQL_PORT")

    auth = user
    if password:
        auth = f"{user}:{quote_plus(password)}"

    location = f"{host}:{port}" if port else host
    return f"mysql+asyncmy://{auth}@{location}/{database}?charset=utf8mb4"

SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL") or _build_mysql_dsn()

# 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL="INFO"

# help 命令的帮助信息
HELP_TEXT = (
        "***指令列表：***\n"
        "/start - 开始使用\n"
        "/help - 查看帮助文档\n"
        "/ref - 查看邀请信息或绑定邀请人\n"
        "/tl - 中英互译功能\n"
        "/music - 搜索音乐\n\n"
        # "/sf - 检测分享链接是否泄露隐私\n\n"（暂时关闭）
        "***群组相关：***\n"
        "/fogmoebot - 在群组中连接\n"
        "/report - 举报垃圾消息给群管理\n"
        "/verify - 管理新成员验证\n"
        "/spam - 垃圾消息管制\n"
        "/keyword - 设置关键词自动回复\n"
        "/chart - 代币图表功能\n\n"
        "***聊天相关：***\n"
        "/setmyinfo - 设置个性化提示词\n"
        "/clear - 清除个人记忆\n\n"
        "***数据相关：***\n"
        "/me - 获取个人信息\n"
        "/lottery - 每天一次免费获取金币\n"
        "/checkin - 每日签到\n"
        "/task - 查看任务列表\n"
        "/shop - 打开商城列表\n"
        "/give - 赠送某人金币\n"
        "/rich - 查看富豪榜前五\n"
        "/stake - 质押经济系统\n"
        "/swap - 兑换$FOGMOE代币\n"
        "/charge - 使用卡密充值金币\n"
        "/bribe - 贿赂雾萌娘提升好感度\n"
        "/webpassword - 设置Web登录密码\n\n"
        "***娱乐相关：***\n"
        "/omikuji - 抽取御神签预测运势\n"
        "/rps\\_game - 猜拳游戏\n"
        "/gamble - 发起赌博游戏\n"
        "/sicbo - 骰子游戏\n"
        "/btc\\_predict - 比特币模拟事件合约\n"
        "/pic - 获取随机图片\n"
        "/rpg - 文字游戏"
    )

# AI 系统提示词
with open(BASE_DIR / 'prompts' / 'system.md', 'r', encoding='utf-8') as f:
    SYSTEM_PROMPT = f.read()

# AI 服务的排序，按照优先级从高到低排序
AI_SERVICE_ORDER = ["gemini", "azure", "zhipu"]
