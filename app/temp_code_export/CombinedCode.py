// ===== File: __init__.py =====


// ===== File: db.py =====
# import os  # работа с переменными окружения (DATABASE_URL)
# from dotenv import load_dotenv  # читает .env файл и добавляет переменные в окружение
# from psycopg_pool import ConnectionPool  # пул соединений для psycopg3 (PostgreSQL)
#
# load_dotenv()  # загружаем переменные из .env в окружение процесса
#
# DATABASE_URL = os.getenv("DATABASE_URL")  # берём строку подключения из окружения
# if not DATABASE_URL:
#     # Если переменной нет — дальше работать нельзя, сразу падаем с понятным сообщением
#     raise RuntimeError("DATABASE_URL не задан в .env")
#
#
# # Создаём пул соединений.
# # Почему пул нужен:
# # - каждый HTTP запрос может нуждаться в БД
# # - создавать соединение каждый раз дорого
# # - пул держит несколько соединений и переиспользует их
# pool = ConnectionPool(
#     conninfo=DATABASE_URL,  # строка подключения
#     min_size=1,             # минимум 1 соединение всегда готово
#     max_size=10             # максимум 10 (на старте хватает)
# )
#
#
# def fetch_one(query: str, params: tuple = ()):
#     """
#     Выполняет SELECT и возвращает ОДНУ строку (fetchone).
#     Подходит для "получить пользователя", "получить время", "получить одну задачу" и т.п.
#     """
#     # pool.connection() — берём соединение из пула (и потом возвращаем обратно автоматически)
#     with pool.connection() as conn:
#         # cursor нужен для выполнения SQL
#         with conn.cursor() as cur:
#             cur.execute(query, params)   # выполняем запрос с параметрами
#             row = cur.fetchone()         # берём одну строку
#             return row                   # возвращаем кортеж или None
#
#
# def fetch_all(query: str, params: tuple = ()):
#     """
#     Выполняет SELECT и возвращает ВСЕ строки (fetchall).
#     Подходит для списков: задачи, чаты, сообщения.
#     """
#     with pool.connection() as conn:
#         with conn.cursor() as cur:
#             cur.execute(query, params)
#             rows = cur.fetchall()
#             return rows
#
#
# def execute(query: str, params: tuple = ()):
#     """
#     Выполняет запрос без возврата результата (INSERT/UPDATE/DELETE).
#     Важно: здесь делаем conn.commit(), иначе изменения не сохранятся.
#     """
#     with pool.connection() as conn:
#         with conn.cursor() as cur:
#             cur.execute(query, params)
#             conn.commit()  # фиксируем транзакцию

import os
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("Не найдена переменная окружения DATABASE_URL")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=True)


def fetch_one(query: str, params: tuple = ()):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()


def execute(query: str, params: tuple = ()):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()
            return cur.rowcount


