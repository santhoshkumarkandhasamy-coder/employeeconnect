"""Application settings. Config comes only from environment variables (see deploy/.env.example)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BAD_SECRET_MARKERS = ("change", "secret", "example", "default", "password")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # No silent "dev" default: forgetting APP_ENV must not enable /docs, CORS and weak-secret paths.
    app_env: Literal["dev", "test", "prod"] = "prod"
    log_level: str = "INFO"
    cors_origins: str = ""  # comma separated; empty in prod
    # Comma separated CIDRs/IPs of reverse proxies whose X-Real-IP / X-Forwarded-For may be trusted
    # (e.g. the docker network of the nginx container: "172.18.0.0/16"). Empty = never trust those headers.
    trusted_proxies: str = ""

    # Connect as the non-owner role `app_user` (INSERT/SELECT only on audit_logs).
    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    session_max_days: int = 90  # absolute lifetime of a refresh-token family (rotation never extends past it)
    refresh_grace_seconds: int = 15  # a just-rotated token may be re-presented once (lost response)
    jwt_leeway_seconds: int = 10

    data_encryption_keys: str  # consumed by app.core.crypto (comma separated Fernet keys, newest first)

    file_storage_dir: str = "/data/files"  # docker volume; outside any web root
    max_upload_mb: int = 10  # hard ceiling; per-purpose limits in services/files.py can only be lower
    upload_rate_per_min: int = 20
    max_image_pixels: int = 16_000_000  # decoded-pixel cap for uploaded images (12 MP phones are ~12.2 Mpx)
    image_concurrency: int = 2  # concurrent image re-encodes (memory bound)
    punch_fail_limit: int = 5  # rejected punches (geofence/accuracy/stale fix/mock) per window before a cool-down
    punch_fail_window_seconds: int = 600

    # Excel export (plan 10): workbooks are built in a worker thread; temp files live in a private per-build directory
    # under EXPORT_TEMP_DIR (point it at a size-limited tmpfs/volume; empty = the system temp dir).
    export_temp_dir: str | None = None
    export_max_concurrency: int = Field(2, ge=1, le=8)  # simultaneous workbook builds per process
    export_timeout_s: int = Field(60, ge=5, le=600)  # whole build budget; exceeded -> 422 RANGE_TOO_LARGE
    export_max_rows: int = Field(200_000, ge=1000, le=1_000_000)  # per sheet; counted BEFORE anything is written
    export_max_pending_downloads: int = Field(8, ge=1, le=100)  # built-but-not-yet-drained workbooks per process (disk bound)
    # Volume ceilings per user and UTC day (review 08 M3). Past a quota -> 429 EXPORT_QUOTA_EXCEEDED. The warn thresholds only
    # write a WARNING log line (for alerting); they never block.
    export_daily_max_exports: int = Field(30, ge=1)
    export_daily_max_rows: int = Field(2_000_000, ge=1000)
    export_warn_rows: int = Field(50_000, ge=1)  # a single export with at least this many rows (all sheets) logs a WARNING
    export_daily_warn_exports: int = Field(10, ge=1)
    export_daily_warn_rows: int = Field(500_000, ge=1)
    # Team scope (export.team) has no reporting history (only the CURRENT manager_id), so `from` is clamped to this many
    # months back (review 08 L3). 0 = no clamp.
    export_team_window_months: int = Field(12, ge=0, le=120)

    # Announcements (plan 06)
    announce_write_rate_per_min: int = 10  # create/update/delete per admin
    announce_read_rate_per_min: int = 120  # mark-read per user
    announce_push_batch: int = 500  # device-token batch per push call
    announce_push_max_users: int = 5000  # fan-out ceiling per announcement (users beyond it are not pushed)

    # Profile (plan 07)
    profile_write_rate_per_min: int = 10  # PUT /me/profile per user
    celebrations_window_days: int = 7  # default look-ahead of the home feed

    # Remote admin console (plan 11). PUBLIC_ORIGIN (e.g. https://hr.company.com) is the only Origin accepted by
    # /api/auth/web/*; unset = the console login is disabled (403).
    public_origin: str | None = None
    app_version: str = "dev"  # image tag, shown by /admin/system/health
    status_dir: str = "/status"  # backup container writes backup.json here (read-only mount)
    tls_cert_file: str | None = "/certs/fullchain.pem"  # public certificate (read-only mount) for the expiry check

    # Optional integrations. Compose passes unset values as "" which is treated as "not configured".
    audit_hmac_key: str | None = None  # >= 32 chars (openssl rand -hex 32); chain/HMAC of audit rows
    announcement_deleted_retention_days: int = (
        30  # soft-deleted announcements lose their image/PDF links (and so the blobs) after N days
    )
    announcement_renotify_cooldown_min: int = 60  # min minutes between re-sent pushes of one announcement
    announcement_push_budget_per_day: int = 20  # push-generating announcement writes (create + renotify) per admin per 24 h
    fcm_credentials_file: str | None = None  # Firebase service-account JSON; unset = push is recorded but not sent
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    argon2_time_cost: int = 3
    argon2_memory_kib: int = 64 * 1024
    argon2_parallelism: int = 2

    login_rate_ip: int = 20  # attempts per window per IP
    temp_password_hours: int = Field(72, ge=1, le=720)  # admin-issued temporary passwords stop working after this
    sensitive_view_alert_per_hour: int = Field(
        100, ge=1
    )  # employee detail views with personal data per admin/hour before admins are alerted
    login_rate_eid: int = 10  # attempts per window per employee id
    login_rate_window_seconds: int = 600
    forgot_rate_eid: int = 3  # per hour
    forgot_rate_eid_day: int = 10  # per employee id per day (silent: the response stays 202)
    reset_request_push_per_hour: int = 1  # reset_request pushes per requester and hour
    forgot_rate_ip: int = 10  # per hour
    forgot_min_latency_ms: int = 300  # forgot-password always takes at least this long (timing equalisation)
    argon2_max_threads: int = 4  # bound on concurrent hash/verify threads
    lockout_threshold: int = 5
    lockout_minutes: int = 15

    # Leave / permission write limits (per user; Redis down = refuse, like uploads)
    leave_write_rate_per_min: int = 10  # apply + cancel, both kinds together
    leave_write_rate_per_day: int = 50
    leave_decide_rate_per_min: int = 60  # approve + reject per approver

    @field_validator(
        "audit_hmac_key",
        "fcm_credentials_file",
        "export_temp_dir",
        "public_origin",
        "smtp_host",
        "smtp_user",
        "smtp_password",
        "smtp_from",
        "smtp_port",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, v, info):
        if isinstance(v, str) and not v.strip():
            return 587 if info.field_name == "smtp_port" else None
        return v

    @field_validator("audit_hmac_key")
    @classmethod
    def _hmac_len(cls, v: str | None) -> str | None:
        if v is not None and len(v) < 32:
            raise ValueError("AUDIT_HMAC_KEY must be at least 32 characters")
        return v

    @field_validator("public_origin")
    @classmethod
    def _origin(cls, v: str | None, info) -> str | None:
        if v is None:
            return None
        v = v.strip().rstrip("/").lower()
        msg = "PUBLIC_ORIGIN must look like https://hr.company.com (scheme + host, no path)"
        try:
            u = urlsplit(v)
            host, _port = u.hostname, u.port  # .port raises ValueError on a bad port
        except ValueError:
            raise ValueError(msg) from None
        if u.scheme not in ("http", "https") or not host or u.path or u.query or u.fragment or "@" in u.netloc:
            raise ValueError(msg)
        # plain http only for local development (any non-prod env) or when the host is exactly a loopback name
        if u.scheme == "http" and info.data.get("app_env", "prod") == "prod" and host not in ("localhost", "127.0.0.1"):
            raise ValueError("PUBLIC_ORIGIN must use https:// in production")
        return v

    @field_validator("smtp_port")
    @classmethod
    def _port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("SMTP_PORT must be 1-65535")
        return v

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        for prefix in ("postgresql://", "postgres://", "postgresql+psycopg://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix) :]
        return v

    @field_validator("jwt_algorithm")
    @classmethod
    def _pin_alg(cls, v: str) -> str:
        if v != "HS256":
            raise ValueError("only HS256 is allowed")
        return v

    @field_validator("trusted_proxies")
    @classmethod
    def _proxies(cls, v: str) -> str:
        for item in v.split(","):
            if item.strip():
                ipaddress.ip_network(item.strip(), strict=False)  # raises ValueError on garbage -> startup fails
        return v

    @model_validator(mode="after")
    def _prod_checks(self) -> "Settings":
        if self.smtp_host and not self.smtp_from:
            raise ValueError("SMTP_FROM is required when SMTP_HOST is set")
        if self.fcm_credentials_file and self.app_env != "test" and not Path(self.fcm_credentials_file).is_file():
            raise ValueError("FCM_CREDENTIALS_FILE does not point to a readable file")
        if self.app_env == "prod":
            low = self.jwt_secret.lower()
            if any(m in low for m in _BAD_SECRET_MARKERS):
                raise ValueError("JWT_SECRET looks like a placeholder; refusing to start in prod")
            if self.cors_origins:
                raise ValueError("CORS_ORIGINS must be empty in prod")
            if not self.export_temp_dir or not (
                PurePosixPath(self.export_temp_dir).is_absolute() or Path(self.export_temp_dir).is_absolute()
            ):
                raise ValueError("EXPORT_TEMP_DIR (absolute path of a dedicated, size-limited directory) is required in prod")
        return self

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(i.strip(), strict=False) for i in self.trusted_proxies.split(",") if i.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
