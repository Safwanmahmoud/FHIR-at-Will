"""Configuration (AGENTS.md 6).

All configuration arrives as environment variables and is validated once, at
startup, into an immutable :class:`Settings` object. A misconfigured process
must not start: :func:`load_settings` raises :class:`ConfigurationError` with a
report naming *every* missing or invalid variable, not just the first.

Defaults are chosen so that the unsafe option always has to be typed out. In
particular ``LOCAL_ONLY_MODE`` defaults to false only because the dev
docker-compose stack needs to reach its own Ollama container; every other
safety-relevant default fails closed.
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Final, Self

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from fhirbridge.deid.policy import DeidMode, DeidProfile
from fhirbridge.util.duration import DurationParseError, parse_duration

logger = logging.getLogger(__name__)

KEY_BYTES: Final[int] = 32


class ConfigurationError(RuntimeError):
    """Fatal startup error. The message lists every problem found."""


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production(self) -> bool:
        return self is Environment.PRODUCTION


class LlmMode(StrEnum):
    """How LLM credentials may be sourced (AGENTS.md 7.1)."""

    BYOK = "byok"
    """Only caller-supplied credentials. A server key is never used."""

    SERVER_DEFAULT = "server_default"
    """Only the server's configured credential."""

    BYOK_OR_DEFAULT = "byok_or_default"
    """Caller credentials preferred, server credential as a fallback."""

    @property
    def allows_server_default(self) -> bool:
        return self in (LlmMode.SERVER_DEFAULT, LlmMode.BYOK_OR_DEFAULT)


class CredentialStorage(StrEnum):
    DISABLED = "disabled"
    ENCRYPTED_DB = "encrypted_db"
    EXTERNAL_SECRETS = "external_secrets"


class DeliveryMode(StrEnum):
    """How far the delivery plane may progress."""

    OFF = "off"
    PLAN_ONLY = "plan_only"
    SUBMIT = "submit"


class QualificationTier(StrEnum):
    """Model qualification tiers (AGENTS.md 7.5), ordered worst to best."""

    UNQUALIFIED = "unqualified"
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"

    @property
    def rank(self) -> int:
        return _TIER_RANK[self]

    def satisfies(self, minimum: QualificationTier) -> bool:
        return self.rank >= minimum.rank


_TIER_RANK: Final[dict[QualificationTier, int]] = {
    QualificationTier.UNQUALIFIED: 0,
    QualificationTier.BRONZE: 1,
    QualificationTier.SILVER: 2,
    QualificationTier.GOLD: 3,
}


class TerminologyAuthMode(StrEnum):
    NONE = "none"
    BASIC = "basic"
    BEARER = "bearer"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"


class IgPackage:
    """A parsed ``name#version`` implementation-guide coordinate."""

    __slots__ = ("name", "version")

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version

    @classmethod
    def parse(cls, raw: str) -> IgPackage:
        text = raw.strip()
        if "#" not in text:
            raise ValueError(f"IG package {raw!r} must be written as 'name#version'")
        name, _, version = text.partition("#")
        if not name or not version:
            raise ValueError(f"IG package {raw!r} must be written as 'name#version'")
        return cls(name, version)

    @property
    def coordinate(self) -> str:
        return f"{self.name}#{self.version}"

    def __str__(self) -> str:
        return self.coordinate

    def __repr__(self) -> str:
        return f"IgPackage({self.coordinate!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, IgPackage) and (self.name, self.version) == (
            other.name,
            other.version,
        )

    def __hash__(self) -> int:
        return hash((self.name, self.version))


