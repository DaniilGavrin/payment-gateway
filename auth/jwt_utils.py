# auth/jwt_utils.py
import os
import time
from datetime import datetime, timedelta
from jose import jwt
from jose.exceptions import JWTError, ExpiredSignatureError
import uuid

JWT_SECRET = os.getenv("JWT_SECRET")
ACCESS_TOKEN_EXPIRE_MINUTES = 15

def create_access_token(tg_id: int, username: str | None = None) -> str:
    """Создает короткоживущий access token"""
    payload = {
        "sub": str(tg_id),  # Стандартный claim для subject (user ID)
        "username": username,
        "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "iat": datetime.utcnow(),  # Issued at
        "type": "access"
    }
    
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_access_token(token: str) -> dict | None:
    """Проверяет access token и возвращает payload"""
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_exp": True}
        )
        
        # Проверяем, что это именно access token
        if payload.get("type") != "access":
            return None
            
        return payload
    except ExpiredSignatureError:
        return None
    except JWTError as e:
        print(f"[JWT ERROR] {e}")
        return None
    except Exception as e:
        print(f"[JWT ERROR] Unexpected: {e}")
        return None

def create_refresh_token() -> str:
    """Генерирует случайный refresh token (просто UUID)"""
    return str(uuid.uuid4())