// ===== File: main.py =====
# import uuid  # для генерации UUID (id пользователя) прямо в бэкенде
#
# # FastAPI: основной класс приложения + типовые инструменты
# from fastapi import FastAPI, HTTPException, Depends
#
# # HTTPBearer — стандартный способ принять заголовок Authorization: Bearer <token>
# # HTTPAuthorizationCredentials — объект, в котором хранится токен
# from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
#
# # CORS нужен, чтобы iOS/браузер могли делать запросы на сервер без блокировки
# from fastapi.middleware.cors import CORSMiddleware
#
# from app import db  # наш модуль для работы с БД (fetch_one/fetch_all/execute)
# from app.schemas import RegisterIn, LoginIn, TokenOut, UserOut  # pydantic-схемы входа/выхода
# from app.security import hash_password, decode_token  # пароль + JWT
# from app.security import verify_and_update_password, create_access_token
#
#
# # Создаём FastAPI приложение.
# # title отображается в /docs (Swagger UI)
# app = FastAPI(title="iCuno Backend (MVP)")
#
#
# # Подключаем middleware CORS.
# # Это важно если запросы приходят не "с этого же домена" (на iOS, в браузере, с другого порта и т.д.)
# # Пока allow_origins=["*"] — разрешаем всем.
# # В проде так нельзя: нужно указать конкретные домены/адреса приложения.
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],          # разрешить запросы от любых источников (на старте удобно)
#     allow_credentials=True,       # разрешить отправку cookies/authorization заголовков
#     allow_methods=["*"],          # разрешить любые HTTP методы (GET/POST/PUT/DELETE)
#     allow_headers=["*"],          # разрешить любые заголовки (включая Authorization)
# )
#
# # Создаём "схему авторизации" Bearer.
# # Это НЕ токен, это инструмент FastAPI:
# # он автоматически:
# # 1) вытащит Authorization заголовок,
# # 2) проверит формат "Bearer ....",
# # 3) передаст креды в функцию через Depends()
# auth_scheme = HTTPBearer()
#
#
# # ----------------- ТЕСТОВЫЕ ЭНДПОИНТЫ -----------------
#
# # @app.get(...) — декоратор: говорит FastAPI "эта функция обслуживает GET /health"
# @app.get("/health")
# def health():
#     # Просто отдаём JSON, чтобы проверить что сервер жив
#     return {"status": "ok"}
#
#
# @app.get("/db/ping")
# def db_ping():
#     # Проверяем, что БД доступна:
#     # SELECT now() возвращает текущее время на стороне PostgreSQL
#     row = db.fetch_one("SELECT now()")
#     # row — это кортеж (например (datetime,))
#     return {"db_time": str(row[0])}
#
#
# # @app.get("/db/postgis")
# # def db_postgis():
# #     # Проверяем PostGIS.
# #     # Если расширение не установлено — здесь будет ошибка (это норм для диагностики)
# #     row = db.fetch_one("SELECT PostGIS_Version()")
# #     return {"postgis_version": row[0]}
#
# @app.get("/db/postgis")
# def db_postgis():
#     row = db.fetch_one("""
#         SELECT e.extversion, n.nspname
#         FROM pg_extension e
#         JOIN pg_namespace n ON n.oid = e.extnamespace
#         WHERE e.extname='postgis'
#     """)
#     if not row:
#         raise HTTPException(status_code=500, detail="PostGIS не установлен. Выполни: CREATE EXTENSION postgis;")
#     return {"postgis_version": row[0], "schema": row[1]}
#
#
# # ----------------- JWT DEPENDENCY (ключевая часть) -----------------
#
# # Это "зависимость" (dependency).
# # Она будет вызываться автоматически, если ты в Depends() укажешь get_current_user.
# #
# # Что делает:
# # 1) достаёт JWT из Authorization: Bearer <token>
# # 2) декодирует и проверяет токен (exp, подпись)
# # 3) вытаскивает user_id из payload['sub']
# # 4) идёт в БД и получает пользователя
# # 5) возвращает объект UserOut (id, email, role)
# #
# # Если что-то не так — кидает HTTPException(401) и запрос не продолжается.
# def get_current_user(
#     creds: HTTPAuthorizationCredentials = Depends(auth_scheme)  # автоматически извлекаем Bearer-токен
# ) -> UserOut:
#     token = creds.credentials  # строка токена (без слова Bearer)
#
#     try:
#         # decode_token проверяет подпись и срок действия (exp)
#         payload = decode_token(token)
#
#         # sub = "subject" — обычно там кладут id пользователя
#         user_id = payload.get("sub")
#
#         # Если sub отсутствует — токен некорректный
#         if not user_id:
#             raise HTTPException(status_code=401, detail="Некорректный токен (нет sub)")
#
#     except Exception:
#         # Любая ошибка декодирования/проверки exp — считаем токен невалидным
#         raise HTTPException(status_code=401, detail="Невалидный или просроченный токен")
#
#     # Если токен валиден — проверяем, что пользователь реально существует в БД
#     row = db.fetch_one(
#         # id::text — приводим UUID к строке, чтобы удобно отдавать в JSON
#         "SELECT id::text, email, role FROM users WHERE id = %s AND deleted_at IS NULL",
#         (user_id,),  # params передаём кортежем, чтобы не было SQL-инъекций
#     )
#
#     if not row:
#         # Токен может быть "валидный", но пользователь уже удалён/заблокирован
#         raise HTTPException(status_code=401, detail="Пользователь не найден")
#
#     # Собираем pydantic-модель ответа (или внутреннего использования)
#     return UserOut(id=row[0], email=row[1], role=row[2])
#
#
# # ----------------- AUTH -----------------
#
# # response_model=UserOut означает:
# # 1) Swagger покажет ответ как UserOut
# # 2) FastAPI приведёт возвращаемые данные к UserOut (валидирует)
# @app.post("/auth/register", response_model=UserOut)
# def register(data: RegisterIn):
#     # Проверяем, есть ли уже пользователь с таким email
#     existing = db.fetch_one("SELECT id FROM users WHERE email = %s", (data.email,))
#     if existing:
#         # 409 Conflict — логично: конфликт данных (email уже занят)
#         raise HTTPException(status_code=409, detail="Email уже зарегистрирован")
#
#     # Генерируем UUID пользователя.
#     # Важно: если в БД id генерируется автоматически — тут будет по-другому.
#     user_id = str(uuid.uuid4())
#
#     # Хэшируем пароль (bcrypt).
#     # Никогда не храним пароль в чистом виде.
#     password_hash = hash_password(data.password)
#
#     # Пишем пользователя в БД.
#     # %s — плейсхолдеры psycopg, params передаются отдельно (защита от SQL-инъекций)
#     db.execute(
#         """
#         INSERT INTO users (id, role, email, password_hash, is_email_verified, is_phone_verified)
#         VALUES (%s, %s, %s, %s, false, false)
#         """,
#         (user_id, "user", data.email, password_hash),
#     )
#
#     # Отдаём данные нового пользователя (без пароля)
#     return UserOut(id=user_id, email=data.email, role="user")
#
#
# # @app.post("/auth/login", response_model=TokenOut)
# # def login(data: LoginIn):
# #     # Ищем пользователя по email, берём hash пароля
# #     row = db.fetch_one(
# #         "SELECT id::text, password_hash FROM users WHERE email = %s AND deleted_at IS NULL",
# #         (data.email,),
# #     )
# #     if not row:
# #         # Не говорим "email не существует", чтобы не давать атакующим информацию
# #         raise HTTPException(status_code=401, detail="Неверный email или пароль")
# #
# #     user_id, password_hash_db = row[0], row[1]
# #
# #     # Проверяем пароль: сравниваем введённый пароль с хэшом из БД
# #     if not verify_password(data.password, password_hash_db):
# #         raise HTTPException(status_code=401, detail="Неверный email или пароль")
# #
# #     # Создаём JWT (в sub кладём user_id)
# #     token = create_access_token(sub=user_id)
# #
# #     # Возвращаем токен по стандарту "bearer"
# #     return TokenOut(access_token=token)
#
# @app.post("/auth/login", response_model=TokenOut)
# def login(data: LoginIn):
#     row = db.fetch_one(
#         "SELECT id::text, password_hash FROM users WHERE email = %s AND deleted_at IS NULL",
#         (data.email,),
#     )
#     if not row:
#         raise HTTPException(status_code=401, detail="Неверный email или пароль")
#
#     user_id, password_hash_db = row[0], row[1]
#
#     ok, new_hash = verify_and_update_password(data.password, password_hash_db)
#     if not ok:
#         raise HTTPException(status_code=401, detail="Неверный email или пароль")
#
#     # Если хэш устарел (например bcrypt) — заменяем на argon2id
#     if new_hash:
#         db.execute(
#             "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
#             (new_hash, user_id),
#         )
#
#     token = create_access_token(sub=user_id)
#     return TokenOut(access_token=token)
#
#
# # /me — пример защищённого эндпоинта
# # user получает результат get_current_user автоматически
# @app.get("/me", response_model=UserOut)
# def me(user: UserOut = Depends(get_current_user)):
#     return user
#
#
# # ----------------- DEBUG -----------------
#
# @app.get("/debug/tasks")
# def debug_tasks(user: UserOut = Depends(get_current_user)):
#     # Просто проверяем, что доступ к tasks работает
#     rows = db.fetch_all(
#         "SELECT id::text, title FROM tasks ORDER BY created_at DESC LIMIT 5"
#     )
#
#     # Преобразуем rows -> список словарей
#     return {"items": [{"id": r[0], "title": r[1]} for r in rows]}


