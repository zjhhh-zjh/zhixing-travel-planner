"""
安全工具（密码加密、JWT）
"""
from datetime import datetime, timedelta
from typing import Optional
import bcrypt
import jwt
from app.config import settings


# ============== 密码加密 ==============

def hash_password(password: str) -> str:
    """哈希密码"""
    salt = bcrypt.gensalt()  # ★ 每次随机加盐
    return bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


# ============== JWT ==============

SECRET_KEY = settings.jwt_secret_key  # 改用专门 JWT 密钥（原借用 dashscope_api_key）
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 天


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建 JWT 访问令牌"""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    """解码 JWT 令牌"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:  # ★ 篡改、伪造、乱码都在这里变成 None
        return None