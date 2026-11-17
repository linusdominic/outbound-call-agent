"""Commands.

callagent personas     build the simulated contacts for both sets (free)
callagent simulate     run every persona's call against the agent (paid)
callagent evaluate     score the recorded calls against the personas (free)
callagent campaign     show which leads on a list may be called right now (free)
callagent serve        the demo page, and the /v1 endpoints for voice platforms

simulate, evaluate and serve take --set heldout (the default) or --set dev.
"""

import argparse
import asyncio
import csv
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from callagent import store
from callagent.personas import Persona, generate
from callagent.playbook import ROOT, Playbook

DEFAULT_MODEL = os.environ.get("CALLAGENT_MODEL", "claude-haiku-5-5")


def cmd_personas(args: argparse.Namespace) -> None:
    for name, (count, seed) in store.SETS.items():
        personas = generate(count, seed)
        path = store.Paths(name).personas
        store.write(path, [persona.to_json() for persona in personas])
        print(f"{len(personas)} personas in {path.relative_to(ROOT)}")


def cmd_simulate(args: argparse.Namespace) -> None:
    from callagent.llm import LLM
    from callagent.simulator import run_call

    paths = store.Paths(args.set)
    personas = [Persona.from_json(row) for row in store.read(paths.personas)][: args.limit]
    llm = LLM(
        ledger_path=store.SPEND,
        budget_usd=args.budget,
        cache_dir=ROOT / ".cache" / "llm",
        max_concurrency=8,
    )
    playbook = Playbook.load()

    async def run() -> list:
        gate = asyncio.Semaphore(args.parallel)

        async def one(persona: Persona):
            async with gate:
                return await run_call(
                    llm,
                    playbook,
                    persona,
                    mode=args.mode,
                    model=args.model,
                    contact_model=args.contact_model,
                )

        return await asyncio.gather(*(one(p) for p in personas), return_exceptions=True)

    results = asyncio.run(run())
    records = [r for r in results if not isinstance(r, BaseException)]
    for persona, result in zip(personas, results, strict=True):
        if isinstance(result, BaseException):  # reported, never guessed at
            print(f"{persona.id} did not finish: {type(result).__name__}: {result}")
    out = paths.calls(args.mode, args.model)
    store.write(out, records)
    print(f"{len(records)} of {len(personas)} calls recorded in {out.relative_to(ROOT)}.")
    print(f"Total spend so far: ${llm.ledger.total:.4f}")


def cmd_evaluate(args: argparse.Namespace) -> None:
    from callagent import report

    text = report.render()
    if args.write:
        (ROOT / "results" / "report.md").write_text(text + "\n")
    print(text)


def cmd_campaign(args: argparse.Namespace) -> None:
    from callagent.book import Book, Lead
    from callagent.gate import may_call

    playbook = Playbook.load()
    now = datetime.fromisoformat(args.now)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("America/New_York"))
    book = Book()
    allowed = 0
    with open(args.leads, newline="") as handle:
        for row in csv.DictReader(handle):
            lead = Lead(
                id=row["id"],
                first=row["first"],
                last=row["last"],
                phone=row["phone"],
                timezone=row["timezone"],
                consent=row["consent"] == "yes",
                inquiry=row["inquiry"],
                inquiry_date=row["inquiry_date"],
            )
            if row["do_not_call"] == "yes":
                book.add_do_not_call(lead.phone, now, "earlier request")
            for stamp in filter(None, row["earlier_attempts"].split(";")):
                at, outcome = stamp.split("=")
                book.record_attempt(lead.id, datetime.fromisoformat(at), outcome)
            decision = may_call(lead, now, book, playbook)
            allowed += decision.allowed
            verdict = "CALL " if decision.allowed else "SKIP "
            print(f"{verdict} {lead.id}  {lead.first:<8} {decision.reason}")
    print(f"\n{allowed} leads may be called at {now:%A %d %B %Y, %H:%M %Z}.")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("callagent.api:app", host=args.host, port=args.port)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="callagent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("personas", help="build the simulated contacts").set_defaults(
        func=cmd_personas
    )

    sim = commands.add_parser("simulate", help="run the simulated calls (paid)")
    sim.add_argument("--set", choices=list(store.SETS), default="heldout")
    sim.add_argument("--mode", choices=["guarded", "prompt_only"], default="guarded")
    sim.add_argument("--model", default=DEFAULT_MODEL)
    sim.add_argument("--contact-model", default="claude-haiku-5-5")
    sim.add_argument("--budget", type=float, default=5.0)
    sim.add_argument("--limit", type=int, default=None)
    sim.add_argument("--parallel", type=int, default=6)
    sim.set_defaults(func=cmd_simulate)

    scoring = commands.add_parser("evaluate", help="score the recorded calls (free)")
    scoring.add_argument("--write", action="store_true", help="also update results/report.md")
    scoring.set_defaults(func=cmd_evaluate)

    campaign = commands.add_parser("campaign", help="which leads may be called now (free)")
    campaign.add_argument("--leads", default=str(ROOT / "data" / "leads.csv"))
    campaign.add_argument("--now", default="2026-11-18T10:30", help="ISO time, Eastern if bare")
    campaign.set_defaults(func=cmd_campaign)

    serving = commands.add_parser("serve", help="talk to the agent in a browser")
    serving.add_argument("--port", type=int, default=8030)
    serving.add_argument(
        "--host", default="127.0.0.1", help="0.0.0.0 to be reachable by a voice platform"
    )
    serving.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