import uuid
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.schemas import RegisterIn, LoginIn, TokenOut, UserOut
from app.security import hash_password, verify_and_update_password, create_access_token, decode_token

app = FastAPI(title="iCuno Backend (MVP)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # потом ограничишь
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

auth_scheme = HTTPBearer()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/register", response_model=TokenOut, status_code=201)
def register(data: RegisterIn):
    row = db.fetch_one(
        "SELECT id FROM users WHERE email = %s AND deleted_at IS NULL",
        (data.email,),
    )
    if row:
        raise HTTPException(status_code=409, detail="User already exists")

    user_id = uuid.uuid4()
    password_hash = hash_password(data.password)

    # Минимальный INSERT. Если у тебя в таблице есть DEFAULT на created_at — всё ок.
    db.execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES (%s, %s, %s, %s)",
        (user_id, data.email, password_hash, "user"),
    )

    token = create_access_token(str(user_id))
    return TokenOut(access_token=token)


@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn):
    row = db.fetch_one(
        "SELECT id, password_hash FROM users WHERE email = %s AND deleted_at IS NULL",
        (data.email,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id, password_hash = row

    ok, new_hash = verify_and_update_password(data.password, password_hash)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Если Passlib решил обновить хеш — сохраним
    if new_hash:
        db.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (new_hash, user_id),
        )

    token = create_access_token(str(user_id))
    return TokenOut(access_token=token)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(auth_scheme)) -> UserOut:
    token = creds.credentials
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    row = db.fetch_one(
        "SELECT id::text, email, role FROM users WHERE id = %s AND deleted_at IS NULL",
        (user_id,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return UserOut(id=row[0], email=row[1], role=row[2])


@app.get("/me", response_model=UserOut)
def me(user: UserOut = Depends(get_current_user)):
    return user

// ===== File: schemas.py =====
# from pydantic import BaseModel, EmailStr, Field  # Pydantic: валидация данных
#
#
# # Входные данные для /auth/register
# class RegisterIn(BaseModel):
#     email: EmailStr                 # EmailStr валидирует формат почты
#     password: str = Field(min_length=6)  # пароль минимум 6 символов
#     name: str | None = None         # опциональное имя (может быть None)
#
#
# # Входные данные для /auth/login
# class LoginIn(BaseModel):
#     email: EmailStr
#     password: str
#
#
# # Ответ /auth/login
# class TokenOut(BaseModel):
#     access_token: str               # сам JWT
#     token_type: str = "bearer"      # стандарт: "bearer"
#
#
# # Как мы отдаём пользователя наружу (без password_hash!)
# class UserOut(BaseModel):
#     id: str
#     email: EmailStr
#     role: str

from pydantic import BaseModel, EmailStr, Field


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=72)


class TokenOut(BaseModel):
    access_token: str


class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str


// ===== File: security.py =====
# import os
# from datetime import datetime, timedelta, timezone  # время для exp/iat в JWT
# from dotenv import load_dotenv
#
# import jwt  # PyJWT — библиотека для создания и проверки JWT
# from passlib.context import CryptContext  # удобная обёртка для bcrypt
#
# load_dotenv()
#
# # Секрет — ключ, которым подписываются токены.
# # Если его украдут — смогут подделывать токены. Держим в .env и не коммитим.
# JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME")
#
# # Алгоритм подписи. HS256 = HMAC SHA-256 (симметричный ключ)
# JWT_ALG = os.getenv("JWT_ALG", "HS256")
#
# # Через сколько минут токен истекает
# ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
#
# # Настраиваем хэширование паролей.
# # bcrypt — стандарт для паролей.
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
#
#
# def hash_password(password: str) -> str:
#     # Возвращает bcrypt-хэш пароля (внутри соль + параметры сложности)
#     return pwd_context.hash(password)
#
#
# def verify_password(password: str, password_hash: str) -> bool:
#     # Проверяет пароль: сравнивает введённый пароль с bcrypt-хэшем
#     return pwd_context.verify(password, password_hash)
#
#
# def create_access_token(sub: str) -> str:
#     """
#     Создаём JWT.
#     sub — идентификатор пользователя (обычно user_id)
#     """
#     now = datetime.now(timezone.utc)  # текущее время UTC
#     exp = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)  # время истечения
#
#     # payload — "начинка" токена
#     payload = {
#         "sub": sub,                    # кому принадлежит токен
#         "iat": int(now.timestamp()),   # issued at — когда выдан
#         "exp": int(exp.timestamp()),   # expire — когда истекает
#     }
#
#     # jwt.encode подписывает payload секретом и возвращает строку токена
#     return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
#
#
# def decode_token(token: str) -> dict:
#     """
#     Проверяет JWT:
#     - подпись (JWT_SECRET)
#     - срок действия exp
#     Если всё ок — возвращает payload (dict).
#     Если нет — кидает исключение.
#     """
#     return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])

# app/security.py
# import os
# from datetime import datetime, timedelta, timezone
# from dotenv import load_dotenv
#
# import jwt
# from passlib.context import CryptContext
#
# load_dotenv()
#
# JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME")
# JWT_ALG = os.getenv("JWT_ALG", "HS256")
# ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
#
# pwd_context = CryptContext(
#     schemes=["argon2", "bcrypt"],   # bcrypt оставляем для старых хэшей
#     deprecated="auto",
#
#     # ЯВНО argon2id:
#     argon2__type="ID",              # Argon2id :contentReference[oaicite:4]{index=4}
#
#     # параметры (нормально для старта; потом можно усилить)
#     argon2__time_cost=2,
#     argon2__memory_cost=65536,      # 64 MB
#     argon2__parallelism=2,
# )
#
# def hash_password(password: str) -> str:
#     return pwd_context.hash(password)
#
# def verify_and_update_password(password: str, stored_hash: str) -> tuple[bool, str | None]:
#     # вернёт (ok, new_hash_if_need_update)
#     return pwd_context.verify_and_update(password, stored_hash)  # :contentReference[oaicite:5]{index=5}
#
# def create_access_token(sub: str) -> str:
#     now = datetime.now(timezone.utc)
#     exp = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
#     payload = {"sub": sub, "iat": int(now.timestamp()), "exp": int(exp.timestamp())}
#     return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
#
# def decode_token(token: str) -> dict:
#     return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])

import os
import datetime as dt
import jwt
from passlib.context import CryptContext

# Аргон2id (argon2__type="ID") — современный безопасный вариант
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__type="ID",
    # параметры можно менять, но эти норм для MVP
    argon2__time_cost=2,
    argon2__memory_cost=102400,
    argon2__parallelism=8,
)

