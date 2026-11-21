"""Command-line driver for the synthetic catalog/publisher.

Examples::

    python -m slabwater.cli run materials/772/input.json
    python -m slabwater.cli verify <db_dir> <plan_id>
    python -m slabwater.cli reopen <db_dir> <plan_id>

The ``run`` subcommand builds the catalog from the material's tile batches,
processes every request and prints a JSON report.  It is a demonstration
harness, not a service.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .experiment import ExperimentStore
from .publisher import Publisher
from .surveyor import (
    FeasibilityTable,
    SessionError,
    SessionStore,
    SurveyOrder,
    SurveySession,
    plan_survey,
)


def run_material(path: str | Path, db_dir: str | Path | None = None) -> dict[str, Any]:
    material = json.loads(Path(path).read_text(encoding="utf-8"))
    db_dir = Path(db_dir) if db_dir else Path(tempfile.mkdtemp(prefix="slabwater-"))
    db_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(db_dir / "catalog.db", material["height"], material["width"], material["ceiling"])
    publisher = Publisher(db_dir / "plans.db", catalog)
    report: dict[str, Any] = {"db_dir": str(db_dir), "batches": [], "requests": []}
    try:
        initial = catalog.apply_batch(material["initial_tiles"])
        report["batches"].append(
            {"name": "initial", "batch": initial.to_dict(), "version": initial.version}
        )
        if "correction" in material:
            correction = catalog.apply_batch([material["correction"]])
            report["batches"].append(
                {"name": "correction", "batch": correction.to_dict(), "version": correction.version}
            )
        if "invalid_correction" in material:
            invalid = catalog.apply_batch([material["invalid_correction"]])
            report["batches"].append(
                {
                    "name": "invalid_correction",
                    "batch": invalid.to_dict(),
                    "version": invalid.version,
                }
            )
        report["versions"] = catalog.list_versions()
        for request in material["requests"]:
            result = publisher.publish(request, default_outlets=material.get("outlets"))
            entry: dict[str, Any] = {
                "request": request,
                "status": result.status,
                "replayed": result.replayed,
                "plan_id": result.plan_id,
                "artifact": result.artifact,
            }
            if result.status == "rejected":
                stored = publisher.outcome_for_request(request["id"])
                entry["evidence"] = stored["payload"]["evidence"]
            if result.status == "error":
                entry["error"] = publisher.outcome_for_request(request["id"])["payload"]
            report["requests"].append(entry)
        # Reject-on-invalid surface check: the invalid correction must not have
        # produced a version; list_versions above is the evidence.
        report["plans"] = publisher.list_plans()
    finally:
        publisher.close()
        catalog.close()
    return report


def _load_orders(path: str | Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return raw["orders"]


def seal_experiment(db_dir: Path, spec_path: str | Path) -> dict[str, Any]:
    """Seal one experiment spec into the append-only experiment archive."""

    db_dir.mkdir(parents=True, exist_ok=True)
    store = ExperimentStore(db_dir / "experiments.db")
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        experiment = store.seal(spec)
        return {"ok": True, "experiment": experiment.to_dict()}
    finally:
        store.close()

def query_receipt(db_dir: Path, experiment_id: str, receipts: list[list[Any]]) -> dict[str, Any]:
    """Return every sealed slice still consistent with a reading receipt."""

    db_dir.mkdir(parents=True, exist_ok=True)
    store = ExperimentStore(db_dir / "experiments.db")
    try:
        experiment = store.load(experiment_id)
        parsed = []
        for point_id, raw_value in receipts:
            if point_id not in {point.id for point in experiment.points}:
                return {"ok": False, "code": "unknown_point",
                        "message": f"point {point_id} is not sealed in this experiment",
                        "details": {"point_id": point_id}}
            parsed.append((point_id, int(raw_value)))
        possible = experiment.possible_slices(parsed)
        return {
            "ok": True,
            "experiment_id": experiment_id,
            "receipt": [{"point_id": p, "value": v} for p, v in parsed],
            "possible_slices": list(possible),
            "out_of_draft": len(possible) == 0,
        }
    finally:
        store.close()


def plan_order(db_dir: Path, experiment_id: str, order_raw: dict[str, Any]) -> dict[str, Any]:
    """Build the minimax adaptive survey sheet for one budgeted order."""

    db_dir.mkdir(parents=True, exist_ok=True)
    store = ExperimentStore(db_dir / "experiments.db")
    try:
        experiment = store.load(experiment_id)
        order = SurveyOrder.from_dict(order_raw)
        table = FeasibilityTable(experiment, order)
        result = plan_survey(experiment, order, table)
        return result.to_dict()
    finally:
        store.close()


def _open_or_resume(
    sessions: SessionStore,
    experiment_id: str,
    order: SurveyOrder,
    result,
    session_id: str | None,
) -> SurveySession:
    if session_id is not None:
        return sessions.load_session(session_id)
    return sessions.open_session(experiment_id, order, result)


def session_command(
    db_dir: Path,
    experiment_id: str,
    order_raw: dict[str, Any],
    action: str,
    session_id: str | None = None,
    point_id: str | None = None,
    value: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Drive one survey session: open/peek/record/fork, with full persistence."""

    db_dir.mkdir(parents=True, exist_ok=True)
    experiments = ExperimentStore(db_dir / "experiments.db")
    sessions = SessionStore(db_dir / "sessions.db", experiments)
    try:
        experiment = experiments.load(experiment_id)
        if action == "resume":
            session = sessions.load_session(session_id)
            return {"ok": True, "session": session.to_dict(), "next": session.next_step(experiment)}
        order = SurveyOrder.from_dict(order_raw)
        result = plan_survey(experiment, order)
        session = _open_or_resume(sessions, experiment_id, order, result, session_id)
        if action == "open":
            sessions.save(session)
            return {"ok": True, "session": session.to_dict(), "next": session.next_step(experiment)}
        if action == "peek":
            return {"ok": True, "next": session.next_step(experiment),
                    "remaining": list(session.remaining_candidates(experiment))}
        if action == "record":
            state = session.record(experiment, point_id, int(value))
            sessions.save(session)
            return {"ok": session.status != "aborted", "state": state,
                    "session": session.to_dict()}
        if action == "fork":
            forked = session.fork(label or "fork")
            sessions.save(forked)
            return {"ok": True, "session": forked.to_dict(),
                    "next": forked.next_step(experiment)}
        raise ValueError(f"unknown session action: {action}")
    finally:
        sessions.close()
        experiments.close()


