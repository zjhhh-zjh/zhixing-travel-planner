"""
全局日志器
用 loguru 统一日志格式与轮转策略
"""
import os
import sys
from loguru import logger
from app.config import BASE_DIR

# 日志目录（不存在则创建）
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 清除 loguru 默认的 stderr handler，全部按我们自己的规则来
logger.remove()

# 1) 控制台输出（彩色、开发时观察用）
logger.add(
    sys.stdout,
    level="INFO",
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
    colorize=True,
)

# 2) 文件输出（应用日志，按天切分，保留 7 天）
logger.add(
    os.path.join(LOG_DIR, "app.log"),
    level="INFO",
    rotation="00:00",      # 每天 0 点切一个新文件
    retention="7 days",    # 只保留最近 7 天
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
)

# 3) 错误日志单独一份，方便排障
logger.add(
    os.path.join(LOG_DIR, "error.log"),
    level="ERROR",
    rotation="00:00",
    retention="30 days",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}\n{exception}",
)

# 对外统一叫 app_logger，便于其他模块 from app.utils.logger import app_logger
app_logger = logger