JWT_SECRET = os.environ.get("JWT_SECRET", "CHANGE_ME_PLEASE")
JWT_ALG = "HS256"
ACCESS_TOKEN_TTL_DAYS = 14


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_and_update_password(password: str, password_hash: str):
    """
    Возвращает (ok, new_hash).
    new_hash может быть None, если апгрейд не нужен.
    """
    return pwd_context.verify_and_update(password, password_hash)


def create_access_token(user_id: str) -> str:
    exp = dt.datetime.utcnow() + dt.timedelta(days=ACCESS_TOKEN_TTL_DAYS)
    payload = {"sub": user_id, "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])




// ===== File: temp_code_export/CombinedCode.py =====
// ===== File: __init__.py =====


// ===== File: db.py =====
# import os  # работа с переменными окружения (DATABASE_URL)
# from dotenv import load_dotenv  # читает .env файл и добавляет переменные в окружение
# from psycopg_pool import ConnectionPool  # пул соединений для psycopg3 (PostgreSQL)
#
# load_dotenv()  # загружаем переменные из .env в окружение процесса
#
# DATABASE_URL = os.getenv("DATABASE_URL")  # берём строку подключения из окружения
# if not DATABASE_URL:
#     # Если переменной нет — дальше работать нельзя, сразу падаем с понятным сообщением
#     raise RuntimeError("DATABASE_URL не задан в .env")
#
#
# # Создаём пул соединений.
# # Почему пул нужен:
# # - каждый HTTP запрос может нуждаться в БД
# # - создавать соединение каждый раз дорого
# # - пул держит несколько соединений и переиспользует их
# pool = ConnectionPool(
#     conninfo=DATABASE_URL,  # строка подключения
#     min_size=1,             # минимум 1 соединение всегда готово
#     max_size=10             # максимум 10 (на старте хватает)
# )
#
#
# def fetch_one(query: str, params: tuple = ()):
#     """
#     Выполняет SELECT и возвращает ОДНУ строку (fetchone).
#     Подходит для "получить пользователя", "получить время", "получить одну задачу" и т.п.
#     """
#     # pool.connection() — берём соединение из пула (и потом возвращаем обратно автоматически)
#     with pool.connection() as conn:
#         # cursor нужен для выполнения SQL
#         with conn.cursor() as cur:
#             cur.execute(query, params)   # выполняем запрос с параметрами
#             row = cur.fetchone()         # берём одну строку
#             return row                   # возвращаем кортеж или None
#
#
# def fetch_all(query: str, params: tuple = ()):
#     """
#     Выполняет SELECT и возвращает ВСЕ строки (fetchall).
#     Подходит для списков: задачи, чаты, сообщения.
#     """
#     with pool.connection() as conn:
#         with conn.cursor() as cur:
#             cur.execute(query, params)
#             rows = cur.fetchall()
#             return rows
#
#
# def execute(query: str, params: tuple = ()):
#     """
#     Выполняет запрос без возврата результата (INSERT/UPDATE/DELETE).
#     Важно: здесь делаем conn.commit(), иначе изменения не сохранятся.
#     """
#     with pool.connection() as conn:
#         with conn.cursor() as cur:
#             cur.execute(query, params)
#             conn.commit()  # фиксируем транзакцию

import os
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("Не найдена переменная окружения DATABASE_URL")

pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=True)