def _decode_key(value: SecretStr, field_name: str) -> bytes:
    """Decode a base64 32-byte key, raising a message that never echoes the key."""
    try:
        raw = base64.b64decode(value.get_secret_value(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field_name} must be valid base64") from exc
    if len(raw) != KEY_BYTES:
        raise ValueError(f"{field_name} must decode to exactly {KEY_BYTES} bytes, got {len(raw)}")
    return raw


CsvList = Annotated[list[str], NoDecode, Field(default_factory=list)]
"""A comma-separated environment variable.

``NoDecode`` is required, not decorative: pydantic-settings JSON-decodes every
complex field at the source, so without it ``A=x,y`` raises SettingsError before
:meth:`Settings._split_csv` ever sees the string, and the only settable values
would be JSON arrays.
"""


class Settings(BaseSettings):
    """Validated process configuration. Immutable after construction."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        frozen=True,
        validate_default=True,
    )

    # --- Runtime ----------------------------------------------------------
    environment: Environment = Field(
        default=Environment.DEVELOPMENT,
        validation_alias=AliasChoices("FHIRBRIDGE_ENV", "ENVIRONMENT"),
    )
    service_name: str = Field(default="fhirbridge", validation_alias="SERVICE_NAME")
    api_root_path: str = Field(default="", validation_alias="API_ROOT_PATH")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    json_logs: bool = Field(default=True, validation_alias="JSON_LOGS")

    # --- Infrastructure ---------------------------------------------------
    database_url: str = Field(validation_alias="DATABASE_URL")
    redis_url: str = Field(validation_alias="REDIS_URL")
    db_pool_size: int = Field(default=10, ge=1, le=200, validation_alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, ge=0, le=200, validation_alias="DB_MAX_OVERFLOW")
    require_rls_enforcement: bool = Field(
        default=True,
        validation_alias="REQUIRE_RLS_ENFORCEMENT",
        description=(
            "Refuse readiness when Postgres reports that row-level security does not "
            "apply to the connected role — which is what happens if the application "
            "connects as a superuser or a BYPASSRLS role, the default in the official "
            "Postgres image. Turning this off means tenant isolation rests on query "
            "construction alone (see docs/deployment.md#database-role)."
        ),
    )

    # --- Secrets ----------------------------------------------------------
    master_key: SecretStr | None = Field(default=None, validation_alias="FHIRBRIDGE_MASTER_KEY")
    ephemeral_key: SecretStr | None = Field(
        default=None, validation_alias="FHIRBRIDGE_EPHEMERAL_KEY"
    )

    # --- FHIR dependencies ------------------------------------------------
    validator_url: AnyHttpUrl = Field(validation_alias="VALIDATOR_URL")
    terminology_url: AnyHttpUrl = Field(validation_alias="TERMINOLOGY_URL")
    terminology_auth_mode: TerminologyAuthMode = Field(
        default=TerminologyAuthMode.NONE, validation_alias="TERMINOLOGY_AUTH_MODE"
    )
    terminology_username: str | None = Field(default=None, validation_alias="TERMINOLOGY_USERNAME")
    terminology_password: SecretStr | None = Field(
        default=None, validation_alias="TERMINOLOGY_PASSWORD"
    )
    terminology_token: SecretStr | None = Field(default=None, validation_alias="TERMINOLOGY_TOKEN")
    validator_version: str | None = Field(
        default=None,
        validation_alias="VALIDATOR_VERSION",
        description=(
            "The org.hl7.fhir.core version running in the sidecar. Set by "
            "docker/validator so it can be stamped into every report (principle 2.8); "
            "the sidecar exposes no version endpoint to read it from."
        ),
    )
    validator_timeout_s: float = Field(
        default=120.0, gt=0, le=900, validation_alias="VALIDATOR_TIMEOUT_S"
    )
    terminology_timeout_s: float = Field(
        default=30.0, gt=0, le=900, validation_alias="TERMINOLOGY_TIMEOUT_S"
    )
    terminology_cache_ttl: timedelta = Field(
        default=timedelta(hours=24), validation_alias="TERMINOLOGY_CACHE_TTL"
    )

    default_fhir_version: str = Field(default="4.0.1", validation_alias="DEFAULT_FHIR_VERSION")
    default_ig_packages: Annotated[list[IgPackage], NoDecode] = Field(
        default_factory=lambda: [IgPackage("hl7.fhir.us.core", "9.0.0")],
        validation_alias="DEFAULT_IG_PACKAGES",
    )

    # --- BYOK / BYOM (AGENTS.md 7) ---------------------------------------
    llm_mode: LlmMode = Field(default=LlmMode.BYOK, validation_alias="LLM_MODE")
    llm_allowed_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"], validation_alias="LLM_ALLOWED_PROVIDERS"
    )
    llm_egress_allowlist: CsvList = Field(validation_alias="LLM_EGRESS_ALLOWLIST")
    local_only_mode: bool = Field(default=False, validation_alias="LOCAL_ONLY_MODE")
    require_phi_egress_ack: bool = Field(default=True, validation_alias="REQUIRE_PHI_EGRESS_ACK")
    credential_storage: CredentialStorage = Field(
        default=CredentialStorage.DISABLED, validation_alias="CREDENTIAL_STORAGE"
    )
    delivery_mode: DeliveryMode = Field(default=DeliveryMode.OFF, validation_alias="DELIVERY_MODE")
    target_egress_allowlist: CsvList = Field(validation_alias="TARGET_EGRESS_ALLOWLIST")
    max_cost_usd_per_conversion: Decimal = Field(
        default=Decimal("1.00"), ge=0, validation_alias="MAX_COST_USD_PER_CONVERSION"
    )
    min_qualification_tier: QualificationTier = Field(
        default=QualificationTier.BRONZE, validation_alias="MIN_QUALIFICATION_TIER"
    )
    allow_insecure_transport: bool = Field(
        default=False, validation_alias="ALLOW_INSECURE_TRANSPORT"
    )

    # --- PHI de-identification -------------------------------------------
    deid_mode: DeidMode = Field(default=DeidMode.OFF, validation_alias="DEID_MODE")
    deid_profile: DeidProfile = Field(
        default=DeidProfile.HIPAA_SAFE_HARBOR,
        validation_alias="DEID_PROFILE",
    )
    deid_allow_audio_egress: bool = Field(
        default=False,
        validation_alias="DEID_ALLOW_AUDIO_EGRESS",
    )

    # --- Pipeline / retention --------------------------------------------
    retention_default: timedelta = Field(
        default=timedelta(days=30), validation_alias="RETENTION_DEFAULT"
    )
    max_repair_iterations: int = Field(
        default=2, ge=0, le=10, validation_alias="MAX_REPAIR_ITERATIONS"
    )
    max_upload_bytes: int = Field(
        default=25 * 1024 * 1024, ge=1024, validation_alias="MAX_UPLOAD_BYTES"
    )
    max_request_bytes: int = Field(
        default=32 * 1024 * 1024, ge=1024, validation_alias="MAX_REQUEST_BYTES"
    )

    # --- Observability ----------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    debug_capture_llm_io: bool = Field(default=False, validation_alias="DEBUG_CAPTURE_LLM_IO")

    # --- Validators -------------------------------------------------------
    @field_validator(
        "llm_allowed_providers",
        "llm_egress_allowlist",
        "target_egress_allowlist",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("default_ig_packages", mode="before")
    @classmethod
    def _parse_ig_packages(cls, value: object) -> object:
        """Split and parse in one *before* validator.

        Parsing cannot happen in an ``after`` validator: the field is typed
        ``list[IgPackage]``, so a list of strings fails field validation and the
        after-validator never runs. That made ``DEFAULT_IG_PACKAGES`` unsettable
        from the environment while the default value still worked.

        The field is also annotated ``NoDecode``, without which this validator
        is never reached either: pydantic-settings JSON-decodes any complex
        field before validation, so a plain ``a#1.0.0,b#2.0.0`` raised
        SettingsError at the source rather than arriving here as a string.
        """
        items = (
            [part.strip() for part in value.split(",") if part.strip()]
            if isinstance(value, str)
            else value
        )
        if not isinstance(items, list):
            return items
        return [
            item if isinstance(item, IgPackage) else IgPackage.parse(str(item)) for item in items
        ]

    @field_validator("retention_default", "terminology_cache_ttl", mode="before")
    @classmethod
    def _parse_compact_duration(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return parse_duration(value)
            except DurationParseError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("master_key", "ephemeral_key", mode="after")
    @classmethod
    def _validate_key_material(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            _decode_key(value, "key")
        return value

    @field_validator("database_url", mode="after")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use the async driver, e.g. "
                "postgresql+asyncpg://user:pass@host:5432/fhirbridge"
            )
        return value

    @field_validator("redis_url", mode="after")
    @classmethod
    def _require_redis_scheme(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must start with redis://, rediss:// or unix://")
        return value

    @field_validator("default_fhir_version", mode="after")
    @classmethod
    def _supported_fhir_version(cls, value: str) -> str:
        from fhirbridge.version import SUPPORTED_FHIR_VERSIONS

        if value not in SUPPORTED_FHIR_VERSIONS:
            raise ValueError(
                f"DEFAULT_FHIR_VERSION {value!r} is not supported by this build; "
                f"supported: {', '.join(SUPPORTED_FHIR_VERSIONS)}"
            )
        return value

    @field_validator("log_level", mode="after")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"LOG_LEVEL {value!r} is not a valid logging level")
        return level

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Self:
        problems: list[str] = []

        if self.deid_mode is not DeidMode.OFF:
            try:
                from fhirbridge.deid.detectors import validate_assets

                validate_assets()
            except (OSError, ValueError) as exc:
                problems.append(
                    "DEID_MODE requires readable, valid de-identification assets "
                    f"({type(exc).__name__})"
                )

        if self.credential_storage is not CredentialStorage.DISABLED and self.master_key is None:
            problems.append(
                f"FHIRBRIDGE_MASTER_KEY is required when CREDENTIAL_STORAGE="
                f"{self.credential_storage} (envelope encryption has no key to wrap DEKs with)"
            )

        if self.delivery_mode is DeliveryMode.SUBMIT and not (
            self.target_egress_allowlist or self.local_only_mode
        ):
            problems.append(
                "DELIVERY_MODE=submit requires TARGET_EGRESS_ALLOWLIST or LOCAL_ONLY_MODE=true"
            )

        if self.terminology_auth_mode is TerminologyAuthMode.BASIC and not (
            self.terminology_username and self.terminology_password
        ):
            problems.append(
                "TERMINOLOGY_USERNAME and TERMINOLOGY_PASSWORD are required when "
                "TERMINOLOGY_AUTH_MODE=basic"
            )
        if self.terminology_auth_mode is TerminologyAuthMode.BEARER and not self.terminology_token:
            problems.append("TERMINOLOGY_TOKEN is required when TERMINOLOGY_AUTH_MODE=bearer")

        if self.environment.is_production:
            if self.allow_insecure_transport:
                problems.append(
                    "ALLOW_INSECURE_TRANSPORT=true is forbidden in production: it would let "
                    "API keys travel over plaintext HTTP"
                )
            if self.debug_capture_llm_io:
                problems.append(
                    "DEBUG_CAPTURE_LLM_IO=true is forbidden in production: it records prompt "
                    "and completion content, which contains PHI"
                )
            if self.ephemeral_key is None:
                problems.append(
                    "FHIRBRIDGE_EPHEMERAL_KEY is required in production so per-job BYOK "
                    "secrets can be encrypted before they reach Redis"
                )
            if "tx.fhir.org" in str(self.terminology_url):
                problems.append(
                    "TERMINOLOGY_URL points at tx.fhir.org, which is not provisioned for "
                    "production use; run your own terminology server "
                    "(see docs/terminology-setup.md)"
                )
            if str(self.validator_url).startswith("http://") and not str(
                self.validator_url
            ).startswith(("http://localhost", "http://127.0.0.1")):
                problems.append("VALIDATOR_URL must use https in production unless it is loopback")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    # --- Derived accessors ------------------------------------------------
    @property
    def master_key_bytes(self) -> bytes | None:
        return None if self.master_key is None else _decode_key(self.master_key, "master_key")

    @property
    def ephemeral_key_bytes(self) -> bytes | None:
        return (
            None if self.ephemeral_key is None else _decode_key(self.ephemeral_key, "ephemeral_key")
        )

    @property
    def validator_base_url(self) -> str:
        return str(self.validator_url).rstrip("/")

    @property
    def terminology_base_url(self) -> str:
        return str(self.terminology_url).rstrip("/")

    @property
    def ig_coordinates(self) -> tuple[str, ...]:
        return tuple(pkg.coordinate for pkg in self.default_ig_packages)

    def provider_allowed(self, provider: str) -> bool:
        allowed = self.llm_allowed_providers
        return "*" in allowed or provider.lower() in {item.lower() for item in allowed}

    def emit_startup_warnings(self) -> list[str]:
        """Return (and log) the loud warnings required for unsafe-but-allowed settings."""
        warnings: list[str] = []
        if self.allow_insecure_transport:
            warnings.append(
                "ALLOW_INSECURE_TRANSPORT=true: API keys will be accepted over plaintext "
                "HTTP. This is for local development only. Never enable this with real PHI "
                "or real provider keys."
            )
        if self.debug_capture_llm_io:
            warnings.append(
                "DEBUG_CAPTURE_LLM_IO=true: prompts and completions will be recorded, "
                "including any PHI they contain. Disable this before handling real data."
            )
        if not self.local_only_mode and self.llm_mode is not LlmMode.SERVER_DEFAULT:
            warnings.append(
                "LOCAL_ONLY_MODE=false: callers may direct LLM traffic to external "
                "providers. Sending PHI to a third party without a BAA/DPA is likely a "
                "regulatory violation. See docs/byok.md."
            )
        if not self.llm_egress_allowlist and not self.local_only_mode:
            warnings.append(
                "LLM_EGRESS_ALLOWLIST is empty and LOCAL_ONLY_MODE=false: every "
                "caller-supplied base_url will be rejected. Set one or the other."
            )
        if self.delivery_mode is DeliveryMode.SUBMIT and not self.local_only_mode:
            warnings.append(
                "DELIVERY_MODE=submit permits clinical resources to leave this process for "
                "hosts in TARGET_EGRESS_ALLOWLIST."
            )
        if not self.require_phi_egress_ack:
            warnings.append(
                "REQUIRE_PHI_EGRESS_ACK=false: callers can send PHI to external providers "
                "without recording an acknowledgement."
            )
        if (
            self.environment.is_production
            and self.deid_mode is DeidMode.OFF
            and self.llm_egress_allowlist
        ):
            warnings.append(
                "DEID_MODE=off with external LLM egress enabled: raw clinical narratives "
                "may leave this deployment. A BAA and operator controls remain required."
            )
        if self.credential_storage is not CredentialStorage.DISABLED:
            warnings.append(
                f"CREDENTIAL_STORAGE={self.credential_storage}: provider API keys will be "
                "stored (envelope-encrypted) in this deployment."
            )
        for message in warnings:
            logger.warning("startup_warning", extra={"warning": message})
        return warnings


def _format_validation_error(exc: ValidationError) -> str:
    """Render every problem, one per line, without echoing any secret value."""
    lines = ["fhirbridge cannot start: invalid configuration.", ""]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        env_name = _ENV_ALIAS_BY_FIELD.get(location, location.upper())
        if error["type"] == "missing":
            lines.append(f"  {env_name}: required but not set")
        else:
            lines.append(f"  {env_name}: {error['msg']}")
    lines += [
        "",
        "See docs/deployment.md and .env.example for the full list of variables.",
    ]
    return "\n".join(lines)


def _build_alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, str):
            index[name] = alias
        elif isinstance(alias, AliasChoices):
            first = alias.choices[0]
            index[name] = first if isinstance(first, str) else name.upper()
        else:
            index[name] = name.upper()
    return index


_ENV_ALIAS_BY_FIELD: Final[dict[str, str]] = _build_alias_index()


def load_settings(**overrides: object) -> Settings:
    """Build :class:`Settings` from the environment, or raise a fatal report."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]  # env-sourced, validated below
    except ValidationError as exc:
        raise ConfigurationError(_format_validation_error(exc)) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return load_settings()


def reset_settings_cache() -> None:
    """Clear the singleton. Tests only."""
    get_settings.cache_clear()


__all__ = [
    "ConfigurationError",
    "CredentialStorage",
    "DeidMode",
    "DeidProfile",
    "DeliveryMode",
    "Environment",
    "IgPackage",
    "LlmMode",
    "QualificationTier",
    "Settings",
    "TerminologyAuthMode",
    "get_settings",
    "load_settings",
    "reset_settings_cache",
]
