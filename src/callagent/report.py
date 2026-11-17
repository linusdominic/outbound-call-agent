"""Score every recorded run and write the report. No model is called."""

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from callagent import store
from callagent.personas import MIX, Persona
from callagent.playbook import ROOT, Playbook
from callagent.scoring import CHECKS, correct, flagged, score

MODES = {"guarded": "Rules enforced in code", "prompt_only": "Rules in the prompt only"}


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = hits / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    scale = 1 + z * z / total
    return (max(0.0, (centre - spread) / scale), min(1.0, (centre + spread) / scale))


def mcnemar(only_first: int, only_second: int) -> float:
    """Exact two-sided McNemar test on the pairs where exactly one of two runs was right."""
    n = only_first + only_second
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(only_first, only_second) + 1)) / 2**n
    return min(1.0, 2 * tail)


def frac(hits: int, total: int) -> str:
    return f"{hits}/{total} ({100 * hits / total:.0f}%)" if total else "n/a"


@dataclass
class Run:
    mode: str
    model: str
    records: dict[str, dict[str, Any]]  # persona id -> record
    checks: dict[str, dict[str, bool | None]]  # persona id -> check -> passed

    @property
    def name(self) -> str:
        return f"{MODES[self.mode]} ({self.model})"

    def tally(self, check: str) -> tuple[int, int]:
        values = [c[check] for c in self.checks.values() if c[check] is not None]
        return sum(values), len(values)

    @property
    def right(self) -> set[str]:
        return {pid for pid, checks in self.checks.items() if correct(checks)}

    def mean(self, key: str) -> float:
        return sum(r[key] for r in self.records.values()) / len(self.records)


def load_review(root: Path) -> dict[str, dict[str, Any]]:
    """My reading of each flagged line: is it really a broken rule, and why."""
    path = root / "results" / "flag_review.json"
    return json.loads(path.read_text()) if path.exists() else {}


def load_runs(
    paths: store.Paths, personas: dict[str, Persona], playbook: Playbook
) -> tuple[list[Run], list[str]]:
    """Every recorded run, scored, and a line for the report about each flagged quote."""
    review = load_review(paths.root)
    runs, notes = [], []
    for path in sorted(paths.results.glob("calls.*.jsonl")):
        _, mode, model = path.stem.split(".", 2)
        records = {}
        for record in store.read(path):
            confirmed = []
            for quote in flagged(record):
                verdict = review.get(f"{paths.name}/{mode}/{record['persona']}/{quote}")
                real = verdict is None or verdict["real"]
                if real:
                    confirmed.append(quote)
                why = verdict["why"] if verdict else "not reviewed, so counted as a broken rule"
                notes.append(
                    f'- `{record["persona"]}`, {MODES[mode].lower()}: "{quote}" '
                    f"{'Counted' if real else 'Not counted'}: {why}"
                )
            records[record["persona"]] = {**record, "confirmed_flags": confirmed}
        checks = {pid: score(rec, personas[pid], playbook) for pid, rec in records.items()}
        runs.append(Run(mode, f"claude-{model}", records, checks))
    return sorted(runs, key=lambda run: (run.mode != "guarded", run.model)), notes


