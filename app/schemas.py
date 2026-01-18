from pydantic import BaseModel, EmailStr, Field  # Pydantic: валидация данных


# Входные данные для /auth/register
class RegisterIn(BaseModel):
    email: EmailStr                 # EmailStr валидирует формат почты
    password: str = Field(min_length=6)  # пароль минимум 6 символов
    name: str | None = None         # опциональное имя (может быть None)


# Входные данные для /auth/login
class LoginIn(BaseModel):
    email: EmailStr
    password: str


# Ответ /auth/login
class TokenOut(BaseModel):
    access_token: str               # сам JWT
    token_type: str = "bearer"      # стандарт: "bearer"


# Как мы отдаём пользователя наружу (без password_hash!)
class UserOut(BaseModel):
    id: str
    email: EmailStr
    role: str
