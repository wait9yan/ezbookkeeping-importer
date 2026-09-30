"""TOML 保存业务决定和日志级别；服务连接只从进程环境读取。"""

import os
import tomllib
from datetime import date, time
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from .domain.errors import ImporterError
from pydantic_core import PydanticCustomError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class BusinessMailSettings(StrictModel):
    source_id: str
    rescan_days: int = Field(default=7, ge=0, strict=True)


class MailSettings(BusinessMailSettings):
    host: str = "imap.qq.com"
    port: int = Field(default=993, ge=1, le=65535)
    username: str = ""
    password: SecretStr = SecretStr("")

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise PydanticCustomError("config_host", "must be nonempty without whitespace")
        return value


class RepaymentMapping(StrictModel):
    source_account_id: str
    destination_account_id: str
    category_id: str
    currency: str
    valid_from: date
    valid_until: date | None = None


class Rule(StrictModel):
    merchant_pattern: str
    category_id: str


class BusinessSettings(StrictModel):
    mail: BusinessMailSettings
    timezone: str
    date_only_time: time | None = None
    repayment_ownership_confirmed: bool = False
    log_level: str = "INFO"
    repayments: tuple[RepaymentMapping, ...] = ()
    rules: tuple[Rule, ...] = ()
    classification_mode: str = "ai"

    @field_validator("log_level")
    @classmethod
    def valid_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise PydanticCustomError(
                "config_log_level", "must be DEBUG, INFO, WARNING, ERROR or CRITICAL"
            )
        return value

    @model_validator(mode="after")
    def validate_choices(self):
        if self.timezone != "Asia/Shanghai":
            raise PydanticCustomError("config_timezone", "must be Asia/Shanghai")
        if self.classification_mode not in {"ai", "rules_only"}:
            raise PydanticCustomError("config_classification_mode", "must be ai or rules_only")
        return self


class Settings(BusinessSettings):
    database_url: SecretStr = SecretStr("")
    ledger_url: str = ""
    ledger_token: SecretStr = SecretStr("")
    mail: MailSettings
    evidence_dir: Path = Path("data/email")
    report_dir: Path = Path("data/reports")
    log_dir: Path = Path("data/logs")
    log_max_bytes: int = Field(default=10_485_760, gt=0)
    log_backups: int = Field(default=5, ge=1)
    ai_url: str | None = None
    ai_model: str | None = None
    ai_token: SecretStr = SecretStr("")

    @field_validator("ledger_url", "ai_url")
    @classmethod
    def valid_service_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(character.isspace() for character in value)
            )
            port = parsed.port
            if not valid or (port is not None and not 1 <= port <= 65535):
                raise ValueError
        except ValueError:
            raise PydanticCustomError(
                "config_service_url",
                "must be an HTTP(S) base URL without credentials, query or fragment",
            ) from None
        return value

    @field_validator("evidence_dir", "report_dir", "log_dir", mode="before")
    @classmethod
    def valid_directory(cls, value: Any) -> Any:
        if isinstance(value, str) and (not value.strip() or "\x00" in value):
            raise PydanticCustomError("config_directory", "must be nonempty without NUL characters")
        return value


class ConfigurationError(ImporterError):
    """Configuration diagnostics contain field names and safe reasons, never inputs."""


ENV_FIELDS = {
    "database_url": "EBKI_DATABASE_URL",
    "ledger_url": "EBKI_LEDGER_URL",
    "ledger_token": "EBKI_LEDGER_TOKEN",
    "ai_url": "EBKI_AI_URL",
    "ai_model": "EBKI_AI_MODEL",
    "ai_token": "EBKI_AI_TOKEN",
    "mail.host": "EBKI_IMAP_HOST",
    "mail.port": "EBKI_IMAP_PORT",
    "mail.username": "EBKI_IMAP_USERNAME",
    "mail.password": "EBKI_IMAP_PASSWORD",
}
LOCAL_COMMANDS = {"migrate", "status", "issues", "sync", "recheck", "resolve"}


