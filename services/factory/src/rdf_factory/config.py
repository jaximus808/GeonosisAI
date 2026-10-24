"""rdf_factory configuration, read from the environment (repo ``.env`` fills gaps).

Keys (see ``.env.example``):
  FACTORY_API_KEY          required; used by the droid subprocess (local) or as Bearer (cloud). Never printed.
  FACTORY_MODE             local (default tonight: no git remote, cloud computers can't clone the repo) | cloud
  FACTORY_ENVIRONMENT_ID   cloud: Factory Environment to create Droid Computers from
  FACTORY_COMPUTER_ID      cloud: reuse this Droid Computer instead of creating one
  FACTORY_MODEL            default gpt-5.4-mini (cheap; S-04 measured ~1-3.5k credits per trivial turn)
  FACTORY_REASONING        default low
  FACTORY_REPO_DIR         local: the checkout droids work against (default: this repo)
  FACTORY_STATE_DIR        local metadata + event jsonl (default services/factory/work/state)
  FACTORY_WORK_ROOT        local: parent of the per-session scratch dirs (default services/factory/work/sessions)
  FACTORY_REPO_TOKEN       cloud, optional: repo access token handed to droids that must push (not used yet)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from rdf_common.spacetime_client import load_dotenv

PKG_ROOT = Path(__file__).resolve().parents[2]  # services/factory
REPO_ROOT = PKG_ROOT.parents[1]
DEFAULT_ENVIRONMENT_ID = "173ec531-f4f8-46e0-b246-6594c9590ea5"  # "droid-environment" (S-04)
CLOUD_HOME = "/home/factory-user"
API_BASE = "https://api.factory.ai/api/v0"
MODES = ("local", "cloud")


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key, "")
    return v.strip() if v and v.strip() else default


@dataclass
class FactoryConfig:
    mode: str = "local"
    model: str = "gpt-5.4-mini"
    reasoning: str = "low"
    environment_id: str = DEFAULT_ENVIRONMENT_ID
    computer_id: str = ""
    repo_dir: Path = REPO_ROOT
    state_dir: Path = PKG_ROOT / "work" / "state"
    work_root: Path = PKG_ROOT / "work" / "sessions"
    api_base: str = API_BASE
    turn_timeout_s: float = 900.0
    heartbeat_s: float = 20.0  # current_step is refreshed at least this often while a turn runs
    tags: tuple[str, ...] = ("rdf",)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, mode: str | None = None, *, dotenv: bool = True) -> "FactoryConfig":
        if dotenv:
            load_dotenv()  # setdefault: real env vars win
        m = (mode or _env("FACTORY_MODE", "local")).lower()
        if m not in MODES:
            raise ValueError(f"FACTORY_MODE must be one of {MODES}, got {m!r}")
        return cls(
            mode=m,
            model=_env("FACTORY_MODEL", cls.model),
            reasoning=_env("FACTORY_REASONING", cls.reasoning),
            environment_id=_env("FACTORY_ENVIRONMENT_ID", DEFAULT_ENVIRONMENT_ID),
            computer_id=_env("FACTORY_COMPUTER_ID"),
            repo_dir=Path(_env("FACTORY_REPO_DIR", str(REPO_ROOT))).resolve(),
            state_dir=Path(_env("FACTORY_STATE_DIR", str(cls.state_dir))).resolve(),
            work_root=Path(_env("FACTORY_WORK_ROOT", str(cls.work_root))).resolve(),
        )

    def api_key(self) -> str:
        key = os.environ.get("FACTORY_API_KEY", "").strip()
        if not key:
            raise RuntimeError("FACTORY_API_KEY is not set (see .env.example)")
        return key

    def workdir_for(self, agent_session_id: int, mode: str | None = None) -> str:
        """Per-agent_session working directory (sessions on one cloud computer share its disk)."""
        if (mode or self.mode) == "cloud":
            return f"{CLOUD_HOME}/agents/{agent_session_id}"
        return str(self.work_root / str(agent_session_id))
