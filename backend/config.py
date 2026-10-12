"""Environment & security-policy settings for ClaimShield AI.

All runtime knobs live in the :class:`Settings` dataclass so that tests and the
end-to-end simulation can build fully isolated application contexts (own SQLite
file, own audit-log file, own HMAC secret).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (no external dependency). Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _today() -> date:
    raw = os.getenv("CLAIMSHIELD_TODAY")
    return date.fromisoformat(raw) if raw else date.today()


@dataclass
class Settings:
    root: Path = ROOT
    db_path: Path = field(default_factory=lambda: Path(os.getenv("CLAIMSHIELD_DB", ROOT / "runtime" / "claimshield.db")))
    audit_log_path: Path = field(
        default_factory=lambda: Path(os.getenv("CLAIMSHIELD_AUDIT_LOG", ROOT / "runtime" / "sandbox_operation_logs.jsonl"))
    )
    sample_dir: Path = field(default_factory=lambda: ROOT / "sample_data")

    # --- cryptography -----------------------------------------------------
    # A fresh random secret per process unless pinned via env; the LLM/agent
    # code never has access to it, so it cannot forge session or L3 tokens.
    hmac_secret: str = field(default_factory=lambda: os.getenv("CLAIMSHIELD_HMAC_SECRET") or secrets.token_hex(32))
    session_ttl_seconds: int = 3600
    approval_ttl_seconds: int = 600
    otp_max_attempts: int = 3
    otp_ttl_seconds: int = 600

    # --- security policy ---------------------------------------------------
    agent_max_tier: int = 2  # an agent session may NEVER be above L2
    budget_cap_ratio: float = 0.10  # riders may cost at most 10% of disposable income / month
    firewall_enabled: bool = True

    # --- sandbox -----------------------------------------------------------
    sandbox_latency_ms: int = 1200  # simulated algorithmic-denial latency
    sandbox_real_sleep: bool = False  # UI animates the delay itself

    # --- LLM engine --------------------------------------------------------
    demo_offline_mode: bool = field(default_factory=lambda: _env_bool("DEMO_OFFLINE_MODE", False))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    llm_timeout_seconds: float = 20.0

    today: date = field(default_factory=_today)

    @property
    def llm_provider(self) -> str:
        """'offline' unless a key is present and DEMO_OFFLINE_MODE is not forced."""
        if self.demo_offline_mode:
            return "offline"
        if self.openai_api_key:
            return "openai"
        if self.anthropic_api_key:
            return "anthropic"
        if self.gemini_api_key:
            return "gemini"
        return "offline"

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.audit_log_path).parent.mkdir(parents=True, exist_ok=True)


def default_settings() -> Settings:
    return Settings()
