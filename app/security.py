import os
from datetime import datetime, timedelta, timezone  # время для exp/iat в JWT
from dotenv import load_dotenv

import jwt  # PyJWT — библиотека для создания и проверки JWT
from passlib.context import CryptContext  # удобная обёртка для bcrypt

load_dotenv()

# Секрет — ключ, которым подписываются токены.
# Если его украдут — смогут подделывать токены. Держим в .env и не коммитим.
JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME")

# Алгоритм подписи. HS256 = HMAC SHA-256 (симметричный ключ)
JWT_ALG = os.getenv("JWT_ALG", "HS256")

# Через сколько минут токен истекает
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# Настраиваем хэширование паролей.
# bcrypt — стандарт для паролей.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    # Возвращает bcrypt-хэш пароля (внутри соль + параметры сложности)
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    # Проверяет пароль: сравнивает введённый пароль с bcrypt-хэшем
    return pwd_context.verify(password, password_hash)


def create_access_token(sub: str) -> str:
    """
    Создаём JWT.
    sub — идентификатор пользователя (обычно user_id)
    """
    now = datetime.now(timezone.utc)  # текущее время UTC
    exp = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)  # время истечения

    # payload — "начинка" токена
    payload = {
        "sub": sub,                    # кому принадлежит токен
        "iat": int(now.timestamp()),   # issued at — когда выдан
        "exp": int(exp.timestamp()),   # expire — когда истекает
    }

    # jwt.encode подписывает payload секретом и возвращает строку токена
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    """
    Проверяет JWT:
    - подпись (JWT_SECRET)
    - срок действия exp
    Если всё ок — возвращает payload (dict).
    Если нет — кидает исключение.
    """
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
