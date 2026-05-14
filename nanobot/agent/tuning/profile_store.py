"""Persistence for reusable tuning intake profiles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
import uuid

import yaml

from nanobot.agent.tuning.schema import TuningRequirements

_PROFILE_DIR = Path(".agent/tuning/profiles")
_YAML_SUFFIXES = (".yaml", ".yml")


@dataclass(slots=True)
class StoredTuningProfile:
    profile_id: str
    name: str
    path: Path
    requirements: TuningRequirements
    saved_at: str = ""
    task_description: str = ""
    redacted_fields: list[str] | None = None

    def summary(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "path": str(self.path),
            "target_system": self.requirements.target_system,
            "target_version": self.requirements.target_version,
            "host": self.requirements.host,
            "port": self.requirements.port,
            "config_file": self.requirements.config_file,
            "benchmark_profile_path": self.requirements.benchmark_profile_path,
            "saved_at": self.saved_at,
            "task_description": self.task_description,
            "redacted_fields": list(self.redacted_fields or []),
        }


class TuningProfileStore:
    """Store structured intake results as reusable YAML profiles."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.profile_dir = workspace / _PROFILE_DIR

    def list_profiles(self, target_system: str | None = None) -> list[StoredTuningProfile]:
        if not self.profile_dir.exists():
            return []

        profiles: list[StoredTuningProfile] = []
        for path in sorted(self.profile_dir.glob("*.y*ml")):
            profile = self._load_stored_profile(path)
            if profile is None:
                continue
            if target_system and profile.requirements.target_system != target_system:
                continue
            profiles.append(profile)

        profiles.sort(key=lambda item: (item.saved_at, item.name), reverse=True)
        return profiles

    def load_requirements(self, path: str | Path) -> TuningRequirements:
        stored = self._load_stored_profile(Path(path))
        if stored is None:
            raise FileNotFoundError(path)
        return stored.requirements

    def save_requirements(
        self,
        requirements: TuningRequirements,
        *,
        task_description: str = "",
    ) -> Path:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        profile_id = uuid.uuid4().hex[:12]
        slug = _slugify(
            "-".join(
                part
                for part in (
                    requirements.target_system,
                    requirements.host or "host",
                    requirements.port or "port",
                )
                if part
            )
        ) or "tuning-profile"
        path = self.profile_dir / f"{slug}-{profile_id}.yaml"
        serialized, redacted_fields = _serialize_requirements(requirements)
        payload = {
            "metadata": {
                "profile_id": profile_id,
                "name": slug,
                "saved_at": datetime.now(UTC).isoformat(),
                "task_description": task_description,
                "redacted_fields": redacted_fields,
            },
            "requirements": serialized,
        }
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        return path

    def _load_stored_profile(self, path: Path) -> StoredTuningProfile | None:
        if not path.exists() or path.suffix.lower() not in _YAML_SUFFIXES:
            return None

        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        metadata = payload.get("metadata", {})
        requirements_data = payload.get("requirements", payload)
        if not isinstance(requirements_data, dict):
            return None

        try:
            requirements = TuningRequirements.from_dict(requirements_data)
        except Exception:
            return None

        if not requirements.target_system:
            return None

        name = path.stem
        profile_id = path.stem
        saved_at = ""
        task_description = ""
        redacted_fields: list[str] = []
        if isinstance(metadata, dict):
            profile_id = str(metadata.get("profile_id", profile_id))
            name = str(metadata.get("name", name))
            saved_at = str(metadata.get("saved_at", ""))
            task_description = str(metadata.get("task_description", ""))
            raw_redacted = metadata.get("redacted_fields", [])
            if isinstance(raw_redacted, list):
                redacted_fields = [str(item) for item in raw_redacted]

        return StoredTuningProfile(
            profile_id=profile_id,
            name=name,
            path=path,
            requirements=requirements,
            saved_at=saved_at,
            task_description=task_description,
            redacted_fields=redacted_fields,
        )


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    normalized = normalized.strip("-._")
    return normalized[:80]


def _serialize_requirements(requirements: TuningRequirements) -> tuple[dict[str, object], list[str]]:
    data = requirements.to_dict()
    redacted_fields: list[str] = []
    if data.get("password"):
        data["password"] = ""
        redacted_fields.append("password")
    return data, redacted_fields
