"""Settings from environment variables (loaded from .env by the CLI) with safe defaults.

Every data path defaults to somewhere under the data dir (``private/`` unless overridden),
so nothing confidential is written into tracked directories.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = "private"
DEFAULT_SOURCE_PRIORITY = ("csv",)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    census_api_key: str | None = None
    # Earlier sources win when building a company's canonical record.
    source_priority: tuple[str, ...] = DEFAULT_SOURCE_PRIORITY
    user_agent_contact: str | None = None
    llm_backend: str = "ollama"
    llm_model: str = "qwen2.5:7b"
    ollama_host: str = "http://127.0.0.1:11434"
    allow_remote_llm: bool = False
    fetch_min_delay: float = 2.0
    fetch_max_pages: int = 5
    sam_api_key: str | None = None
    sam_daily_budget: int = 8

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        priority = tuple(
            s.strip() for s in env.get("DEALSOURCE_SOURCE_PRIORITY", "").split(",") if s.strip()
        )
        return cls(
            data_dir=Path(env.get("DEALSOURCE_DATA_DIR") or DEFAULT_DATA_DIR),
            census_api_key=env.get("CENSUS_API_KEY") or None,
            source_priority=priority or DEFAULT_SOURCE_PRIORITY,
            user_agent_contact=env.get("DEALSOURCE_USER_AGENT_CONTACT") or None,
            llm_backend=env.get("LLM_BACKEND") or "ollama",
            llm_model=env.get("LLM_MODEL") or "qwen2.5:7b",
            ollama_host=env.get("OLLAMA_HOST") or "http://127.0.0.1:11434",
            allow_remote_llm=env.get("DEALSOURCE_ALLOW_REMOTE_LLM") == "1",
            fetch_min_delay=float(env.get("FETCH_MIN_DELAY_SECONDS") or 2.0),
            fetch_max_pages=int(env.get("FETCH_MAX_PAGES_PER_SITE") or 5),
            sam_api_key=env.get("SAM_API_KEY") or None,
            sam_daily_budget=int(env.get("SAM_DAILY_REQUEST_BUDGET") or 8),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dealsource.db"

    @property
    def split_manifest_path(self) -> Path:
        return self.data_dir / "labels_split.json"

    @property
    def labels_path(self) -> Path:
        return self.data_dir / "labels.csv"

    @property
    def to_label_path(self) -> Path:
        return self.data_dir / "to_label.csv"

    @property
    def osm_dir(self) -> Path:
        return self.data_dir / "cache" / "osm"

    @property
    def sam_dir(self) -> Path:
        return self.data_dir / "cache" / "sam"

    @property
    def overrides_path(self) -> Path:
        return self.data_dir / "overrides.yaml"

    @property
    def review_dir(self) -> Path:
        return self.data_dir / "review"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def evals_dir(self) -> Path:
        return self.data_dir / "evals"

    @property
    def test_eval_log_path(self) -> Path:
        return self.evals_dir / "test_eval_log.jsonl"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def user_agent(settings: Settings) -> str:
    from dealsource import __version__

    contact = settings.user_agent_contact
    return f"dealsource/{__version__}" + (f" (+{contact})" if contact else "")