def fetch_one(query: str, params: tuple = ()):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()


def execute(query: str, params: tuple = ()):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()
            return cur.rowcount


// ===== File: main.py =====
# import uuid  # для генерации UUID (id пользователя) прямо в бэкенде
#
# # FastAPI: основной класс приложения + типовые инструменты
# from fastapi import FastAPI, HTTPException, Depends
#
# # HTTPBearer — стандартный способ принять заголовок Authorization: Bearer <token>
# # HTTPAuthorizationCredentials — объект, в котором хранится токен
# from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
#
# # CORS нужен, чтобы iOS/браузер могли делать запросы на сервер без блокировки
# from fastapi.middleware.cors import CORSMiddleware
#
# from app import db  # наш модуль для работы с БД (fetch_one/fetch_all/execute)
# from app.schemas import RegisterIn, LoginIn, TokenOut, UserOut  # pydantic-схемы входа/выхода
# from app.security import hash_password, decode_token  # пароль + JWT
# from app.security import verify_and_update_password, create_access_token
#
#
# # Создаём FastAPI приложение.
# # title отображается в /docs (Swagger UI)
# app = FastAPI(title="iCuno Backend (MVP)")
#
#
# # Подключаем middleware CORS.
# # Это важно если запросы приходят не "с этого же домена" (на iOS, в браузере, с другого порта и т.д.)
# # Пока allow_origins=["*"] — разрешаем всем.
# # В проде так нельзя: нужно указать конкретные домены/адреса приложения.
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],          # разрешить запросы от любых источников (на старте удобно)
#     allow_credentials=True,       # разрешить отправку cookies/authorization заголовков
#     allow_methods=["*"],          # разрешить любые HTTP методы (GET/POST/PUT/DELETE)
#     allow_headers=["*"],          # разрешить любые заголовки (включая Authorization)
# )
#
# # Создаём "схему авторизации" Bearer.
# # Это НЕ токен, это инструмент FastAPI:
# # он автоматически:
# # 1) вытащит Authorization заголовок,
# # 2) проверит формат "Bearer ....",
# # 3) передаст креды в функцию через Depends()
# auth_scheme = HTTPBearer()
#
#
# # ----------------- ТЕСТОВЫЕ ЭНДПОИНТЫ -----------------
#
# # @app.get(...) — декоратор: говорит FastAPI "эта функция обслуживает GET /health"
# @app.get("/health")
# def health():
#     # Просто отдаём JSON, чтобы проверить что сервер жив
#     return {"status": "ok"}
#
#
# @app.get("/db/ping")
# def db_ping():
#     # Проверяем, что БД доступна:
#     # SELECT now() возвращает текущее время на стороне PostgreSQL
#     row = db.fetch_one("SELECT now()")
#     # row — это кортеж (например (datetime,))
#     return {"db_time": str(row[0])}
#
#
# # @app.get("/db/postgis")
# # def db_postgis():
# #     # Проверяем PostGIS.
# #     # Если расширение не установлено — здесь будет ошибка (это норм для диагностики)
# #     row = db.fetch_one("SELECT PostGIS_Version()")
# #     return {"postgis_version": row[0]}
#
# @app.get("/db/postgis")
# def db_postgis():
#     row = db.fetch_one("""
#         SELECT e.extversion, n.nspname
#         FROM pg_extension e
#         JOIN pg_namespace n ON n.oid = e.extnamespace
#         WHERE e.extname='postgis'
#     """)
#     if not row:
#         raise HTTPException(status_code=500, detail="PostGIS не установлен. Выполни: CREATE EXTENSION postgis;")
#     return {"postgis_version": row[0], "schema": row[1]}
#
#
# # ----------------- JWT DEPENDENCY (ключевая часть) -----------------
#
# # Это "зависимость" (dependency).
# # Она будет вызываться автоматически, если ты в Depends() укажешь get_current_user.
# #
# # Что делает:
# # 1) достаёт JWT из Authorization: Bearer <token>
# # 2) декодирует и проверяет токен (exp, подпись)
# # 3) вытаскивает user_id из payload['sub']
# # 4) идёт в БД и получает пользователя
# # 5) возвращает объект UserOut (id, email, role)
# #
# # Если что-то не так — кидает HTTPException(401) и запрос не продолжается.
# def get_current_user(
#     creds: HTTPAuthorizationCredentials = Depends(auth_scheme)  # автоматически извлекаем Bearer-токен
# ) -> UserOut:
#     token = creds.credentials  # строка токена (без слова Bearer)
#
#     try:
#         # decode_token проверяет подпись и срок действия (exp)
#         payload = decode_token(token)
#
#         # sub = "subject" — обычно там кладут id пользователя
#         user_id = payload.get("sub")
#
#         # Если sub отсутствует — токен некорректный
#         if not user_id:
#             raise HTTPException(status_code=401, detail="Некорректный токен (нет sub)")
#
#     except Exception:
#         # Любая ошибка декодирования/проверки exp — считаем токен невалидным
#         raise HTTPException(status_code=401, detail="Невалидный или просроченный токен")
#
#     # Если токен валиден — проверяем, что пользователь реально существует в БД
#     row = db.fetch_one(
#         # id::text — приводим UUID к строке, чтобы удобно отдавать в JSON
#         "SELECT id::text, email, role FROM users WHERE id = %s AND deleted_at IS NULL",
#         (user_id,),  # params передаём кортежем, чтобы не было SQL-инъекций
#     )
#
#     if not row:
#         # Токен может быть "валидный", но пользователь уже удалён/заблокирован
#         raise HTTPException(status_code=401, detail="Пользователь не найден")
#
#     # Собираем pydantic-модель ответа (или внутреннего использования)
#     return UserOut(id=row[0], email=row[1], role=row[2])
#
#
# # ----------------- AUTH -----------------
#
# # response_model=UserOut означает:
# # 1) Swagger покажет ответ как UserOut
# # 2) FastAPI приведёт возвращаемые данные к UserOut (валидирует)
# @app.post("/auth/register", response_model=UserOut)
# def register(data: RegisterIn):
#     # Проверяем, есть ли уже пользователь с таким email
#     existing = db.fetch_one("SELECT id FROM users WHERE email = %s", (data.email,))
#     if existing:
#         # 409 Conflict — логично: конфликт данных (email уже занят)
#         raise HTTPException(status_code=409, detail="Email уже зарегистрирован")
#
#     # Генерируем UUID пользователя.
#     # Важно: если в БД id генерируется автоматически — тут будет по-другому.
#     user_id = str(uuid.uuid4())
#
#     # Хэшируем пароль (bcrypt).
#     # Никогда не храним пароль в чистом виде.
#     password_hash = hash_password(data.password)
#
#     # Пишем пользователя в БД.
#     # %s — плейсхолдеры psycopg, params передаются отдельно (защита от SQL-инъекций)
#     db.execute(
#         """
#         INSERT INTO users (id, role, email, password_hash, is_email_verified, is_phone_verified)
#         VALUES (%s, %s, %s, %s, false, false)
#         """,
#         (user_id, "user", data.email, password_hash),
#     )
#
#     # Отдаём данные нового пользователя (без пароля)
#     return UserOut(id=user_id, email=data.email, role="user")
#
#
# # @app.post("/auth/login", response_model=TokenOut)
# # def login(data: LoginIn):
# #     # Ищем пользователя по email, берём hash пароля
# #     row = db.fetch_one(
# #         "SELECT id::text, password_hash FROM users WHERE email = %s AND deleted_at IS NULL",
# #         (data.email,),
# #     )
# #     if not row:
# #         # Не говорим "email не существует", чтобы не давать атакующим информацию
# #         raise HTTPException(status_code=401, detail="Неверный email или пароль")
# #
# #     user_id, password_hash_db = row[0], row[1]
# #
# #     # Проверяем пароль: сравниваем введённый пароль с хэшом из БД
# #     if not verify_password(data.password, password_hash_db):
# #         raise HTTPException(status_code=401, detail="Неверный email или пароль")
# #
# #     # Создаём JWT (в sub кладём user_id)
# #     token = create_access_token(sub=user_id)
# #
# #     # Возвращаем токен по стандарту "bearer"
# #     return TokenOut(access_token=token)
#
# @app.post("/auth/login", response_model=TokenOut)
# def login(data: LoginIn):
#     row = db.fetch_one(
#         "SELECT id::text, password_hash FROM users WHERE email = %s AND deleted_at IS NULL",
#         (data.email,),
#     )
#     if not row:
#         raise HTTPException(status_code=401, detail="Неверный email или пароль")
#
#     user_id, password_hash_db = row[0], row[1]
#
#     ok, new_hash = verify_and_update_password(data.password, password_hash_db)
#     if not ok:
#         raise HTTPException(status_code=401, detail="Неверный email или пароль")
#
#     # Если хэш устарел (например bcrypt) — заменяем на argon2id
#     if new_hash:
#         db.execute(
#             "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
#             (new_hash, user_id),
#         )
#
#     token = create_access_token(sub=user_id)
#     return TokenOut(access_token=token)
#
#
# # /me — пример защищённого эндпоинта
# # user получает результат get_current_user автоматически
# @app.get("/me", response_model=UserOut)
# def me(user: UserOut = Depends(get_current_user)):
#     return user
#
#
# # ----------------- DEBUG -----------------
#
# @app.get("/debug/tasks")
# def debug_tasks(user: UserOut = Depends(get_current_user)):
#     # Просто проверяем, что доступ к tasks работает
#     rows = db.fetch_all(
#         "SELECT id::text, title FROM tasks ORDER BY created_at DESC LIMIT 5"
#     )
#
#     # Преобразуем rows -> список словарей
#     return {"items": [{"id": r[0], "title": r[1]} for r in rows]}


