"""Per-run context handed to every stage: settings, job store and the video's cache dir."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from explainer.config import Settings
from explainer.jobs import JobStore

M = TypeVar("M", bound=BaseModel)


@dataclass
class Context:
    video_id: str
    settings: Settings
    jobs: JobStore
    transcript_method: str

    @property
    def dir(self) -> Path:
        path = self.settings.data_dir / self.video_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, filename: str) -> Path:
        return self.dir / filename

    def save_model(self, filename: str, model: BaseModel) -> None:
        self.path(filename).write_text(model.model_dump_json(indent=2), encoding="utf-8")

    def load_model(self, filename: str, cls: type[M]) -> M:
        path = self.path(filename)
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; run the earlier stage first")
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save_json(self, filename: str, data) -> None:
        self.path(filename).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def load_json(self, filename: str):
        return json.loads(self.path(filename).read_text(encoding="utf-8"))