def set_section(paths: store.Paths, title: str, playbook: Playbook) -> list[str]:
    if not paths.personas.exists():
        return []
    personas = {row["id"]: Persona.from_json(row) for row in store.read(paths.personas)}
    runs, notes = load_runs(paths, personas, playbook)
    kinds = Counter(persona.kind for persona in personas.values())
    mix = ", ".join(f"{kinds[kind]} {kind}" for kind in MIX if kinds[kind])
    lines = [f"## {title}", "", f"{len(personas)} simulated contacts: {mix}.", ""]
    if not runs:
        return [*lines, "No runs yet. Run `callagent simulate`.", ""]

    header = " | ".join(run.name for run in runs)
    lines += [
        f"| Check | {header} |",
        "|---|" + "---|" * len(runs),
        "| **Calls with every check passed** | "
        + " | ".join(f"**{frac(len(run.right), len(run.records))}**" for run in runs)
        + " |",
    ]
    for check, label in CHECKS.items():
        lines.append(f"| {label} | " + " | ".join(frac(*run.tally(check)) for run in runs) + " |")
    lines += [
        "| Cost per call, agent and simulated contact | "
        + " | ".join(
            f"${run.mean('agent_cost_usd') + run.mean('contact_cost_usd'):.4f}" for run in runs
        )
        + " |",
        "| Things the agent said per call | "
        + " | ".join(f"{run.mean('agent_turns'):.1f}" for run in runs)
        + " |",
        "| Model time per call | "
        + " | ".join(f"{run.mean('agent_latency_s'):.1f} s" for run in runs)
        + " |",
        "",
        "A check is counted only on the calls it applies to. The 95% interval on "
        + "; ".join(
            f"{frac(len(run.right), len(run.records))} is "
            f"{100 * wilson(len(run.right), len(run.records))[0]:.0f} to "
            f"{100 * wilson(len(run.right), len(run.records))[1]:.0f}%"
            for run in runs
        )
        + ".",
        "",
    ]

    by_mode = {run.mode: run for run in runs if "haiku" in run.model}
    if len(by_mode) == 2:
        first, second = by_mode["guarded"], by_mode["prompt_only"]
        shared = set(first.records) & set(second.records)
        only_first = len((first.right - second.right) & shared)
        only_second = len((second.right - first.right) & shared)
        lines += [
            f"On the same {len(shared)} contacts, {only_first} calls were right only with the "
            f"rules in code and {only_second} only with the rules in the prompt "
            f"(exact McNemar p = {mcnemar(only_first, only_second):.3f}).",
            "",
        ]

    lines += ["### By kind of contact", "", f"| Kind | Contacts | {header} |"]
    lines.append("|---|---|" + "---|" * len(runs))
    for kind in MIX:
        ids = [pid for pid, persona in personas.items() if persona.kind == kind]
        if ids:
            cells = [
                frac(len(run.right & set(ids)), len(set(ids) & set(run.records))) for run in runs
            ]
            lines.append(f"| {kind} | {len(ids)} | " + " | ".join(cells) + " |")

    for run in runs:
        events = [e for record in run.records.values() for e in record["events"]]
        guard_rules = Counter(e["rule"] for e in events if e["type"] == "guard")
        sources = Counter(e["source"] for e in events if e["type"] == "do_not_call")
        refused = Counter(e["name"] for e in events if e["type"] == "tool" and not e["ok"])
        lines += [
            "",
            f"### What happened inside: {run.name}",
            "",
            f"- Replies the guard changed before they were spoken: {sum(guard_rules.values())}"
            + (
                f" ({', '.join(f'{n} {rule}' for rule, n in guard_rules.most_common())})."
                if guard_rules
                else "."
            ),
            "- Do-not-call requests recorded by: "
            + (", ".join(f"{source} {n}" for source, n in sources.most_common()) or "nobody")
            + ".",
            "- Actions the tools refused: "
            + (", ".join(f"{name} {n}" for name, n in refused.most_common()) or "none")
            + ".",
            "",
            "Calls with a failed check:",
            "",
        ]
        wrong = sorted(set(run.records) - run.right)
        for pid in wrong:
            failed = [CHECKS[c] for c, ok in run.checks[pid].items() if ok is False]
            lines.append(
                f"- `{pid}` ({personas[pid].kind}, ended as {run.records[pid]['outcome']}): "
                + "; ".join(failed)
                + "."
            )
        if not wrong:
            lines.append("None.")
    lines += [
        "",
        "### Lines flagged as possibly breaking a rule",
        "",
        "Flagged by the guard's patterns or by a separate model reading the agent's lines. "
        "Each one was read by me, and the verdicts are in `results/flag_review.json`.",
        "",
        *(notes or ["None."]),
    ]
    return [*lines, ""]


def render(root: Path = ROOT) -> str:
    playbook = Playbook.load()
    lines = [
        "# Evaluation report",
        "",
        "Built by `callagent evaluate` from the files in `data/` and `results/`. No model is "
        "called.",
        "",
        "The held-out contacts were generated and called after the prompt and the guard "
        "patterns were frozen. The development contacts are the ones those were worked out "
        "on.",
        "",
    ]
    lines += set_section(store.Paths("heldout", root), "Held-out set", playbook)
    lines += set_section(store.Paths("dev", root), "Development set", playbook)
    return "\n".join(lines).rstrip()
