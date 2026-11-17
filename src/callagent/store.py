"""Where the evaluation keeps its files.

Two sets of personas from different seeds. The development set is the one the prompt and
the guard patterns were worked out on. The held-out set was generated and run after those
were frozen, and its results are the ones to quote.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from callagent.playbook import ROOT

SETS = {"dev": (60, 5), "heldout": (200, 29)}  # name -> (personas, seed)
SPEND = ROOT / "results" / "spend.json"


@dataclass(frozen=True)
class Paths:
    name: str
    root: Path = ROOT

    @property
    def personas(self) -> Path:
        return self.root / "data" / self.name / "personas.jsonl"

    @property
    def results(self) -> Path:
        return self.root / "results" / self.name

    def calls(self, mode: str, model: str) -> Path:
        return self.results / f"calls.{mode}.{model.removeprefix('claude-')}.jsonl"


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
