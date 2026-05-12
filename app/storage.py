from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.config import app_env, get_env, get_int


class Storage(Protocol):
    def put(self, key: str, content: bytes, *, content_type: str | None = None) -> str: ...
    def get_url(self, key: str, *, expires_seconds: int = 900) -> str: ...
    def delete(self, key: str) -> bool: ...
    def exists(self, key: str) -> bool: ...


@lru_cache(maxsize=1)
def storage_backend() -> str:
    return (get_env("STORAGE_BACKEND", "local") or "local").strip().lower()


@lru_cache(maxsize=1)
def default_presigned_expires_seconds() -> int:
    return max(30, get_int("S3_PRESIGNED_EXPIRES_SECONDS", 900))


@lru_cache(maxsize=1)
def _max_presigned_expires_seconds() -> int:
    return max(30, get_int("S3_PRESIGNED_EXPIRES_SECONDS_MAX", 3600))


def _normalize_ttl(expires_seconds: int) -> int:
    value = max(30, int(expires_seconds))
    return min(value, _max_presigned_expires_seconds())


def normalize_storage_key(key: str) -> str:
    normalized = str(key or "").strip().replace("\\", "/").lstrip("/")
    if not normalized or "\x00" in normalized:
        raise ValueError("Invalid storage key")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("Invalid storage key")
    return "/".join(parts)


class LocalFSStorage:
    def __init__(self) -> None:
        raw_root = Path(get_env("UPLOADS_DIR", "uploads") or "uploads").expanduser()
        self._root = (raw_root if raw_root.is_absolute() else Path.cwd() / raw_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized = normalize_storage_key(key)
        path = (self._root / normalized).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("Invalid storage key") from exc
        return path

    def put(self, key: str, content: bytes, *, content_type: str | None = None) -> str:
        normalized = normalize_storage_key(key)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return normalized

    def get_url(self, key: str, *, expires_seconds: int = 900) -> str:
        normalized = normalize_storage_key(key)
        return f"/uploads/{normalized}"

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return False
        path.unlink()
        return True

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3Storage:
    def __init__(self) -> None:
        try:
            import boto3  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("boto3 is required for STORAGE_BACKEND=s3") from exc

        is_prod = app_env() in {"prod", "production"}
        endpoint = get_env("S3_ENDPOINT_URL", "http://minio:9000" if not is_prod else "")
        access_key = get_env("S3_ACCESS_KEY", "minio" if not is_prod else "")
        secret_key = get_env("S3_SECRET_KEY", "minio123" if not is_prod else "")
        region = get_env("S3_REGION", "us-east-1")
        self._bucket = (get_env("S3_BUCKET", "vzaimno-uploads") or "vzaimno-uploads").strip()
        if is_prod:
            missing = [
                name
                for name, value in (
                    ("S3_ENDPOINT_URL", endpoint),
                    ("S3_ACCESS_KEY", access_key),
                    ("S3_SECRET_KEY", secret_key),
                    ("S3_BUCKET", self._bucket),
                )
                if not str(value or "").strip()
            ]
            if missing:
                raise RuntimeError(
                    "S3 storage is enabled in production but required values are missing: "
                    + ", ".join(missing)
                )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put(self, key: str, content: bytes, *, content_type: str | None = None) -> str:
        normalized = normalize_storage_key(key)
        params: dict[str, object] = {"Bucket": self._bucket, "Key": normalized, "Body": content}
        if content_type:
            params["ContentType"] = content_type
        self._client.put_object(**params)
        return normalized

    def get_url(self, key: str, *, expires_seconds: int = 900) -> str:
        normalized = normalize_storage_key(key)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": normalized},
            ExpiresIn=_normalize_ttl(expires_seconds),
        )

    def delete(self, key: str) -> bool:
        self._client.delete_object(Bucket=self._bucket, Key=normalize_storage_key(key))
        return True

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=normalize_storage_key(key))
            return True
        except Exception:
            return False


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    if storage_backend() == "s3":
        return S3Storage()
    return LocalFSStorage()
