"""The playbook: what the agent may say and do, loaded from ``config/playbook.yml``."""

from dataclasses import dataclass
from datetime import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = ROOT / "config" / "playbook.yml"


@dataclass(frozen=True)
class Question:
    id: str
    ask: str
    values: tuple[str, ...]  # empty for a number

    @property
    def is_number(self) -> bool:
        return not self.values


@dataclass(frozen=True)
class Playbook:
    company: str
    agent_name: str
    callback_number: str
    opening: str
    fallback: str
    voicemail: str
    earliest: time
    latest: time
    max_attempts: int
    min_hours_between_attempts: int
    questions: tuple[Question, ...]
    questions_needed_to_book: int
    answers: dict[str, str]

    @classmethod
    def load(cls, path: Path = PLAYBOOK_PATH) -> "Playbook":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))

        def clean(text: str) -> str:
            return " ".join(text.split())

        calling = raw["calling"]
        return cls(
            company=raw["company"],
            agent_name=raw["agent_name"],
            callback_number=str(raw["callback_number"]),
            opening=clean(raw["opening"]),
            fallback=clean(raw["fallback"]),
            voicemail=clean(raw["voicemail"]),
            earliest=time.fromisoformat(calling["earliest"]),
            latest=time.fromisoformat(calling["latest"]),
            max_attempts=calling["max_attempts"],
            min_hours_between_attempts=calling["min_hours_between_attempts"],
            questions=tuple(
                Question(key, clean(item["ask"]), tuple(item.get("values", ())))
                for key, item in raw["questions"].items()
            ),
            questions_needed_to_book=raw["questions_needed_to_book"],
            answers={key: clean(value) for key, value in raw["answers"].items()},
        )

    def question(self, name: str) -> Question | None:
        return next((q for q in self.questions if q.id == name), None)

    def opening_line(self, first: str) -> str:
        return self.opening.format(agent=self.agent_name, company=self.company, first=first)

    def voicemail_line(self) -> str:
        return self.voicemail.format(
            agent=self.agent_name, company=self.company, callback=self.callback_number
        )