def run_survey_material(db_dir: Path, experiment_path: str | Path,
                        orders_path: str | Path) -> dict[str, Any]:
    """Seal an experiment and plan every order: a reproducible demo harness."""

    db_dir.mkdir(parents=True, exist_ok=True)
    experiments = ExperimentStore(db_dir / "experiments.db")
    report: dict[str, Any] = {"db_dir": str(db_dir)}
    try:
        spec = json.loads(Path(experiment_path).read_text(encoding="utf-8"))
        experiment = experiments.seal(spec)
        report["experiment"] = experiment.to_dict()
        report["orders"] = []
        for order_raw in _load_orders(orders_path):
            order = SurveyOrder.from_dict(order_raw)
            result = plan_survey(experiment, order)
            report["orders"].append(result.to_dict())
        return report
    finally:
        experiments.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slabwater", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run a material input file end to end")
    run_parser.add_argument("input", help="path to input.json")
    run_parser.add_argument("--db-dir", default=None, help="directory for catalog.db/plans.db")

    verify_parser = sub.add_parser("verify", help="verify a stored plan")
    verify_parser.add_argument("db_dir")
    verify_parser.add_argument("plan_id")

    reopen_parser = sub.add_parser("reopen", help="re-open a stored plan artifact")
    reopen_parser.add_argument("db_dir")
    reopen_parser.add_argument("plan_id")

    # --- survey subcommands ----------------------------------------------
    seal_parser = sub.add_parser("seal", help="seal a survey experiment spec")
    seal_parser.add_argument("db_dir")
    seal_parser.add_argument("spec")

    receipt_parser = sub.add_parser("receipt", help="query slices consistent with a receipt")
    receipt_parser.add_argument("db_dir")
    receipt_parser.add_argument("experiment_id")
    receipt_parser.add_argument(
        "--read", action="append", default=[], metavar="POINT=VALUE",
        help="one reading, repeatable; multiple reads form one receipt",
    )

    plan_parser = sub.add_parser("plan-order", help="build an adaptive survey sheet")
    plan_parser.add_argument("db_dir")
    plan_parser.add_argument("experiment_id")
    plan_parser.add_argument("order_json", help="path to a JSON file holding one order")

    sess_parser = sub.add_parser("session", help="open/drive a persistent survey session")
    sess_parser.add_argument("db_dir")
    sess_parser.add_argument("experiment_id")
    sess_parser.add_argument("order_json")
    sess_parser.add_argument(
        "--action", choices=["open", "peek", "record", "fork", "resume"], default="open")
    sess_parser.add_argument("--session-id", default=None)
    sess_parser.add_argument("--point", default=None)
    sess_parser.add_argument("--value", type=int, default=None)
    sess_parser.add_argument("--label", default=None)

    survey_parser = sub.add_parser("survey-demo", help="seal experiment and plan all its orders")
    survey_parser.add_argument("db_dir")
    survey_parser.add_argument("experiment_json")
    survey_parser.add_argument("orders_json")

    args = parser.parse_args(argv)
    if args.command == "run":
        report = run_material(args.input, args.db_dir)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.command == "seal":
        print(json.dumps(seal_experiment(Path(args.db_dir), args.spec), indent=2, ensure_ascii=False))
        return 0
    if args.command == "receipt":
        reads = [item.split("=", 1) for item in args.read]
        report = query_receipt(Path(args.db_dir), args.experiment_id, reads)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report.get("ok", True):
            return 3
        return 0 if not report["out_of_draft"] else 2
    if args.command == "plan-order":
        order_raw = json.loads(Path(args.order_json).read_text(encoding="utf-8"))
        report = plan_order(Path(args.db_dir), args.experiment_id, order_raw)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 2
    if args.command == "session":
        order_raw = json.loads(Path(args.order_json).read_text(encoding="utf-8"))
        try:
            report = session_command(
                Path(args.db_dir), args.experiment_id, order_raw, args.action,
                session_id=args.session_id, point_id=args.point, value=args.value,
                label=args.label,
            )
        except SessionError as exc:
            print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False))
            return 3
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("ok", True) else 2
    if args.command == "survey-demo":
        report = run_survey_material(
            Path(args.db_dir), args.experiment_json, args.orders_json)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    db_dir = Path(args.db_dir)
    catalog = Catalog.open(db_dir / "catalog.db")
    publisher = Publisher(db_dir / "plans.db", catalog)
    try:
        if args.command == "verify":
            result = publisher.verify(args.plan_id).to_dict()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["ok"] else 1
        reopened = publisher.reopen(args.plan_id)
        print(json.dumps(reopened.to_dict(), indent=2, ensure_ascii=False))
        return 0
    finally:
        publisher.close()
        catalog.close()


if __name__ == "__main__":
    sys.exit(main())