import uuid
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.schemas import RegisterIn, LoginIn, TokenOut, UserOut
from app.security import hash_password, verify_and_update_password, create_access_token, decode_token

app = FastAPI(title="iCuno Backend (MVP)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # потом ограничишь
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

auth_scheme = HTTPBearer()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/register", response_model=TokenOut, status_code=201)
def register(data: RegisterIn):
    row = db.fetch_one(
        "SELECT id FROM users WHERE email = %s AND deleted_at IS NULL",
        (data.email,),
    )
    if row:
        raise HTTPException(status_code=409, detail="User already exists")

    user_id = uuid.uuid4()
    password_hash = hash_password(data.password)

    # Минимальный INSERT. Если у тебя в таблице есть DEFAULT на created_at — всё ок.
    db.execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES (%s, %s, %s, %s)",
        (user_id, data.email, password_hash, "user"),
    )

    token = create_access_token(str(user_id))
    return TokenOut(access_token=token)


@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn):
    row = db.fetch_one(
        "SELECT id, password_hash FROM users WHERE email = %s AND deleted_at IS NULL",
        (data.email,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id, password_hash = row

    ok, new_hash = verify_and_update_password(data.password, password_hash)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Если Passlib решил обновить хеш — сохраним
    if new_hash:
        db.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (new_hash, user_id),
        )

    token = create_access_token(str(user_id))
    return TokenOut(access_token=token)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(auth_scheme)) -> UserOut:
    token = creds.credentials
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    row = db.fetch_one(
        "SELECT id::text, email, role FROM users WHERE id = %s AND deleted_at IS NULL",
        (user_id,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return UserOut(id=row[0], email=row[1], role=row[2])


@app.get("/me", response_model=UserOut)
def me(user: UserOut = Depends(get_current_user)):
    return user