def command_capabilities(
    command: str,
    classification_mode: str,
    *,
    action: str | None = None,
    account_id: str | None = None,
) -> frozenset[str]:
    capabilities = {"database"}
    if command in {"run", "doctor"}:
        capabilities.update({"ledger", "mail"})
        if classification_mode == "ai":
            capabilities.add("ai")
    elif command == "restore-audit" or (command == "resolve" and (action == "link" or account_id)):
        capabilities.add("ledger")
    elif command not in LOCAL_COMMANDS:
        raise ConfigurationError("unknown command dependency profile")
    if command in {"migrate", "run"}:
        capabilities.add("create_database")
    if command == "run":
        capabilities.update({"evidence", "pipeline"})
    return frozenset(capabilities)


def validate_command(
    settings: Settings, command: str, *, action: str | None = None, account_id: str | None = None
) -> frozenset[str]:
    capabilities = command_capabilities(
        command, settings.classification_mode, action=action, account_id=account_id
    )
    required: dict[str, str | None] = {
        "EBKI_DATABASE_URL": settings.database_url.get_secret_value()
    }
    if "ledger" in capabilities:
        required.update(
            {
                "EBKI_LEDGER_URL": settings.ledger_url,
                "EBKI_LEDGER_TOKEN": settings.ledger_token.get_secret_value(),
            }
        )
    if "mail" in capabilities:
        required.update(
            {
                "EBKI_IMAP_HOST": settings.mail.host,
                "EBKI_IMAP_USERNAME": settings.mail.username,
                "EBKI_IMAP_PASSWORD": settings.mail.password.get_secret_value(),
            }
        )
    if "ai" in capabilities:
        required.update(
            {
                "EBKI_AI_URL": settings.ai_url,
                "EBKI_AI_MODEL": settings.ai_model,
                "EBKI_AI_TOKEN": settings.ai_token.get_secret_value(),
            }
        )
    missing = [name for name, value in required.items() if value is None or not value.strip()]
    if missing:
        raise ConfigurationError("missing required environment variables: " + ", ".join(missing))
    return capabilities


SAFE_VALIDATION_REASONS = {
    "missing": "required field is missing",
    "extra_forbidden": "unknown TOML field",
    "int_parsing": "must be an integer",
    "int_from_float": "must be an integer",
    "int_type": "must be an integer",
    "greater_than": "must be greater than zero",
    "greater_than_equal": "must be at least one",
    "less_than_equal": "must be at most 65535",
    "string_type": "must be a string",
    "model_type": "must be a TOML table",
    "bool_parsing": "must be a boolean",
    "date_from_datetime_parsing": "must be a valid calendar date",
    "time_parsing": "must be a valid time",
}
BUSINESS_ERROR_FIELDS = {
    "config_timezone": "timezone",
    "config_classification_mode": "classification_mode",
}


def _safe_validation_error(exc: ValidationError) -> ConfigurationError:
    problems = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        code = error["type"]
        field = BUSINESS_ERROR_FIELDS.get(code, ".".join(str(part) for part in error["loc"]))
        name = field if code == "extra_forbidden" else ENV_FIELDS.get(field, field)
        name = name or "business configuration"
        # config_* errors are defined above with constant messages and no input arguments.
        # Other Pydantic messages are replaced with controlled reasons instead of echoed.
        reason = (
            error["msg"]
            if code.startswith("config_")
            else SAFE_VALIDATION_REASONS.get(code, f"invalid value ({code})")
        )
        problems.append(f"{name}: {reason}")
    return ConfigurationError("invalid configuration: " + "; ".join(problems))


def load_settings(
    path: str, *, command: str = "run", action: str | None = None, account_id: str | None = None
) -> Settings:
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError:
        raise ConfigurationError("business configuration: invalid TOML syntax") from None
    except OSError:
        raise ConfigurationError("business configuration file cannot be read") from None
    try:
        data = BusinessSettings.model_validate(data).model_dump()
    except ValidationError as exc:
        raise _safe_validation_error(exc) from None
    for field, variable in ENV_FIELDS.items():
        if variable not in os.environ:
            continue
        value = os.environ[variable]
        parts = field.split(".")
        if len(parts) == 1:
            data[field] = value
        else:
            data[parts[0]][parts[1]] = value
    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise _safe_validation_error(exc) from None
    validate_command(settings, command, action=action, account_id=account_id)
    return settings
