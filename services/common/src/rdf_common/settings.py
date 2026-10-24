"""Fail-fast settings loaders for the Python services.

Each service calls ``load_settings(<ServiceSettings>, "<service>")`` once at
startup. Values come from the process environment first, then from the repo
root ``.env`` (gitignored). A required key that is missing or blank makes the
process exit non-zero with a message naming the key. Values are never printed.

Key names match ``.env.example``; keep the two in sync.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, TypeVar

from pydantic import Field, SecretStr, StringConstraints, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# services/common/src/rdf_common/settings.py -> repo root is parents[4]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = Path(os.environ.get("RDF_ENV_FILE", REPO_ROOT / ".env"))

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RdfSettings(BaseSettings):
    """Base class: env vars win over .env; blank values count as missing."""

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )


# ---------- building blocks ----------


class SpacetimeSettings(RdfSettings):
    spacetime_host: NonEmptyStr
    spacetime_db_name: NonEmptyStr
    spacetime_token: SecretStr = Field(min_length=1)


class ModalSettings(RdfSettings):
    # Optional locally: the Modal CLI is logged in via ~/.modal.toml.
    modal_token_id: SecretStr | None = None
    modal_token_secret: SecretStr | None = None


class StorageSettings(RdfSettings):
    """S3-compatible object storage for episode media. Load it only where used."""

    storage_endpoint: NonEmptyStr
    storage_bucket: NonEmptyStr
    storage_access_key: SecretStr = Field(min_length=1)
    storage_secret_key: SecretStr = Field(min_length=1)


# ---------- per-service settings ----------


class OrchestratorSettings(SpacetimeSettings, ModalSettings):
    anthropic_api_key: SecretStr = Field(min_length=1)
    orchestrator_url: NonEmptyStr = "http://localhost:8000"


class FactorySettings(SpacetimeSettings):
    factory_api_key: SecretStr = Field(min_length=1)
    orchestrator_url: NonEmptyStr = "http://localhost:8000"


class TrainingSettings(SpacetimeSettings, ModalSettings):
    hf_token: SecretStr = Field(min_length=1)
    wandb_api_key: SecretStr | None = None


class RobotClientSettings(SpacetimeSettings):
    robot_id: NonEmptyStr = "yam-1"
    orchestrator_url: NonEmptyStr = "http://localhost:8000"
    jetson_ws_port: int = Field(default=8765, ge=1, le=65535)


class PaymentsSettings(RdfSettings):
    stripe_secret_key: SecretStr = Field(min_length=1)
    stripe_webhook_secret: SecretStr | None = None

    @field_validator("stripe_secret_key")
    @classmethod
    def _test_mode_only(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith(("sk_test_", "rk_test_")):
            raise ValueError("must be a Stripe TEST key (sk_test_/rk_test_); no real money")
        return v


# ---------- loader ----------

S = TypeVar("S", bound=RdfSettings)


def settings_errors(cls: type[RdfSettings], exc: ValidationError) -> list[str]:
    """Turn a ValidationError into 'ENV_VAR: reason' lines, without values."""
    lines = []
    for err in exc.errors(include_input=False, include_url=False):
        field = str(err["loc"][0]) if err["loc"] else "?"
        name = field.upper()
        reason = "missing (required)" if err["type"] == "missing" else err["msg"]
        lines.append(f"{name}: {reason}")
    return lines


def load_settings(cls: type[S], service: str, *, env_file: Path | None | str = "default") -> S:
    """Load settings or exit(2) naming every missing/invalid env var.

    Pass ``env_file=None`` to ignore ``.env`` (tests use this with fake env).
    """
    try:
        if env_file == "default":
            return cls()
        return cls(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        lines = settings_errors(cls, exc)
        msg = "\n".join(f"  - {line}" for line in lines)
        print(
            f"[{service}] config error: {len(lines)} env var(s) missing or invalid:\n{msg}\n"
            f"  Set them in the environment or in .env (see .env.example; "
            f"check with ./scripts/check-env.sh).",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
