import os  # работа с переменными окружения (DATABASE_URL)
from dotenv import load_dotenv  # читает .env файл и добавляет переменные в окружение
from psycopg_pool import ConnectionPool  # пул соединений для psycopg3 (PostgreSQL)

load_dotenv()  # загружаем переменные из .env в окружение процесса

DATABASE_URL = os.getenv("DATABASE_URL")  # берём строку подключения из окружения
if not DATABASE_URL:
    # Если переменной нет — дальше работать нельзя, сразу падаем с понятным сообщением
    raise RuntimeError("DATABASE_URL не задан в .env")


# Создаём пул соединений.
# Почему пул нужен:
# - каждый HTTP запрос может нуждаться в БД
# - создавать соединение каждый раз дорого
# - пул держит несколько соединений и переиспользует их
pool = ConnectionPool(
    conninfo=DATABASE_URL,  # строка подключения
    min_size=1,             # минимум 1 соединение всегда готово
    max_size=10             # максимум 10 (на старте хватает)
)


def fetch_one(query: str, params: tuple = ()):
    """
    Выполняет SELECT и возвращает ОДНУ строку (fetchone).
    Подходит для "получить пользователя", "получить время", "получить одну задачу" и т.п.
    """
    # pool.connection() — берём соединение из пула (и потом возвращаем обратно автоматически)
    with pool.connection() as conn:
        # cursor нужен для выполнения SQL
        with conn.cursor() as cur:
            cur.execute(query, params)   # выполняем запрос с параметрами
            row = cur.fetchone()         # берём одну строку
            return row                   # возвращаем кортеж или None


def fetch_all(query: str, params: tuple = ()):
    """
    Выполняет SELECT и возвращает ВСЕ строки (fetchall).
    Подходит для списков: задачи, чаты, сообщения.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            return rows


def execute(query: str, params: tuple = ()):
    """
    Выполняет запрос без возврата результата (INSERT/UPDATE/DELETE).
    Важно: здесь делаем conn.commit(), иначе изменения не сохранятся.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            conn.commit()  # фиксируем транзакцию
