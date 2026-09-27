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
    def overrides_path(self) -> Path:
        return self.data_dir / "overrides.yaml"

    @property
    def review_dir(self) -> Path:
        return self.data_dir / "review"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def user_agent(settings: Settings) -> str:
    from dealsource import __version__

    contact = settings.user_agent_contact
    return f"dealsource/{__version__}" + (f" (+{contact})" if contact else "")
