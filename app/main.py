import uuid  # для генерации UUID (id пользователя) прямо в бэкенде

# FastAPI: основной класс приложения + типовые инструменты
from fastapi import FastAPI, HTTPException, Depends

# HTTPBearer — стандартный способ принять заголовок Authorization: Bearer <token>
# HTTPAuthorizationCredentials — объект, в котором хранится токен
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# CORS нужен, чтобы iOS/браузер могли делать запросы на сервер без блокировки
from fastapi.middleware.cors import CORSMiddleware

from app import db  # наш модуль для работы с БД (fetch_one/fetch_all/execute)
from app.schemas import RegisterIn, LoginIn, TokenOut, UserOut  # pydantic-схемы входа/выхода
from app.security import hash_password, verify_password, create_access_token, decode_token  # пароль + JWT


# Создаём FastAPI приложение.
# title отображается в /docs (Swagger UI)
app = FastAPI(title="iCuno Backend (MVP)")


# Подключаем middleware CORS.
# Это важно если запросы приходят не "с этого же домена" (на iOS, в браузере, с другого порта и т.д.)
# Пока allow_origins=["*"] — разрешаем всем.
# В проде так нельзя: нужно указать конкретные домены/адреса приложения.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # разрешить запросы от любых источников (на старте удобно)
    allow_credentials=True,       # разрешить отправку cookies/authorization заголовков
    allow_methods=["*"],          # разрешить любые HTTP методы (GET/POST/PUT/DELETE)
    allow_headers=["*"],          # разрешить любые заголовки (включая Authorization)
)

# Создаём "схему авторизации" Bearer.
# Это НЕ токен, это инструмент FastAPI:
# он автоматически:
# 1) вытащит Authorization заголовок,
# 2) проверит формат "Bearer ....",
# 3) передаст креды в функцию через Depends()
auth_scheme = HTTPBearer()


# ----------------- ТЕСТОВЫЕ ЭНДПОИНТЫ -----------------

# @app.get(...) — декоратор: говорит FastAPI "эта функция обслуживает GET /health"
@app.get("/health")
def health():
    # Просто отдаём JSON, чтобы проверить что сервер жив
    return {"status": "ok"}


@app.get("/db/ping")
def db_ping():
    # Проверяем, что БД доступна:
    # SELECT now() возвращает текущее время на стороне PostgreSQL
    row = db.fetch_one("SELECT now()")
    # row — это кортеж (например (datetime,))
    return {"db_time": str(row[0])}


@app.get("/db/postgis")
def db_postgis():
    # Проверяем PostGIS.
    # Если расширение не установлено — здесь будет ошибка (это норм для диагностики)
    row = db.fetch_one("SELECT PostGIS_Version()")
    return {"postgis_version": row[0]}


# ----------------- JWT DEPENDENCY (ключевая часть) -----------------

# Это "зависимость" (dependency).
# Она будет вызываться автоматически, если ты в Depends() укажешь get_current_user.
#
# Что делает:
# 1) достаёт JWT из Authorization: Bearer <token>
# 2) декодирует и проверяет токен (exp, подпись)
# 3) вытаскивает user_id из payload['sub']
# 4) идёт в БД и получает пользователя
# 5) возвращает объект UserOut (id, email, role)
#
# Если что-то не так — кидает HTTPException(401) и запрос не продолжается.
def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(auth_scheme)  # автоматически извлекаем Bearer-токен
) -> UserOut:
    token = creds.credentials  # строка токена (без слова Bearer)

    try:
        # decode_token проверяет подпись и срок действия (exp)
        payload = decode_token(token)

        # sub = "subject" — обычно там кладут id пользователя
        user_id = payload.get("sub")

        # Если sub отсутствует — токен некорректный
        if not user_id:
            raise HTTPException(status_code=401, detail="Некорректный токен (нет sub)")

    except Exception:
        # Любая ошибка декодирования/проверки exp — считаем токен невалидным
        raise HTTPException(status_code=401, detail="Невалидный или просроченный токен")

    # Если токен валиден — проверяем, что пользователь реально существует в БД
    row = db.fetch_one(
        # id::text — приводим UUID к строке, чтобы удобно отдавать в JSON
        "SELECT id::text, email, role FROM users WHERE id = %s AND deleted_at IS NULL",
        (user_id,),  # params передаём кортежем, чтобы не было SQL-инъекций
    )

    if not row:
        # Токен может быть "валидный", но пользователь уже удалён/заблокирован
        raise HTTPException(status_code=401, detail="Пользователь не найден")

    # Собираем pydantic-модель ответа (или внутреннего использования)
    return UserOut(id=row[0], email=row[1], role=row[2])


# ----------------- AUTH -----------------

# response_model=UserOut означает:
# 1) Swagger покажет ответ как UserOut
# 2) FastAPI приведёт возвращаемые данные к UserOut (валидирует)
@app.post("/auth/register", response_model=UserOut)
def register(data: RegisterIn):
    # Проверяем, есть ли уже пользователь с таким email
    existing = db.fetch_one("SELECT id FROM users WHERE email = %s", (data.email,))
    if existing:
        # 409 Conflict — логично: конфликт данных (email уже занят)
        raise HTTPException(status_code=409, detail="Email уже зарегистрирован")

    # Генерируем UUID пользователя.
    # Важно: если в БД id генерируется автоматически — тут будет по-другому.
    user_id = str(uuid.uuid4())

    # Хэшируем пароль (bcrypt).
    # Никогда не храним пароль в чистом виде.
    password_hash = hash_password(data.password)

    # Пишем пользователя в БД.
    # %s — плейсхолдеры psycopg, params передаются отдельно (защита от SQL-инъекций)
    db.execute(
        """
        INSERT INTO users (id, role, email, password_hash, is_email_verified, is_phone_verified)
        VALUES (%s, %s, %s, %s, false, false)
        """,
        (user_id, "user", data.email, password_hash),
    )

    # Отдаём данные нового пользователя (без пароля)
    return UserOut(id=user_id, email=data.email, role="user")


@app.post("/auth/login", response_model=TokenOut)
def login(data: LoginIn):
    # Ищем пользователя по email, берём hash пароля
    row = db.fetch_one(
        "SELECT id::text, password_hash FROM users WHERE email = %s AND deleted_at IS NULL",
        (data.email,),
    )
    if not row:
        # Не говорим "email не существует", чтобы не давать атакующим информацию
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    user_id, password_hash_db = row[0], row[1]

    # Проверяем пароль: сравниваем введённый пароль с хэшом из БД
    if not verify_password(data.password, password_hash_db):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    # Создаём JWT (в sub кладём user_id)
    token = create_access_token(sub=user_id)

    # Возвращаем токен по стандарту "bearer"
    return TokenOut(access_token=token)


# /me — пример защищённого эндпоинта
# user получает результат get_current_user автоматически
@app.get("/me", response_model=UserOut)
def me(user: UserOut = Depends(get_current_user)):
    return user


# ----------------- DEBUG -----------------

@app.get("/debug/tasks")
def debug_tasks(user: UserOut = Depends(get_current_user)):
    # Просто проверяем, что доступ к tasks работает
    rows = db.fetch_all(
        "SELECT id::text, title FROM tasks ORDER BY created_at DESC LIMIT 5"
    )

    # Преобразуем rows -> список словарей
    return {"items": [{"id": r[0], "title": r[1]} for r in rows]}
