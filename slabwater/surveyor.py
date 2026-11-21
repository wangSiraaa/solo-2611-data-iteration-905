"""Adaptive survey planner and step-by-step survey sessions.

Given a sealed :class:`~slabwater.experiment.Experiment` and a field order
(candidate outlets inherited from the stand, an upper bound on how many may be
opened, and required storage upper bounds on named cells), the planner builds a
*survey sheet*: a decision tree whose internal nodes re-measure one point and
whose leaves name an outlet set that is feasible for **every** slice still
consistent with the receipts so far.  The hidden slice never has to be
identified -- a common feasible set is enough to sign the release.

Costs are minimax: a node's cost is the chosen point's cost plus the most
expensive answer branch, and the planner minimizes this worst-branch cost with
deterministic tie-breaking.  Two distinct failure modes are reported:

* ``no_distinguishing_point`` -- even with unlimited budget, no remaining point
  can split the candidates toward common feasible sets; the receipts (and a
  witness pair) are the verifiable reason.
* ``insufficient_budget`` -- an adaptive sheet exists, but its worst-branch cost
  exceeds the budget; the achievable minimum cost is reported alongside.

Sessions make a sheet executable one reading at a time, persist every receipt,
stop (without signing) on a value no sealed slice produces, and can be forked so
the same experiment can be re-opened in another process and walked down a
different reading branch.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .experiment import (
    MAX_OUTLETS,
    Experiment,
    ExperimentError,
    ExperimentStore,
    _GridSurface,
)
from .model import Cell, Outlet, is_int, sha256_hex
from .solver import _INF, _materialize, _run_minimax, enumerate_subsets

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    order_hash   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# Field orders
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SurveyOrder:
    """One budgeted field order for an experiment."""

    order_id: str
    outlet_ids: tuple[str, ...]
    opens: int
    cell_max: tuple[tuple[Cell, int], ...]
    total_max: int | None = None
    budget: int | None = None
    allowed_points: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "outlet_ids": list(self.outlet_ids),
            "opens": self.opens,
            "cell_max": [
                {"cell": [r, c], "max": limit} for (r, c), limit in self.cell_max
            ],
            "total_max": self.total_max,
            "budget": self.budget,
            "allowed_points": list(self.allowed_points) if self.allowed_points is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SurveyOrder":
        if not isinstance(raw.get("order_id"), str) or not raw.get("order_id"):
            raise ExperimentError("bad_order", "order_id must be a non-empty string")
        outlet_ids = raw.get("outlet_ids")
        if not isinstance(outlet_ids, (list, tuple)) or not outlet_ids:
            raise ExperimentError("bad_order", "order must name at least one usable outlet")
        if len(outlet_ids) > MAX_OUTLETS:
            raise ExperimentError("bad_order", f"at most {MAX_OUTLETS} outlets")
        if len(set(outlet_ids)) != len(outlet_ids):
            raise ExperimentError("bad_order", "outlet ids must be unique")
        opens = raw.get("opens")
        if not is_int(opens) or opens < 0:
            raise ExperimentError("bad_order", "opens must be a non-negative integer")
        total_max = raw.get("total_max")
        if total_max is not None and (not is_int(total_max) or total_max < 0):
            raise ExperimentError("bad_order", "total_max must be a non-negative integer or null")
        budget = raw.get("budget")
        if budget is not None and (not is_int(budget) or budget < 0):
            raise ExperimentError("bad_order", "budget must be a non-negative integer or null")
        raw_limits = raw.get("cell_max", [])
        if not isinstance(raw_limits, list):
            raise ExperimentError("bad_order", "cell_max must be a list")
        cell_max: list[tuple[Cell, int]] = []
        for item in raw_limits:
            cell = item.get("cell")
            limit = item.get("max")
            if not isinstance(cell, (list, tuple)) or len(cell) != 2:
                raise ExperimentError("bad_order", "cell_max entry must use [row, col]")
            r, c = cell
            if not is_int(r) or not is_int(c) or not is_int(limit) or limit < 0:
                raise ExperimentError("bad_order", "cell_max cell and max must be non-negative integers")
            cell_max.append(((r, c), limit))
        allowed = raw.get("allowed_points")
        if allowed is not None:
            if not isinstance(allowed, (list, tuple)) or not allowed:
                raise ExperimentError("bad_order", "allowed_points must be a non-empty list or null")
            if len(set(allowed)) != len(allowed):
                raise ExperimentError("bad_order", "allowed_points must be unique")
            allowed_tuple = tuple(allowed)
        else:
            allowed_tuple = None
        return cls(
            order_id=raw["order_id"],
            outlet_ids=tuple(outlet_ids),
            opens=opens,
            cell_max=tuple(sorted(cell_max, key=lambda pair: (pair[0][0], pair[0][1]))),
            total_max=total_max,
            budget=budget,
            allowed_points=allowed_tuple,
        )


# ---------------------------------------------------------------------------
# Feasibility evaluation
# ---------------------------------------------------------------------------
class FeasibilityTable:
    """Per-slice feasibility of every allowed outlet subset for one order.

    Bottleneck maps are computed once per (slice, outlet) and combined per-cell
    by minimum, exactly like the solver's ``reuse`` strategy.  The table is the
    single source of truth the planner and the final signing checks share.
    """

    def __init__(self, experiment: Experiment, order: SurveyOrder):
        self.experiment = experiment
        self.order = order
        self.outlets = [experiment.outlet(oid) for oid in order.outlet_ids]
        unknown = set(order.outlet_ids) - {o.id for o in self.outlets}
        if unknown:
            raise ExperimentError(
                "unknown_outlet", f"order names outlets absent from the experiment",
                {"outlets": sorted(unknown)},
            )
        if order.opens > len(self.outlets):
            raise ExperimentError(
                "opens_exceeds_outlets",
                f"opens={order.opens} but only {len(self.outlets)} outlets are named",
            )
        for (r, c), _ in order.cell_max:
            if not (0 <= r < experiment.height and 0 <= c < experiment.width):
                raise ExperimentError(
                    "cell_out_of_bounds", f"limit cell ({r},{c}) is outside the surface"
                )
        self.subsets: list[tuple[Outlet, ...]] = list(
            enumerate_subsets(self.outlets, order.opens)
        )
        self._maps: dict[str, dict[str, tuple[int, ...]]] = {}
        self._feasible: dict[str, list[frozenset[str]]] = {}
        self._water: dict[tuple[str, frozenset[str]], dict[str, Any]] = {}
        self._build()

    def _bottleneck(self, slice_id: str, outlet: Outlet) -> tuple[int, ...]:
        per_slice = self._maps.setdefault(slice_id, {})
        cached = per_slice.get(outlet.id)
        if cached is None:
            surface = self.experiment.surface(slice_id)
            values, _ = _run_minimax(surface, [outlet])
            per_slice[outlet.id] = values
            return values
        return cached

    def _water_for(self, slice_id: str, subset: Sequence[Outlet]):
        surface = self.experiment.surface(slice_id)
        combined: list[int] | None = None
        for outlet in subset:
            values = self._bottleneck(slice_id, outlet)
            if combined is None:
                combined = list(values)
            else:
                for i, value in enumerate(values):
                    if value < combined[i]:
                        combined[i] = value
        if combined is None:
            combined = [_INF] * (experiment_size(surface))
        return _materialize(surface, combined, subset)

    def _build(self) -> None:
        for slice_id in self.experiment.slice_ids:
            ok_sets: list[frozenset[str]] = []
            for subset in self.subsets:
                water = self._water_for(slice_id, subset)
                violations = []
                if self.order.total_max is not None and water.total_storage > self.order.total_max:
                    violations.append(
                        {"constraint": "total", "limit": self.order.total_max,
                         "actual": water.total_storage}
                    )
                for (r, c), limit in self.order.cell_max:
                    actual = water.storage[r][c]
                    if actual > limit:
                        violations.append(
                            {"constraint": "cell", "cell": [r, c], "limit": limit, "actual": actual}
                        )
                key = frozenset(outlet.id for outlet in subset)
                self._water[(slice_id, key)] = {
                    "storage": [list(row) for row in water.storage],
                    "levels": [list(row) for row in water.levels],
                    "total_storage": water.total_storage,
                    "violations": violations,
                }
                if not violations:
                    ok_sets.append(key)
            self._feasible[slice_id] = ok_sets

    def feasible_sets(self, slice_id: str) -> list[frozenset[str]]:
        return self._feasible[slice_id]

    def common_feasible(self, candidate_ids: Sequence[str]) -> frozenset[str] | None:
        """An outlet subset feasible for every candidate, in deterministic order."""

        candidates = list(candidate_ids)
        if not candidates:
            return None
        intersection: frozenset[str] | None = None
        for slice_id in candidates:
            sets = set(self._feasible[slice_id])
            intersection = sets if intersection is None else intersection & sets
            if not intersection:
                return None
        # Subsets were enumerated by size then outlet id; preserve that order.
        for subset in self.subsets:
            key = frozenset(o.id for o in subset)
            if key in intersection:
                return key
        return None

    def water_report(self, slice_id: str, opened: frozenset[str]) -> dict[str, Any]:
        return self._water[(slice_id, opened)]


def experiment_size(surface: _GridSurface) -> int:
    return surface.height * surface.width


# ---------------------------------------------------------------------------
# Minimax adaptive planning
# ---------------------------------------------------------------------------
@dataclass
class PlanNode:
    """One node of the survey sheet (a leaf when ``point_id`` is None)."""

    candidate_ids: tuple[str, ...]
    point_id: str | None
    point_cost: int
    worst_cost: int
    release: tuple[str, ...] | None
    branches: dict[int, "PlanNode"] = field(default_factory=dict)

    def is_leaf(self) -> bool:
        return self.point_id is None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "candidates": list(self.candidate_ids),
            "point_id": self.point_id,
            "point_cost": self.point_cost,
            "worst_cost": self.worst_cost,
            "release": list(self.release) if self.release is not None else None,
            "branches": {str(value): node.to_dict() for value, node in sorted(self.branches.items())},
        }
        return data


@dataclass
class PlanResult:
    ok: bool
    reason: str | None
    node: PlanNode | None
    achievable_cost: int | None
    budget: int | None
    witnesses: list[dict[str, Any]]
    diagnostic: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "budget": self.budget,
            "achievable_cost": self.achievable_cost,
            "witnesses": self.witnesses,
            "diagnostic": self.diagnostic,
            "sheet": self.node.to_dict() if self.node is not None else None,
        }


def _indistinguishable(experiment: Experiment, candidates: Sequence[str],
                       usable_points: Sequence[Any]) -> list[tuple[str, str]]:
    """Pairs of candidates that give identical readings at every usable point."""

    pairs: list[tuple[str, str]] = []
    ids = list(candidates)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if all(experiment.reading(a, p.id) == experiment.reading(b, p.id) for p in usable_points):
                pairs.append((a, b))
    return pairs


def plan_survey(
    experiment: Experiment,
    order: SurveyOrder,
    table: FeasibilityTable | None = None,
) -> PlanResult:
    """Build the minimum worst-branch-cost adaptive survey sheet."""

    if table is None:
        table = FeasibilityTable(experiment, order)

    # The order may restrict which sealed points the crew is allowed to use.
    if order.allowed_points is not None:
        unknown = set(order.allowed_points) - {point.id for point in experiment.points}
        if unknown:
            raise ExperimentError(
                "unknown_point", "order names points absent from the experiment",
                {"points": sorted(unknown)},
            )
        usable_points = tuple(
            point for point in experiment.points if point.id in set(order.allowed_points)
        )
    else:
        usable_points = experiment.points

    # Every sealed slice must individually have at least one feasible set,
    # otherwise no amount of surveying can ever produce a valid release.
    hopeless = [sid for sid in experiment.slice_ids if not table.feasible_sets(sid)]
    if hopeless:
        witness = _no_feasible_witness(table, hopeless[0], order)
        diagnostic = {
            "code": "slice_without_feasible_outlet",
            "slices": hopeless,
            "explanation": "these slices satisfy no allowed outlet subset even when known exactly; "
                           "no measurement can help",
            "example": witness,
        }
        return PlanResult(False, "no_distinguishing_point", None, None, order.budget, [witness], diagnostic)

    memo: dict[tuple[frozenset[str], int | None], PlanNode | None] = {}

    def dp(candidates: tuple[str, ...], budget_left: int | None) -> PlanNode | None:
        key = (frozenset(candidates), budget_left)
        if key in memo:
            return memo[key]
        common = table.common_feasible(candidates)
        if common is not None:
            node = PlanNode(
                candidate_ids=tuple(sorted(candidates)),
                point_id=None,
                point_cost=0,
                worst_cost=0,
                release=tuple(sorted(common)),
            )
            memo[key] = node
            return node
        if budget_left is not None and budget_left <= 0:
            memo[key] = None
            return None
        best: PlanNode | None = None
        # Deterministic point ordering; cost tie-break handled explicitly below.
        for point in usable_points:
            if budget_left is not None and point.cost > budget_left:
                continue
            grouped: dict[int, list[str]] = {}
            for slice_id in candidates:
                value = experiment.reading(slice_id, point.id)
                grouped.setdefault(value, []).append(slice_id)
            if len(grouped) < 2:
                continue  # point carries no information on this candidate set
            child_budget = None if budget_left is None else budget_left - point.cost
            children: dict[int, PlanNode] = {}
            branch_worst = 0
            viable = True
            for value in sorted(grouped):
                child = dp(tuple(sorted(grouped[value])), child_budget)
                if child is None:
                    viable = False
                    break
                children[value] = child
                branch_worst = max(branch_worst, child.worst_cost)
            if not viable:
                continue
            worst = point.cost + branch_worst
            candidate_node = PlanNode(
                candidate_ids=tuple(sorted(candidates)),
                point_id=point.id,
                point_cost=point.cost,
                worst_cost=worst,
                release=None,
                branches=children,
            )
            if best is None or (worst, point.id) < (best.worst_cost, best.point_id):
                best = candidate_node
        memo[key] = best
        return best

    root_candidates = tuple(experiment.slice_ids)
    # First solve with unlimited budget to learn the true minimax cost.
    unlimited = dp(root_candidates, None)
    if unlimited is None:
        pairs = _indistinguishable(experiment, root_candidates, usable_points)
        witnesses = []
        for a, b in pairs:
            if table.common_feasible((a, b)) is None:
                witnesses.append(_pair_witness(experiment, table, order, a, b, usable_points))
        if not witnesses:
            # No single indistinguishable pair: report the stuck candidate set and
            # every remaining point's non-informative partition as the reason.
            witnesses.append(
                _stuck_witness(experiment, table, order, root_candidates, usable_points)
            )
        diagnostic = {
            "code": "no_distinguishing_point",
            "explanation": "no sequence of available points can separate the candidates into sets "
                           "that share a feasible outlet subset",
            "indistinguishable_pairs": [list(pair) for pair in pairs],
            "usable_points": [point.id for point in usable_points],
        }
        return PlanResult(False, "no_distinguishing_point", None, None, order.budget,
                          witnesses, diagnostic)

    achievable = unlimited.worst_cost
    if order.budget is not None:
        limited = dp(root_candidates, order.budget)
        if limited is None:
            diagnostic = {
                "code": "insufficient_budget",
                "explanation": "an adaptive sheet exists but its minimum worst-branch cost "
                               "exceeds the budget",
                "budget": order.budget,
                "achievable_cost": achievable,
                "shortfall": achievable - order.budget,
            }
            return PlanResult(False, "insufficient_budget", None, achievable, order.budget, [],
                              diagnostic)
        node = limited
    else:
        node = unlimited
    diagnostic = {
        "code": "feasible",
        "achievable_cost": achievable,
        "budget": order.budget,
        "within_budget": order.budget is None or achievable <= order.budget,
    }
    return PlanResult(True, None, node, achievable, order.budget, [], diagnostic)


def _pair_witness(experiment: Experiment, table: FeasibilityTable,
                  order: SurveyOrder, a: str, b: str,
                  usable_points: Sequence[Any]) -> dict[str, Any]:
    return {
        "type": "indistinguishable_pair",
        "slices": [a, b],
        "readings": [
            {"point_id": p.id, "values": [experiment.reading(a, p.id), experiment.reading(b, p.id)]}
            for p in usable_points
        ],
        "feasible_outlets": {
            a: [sorted(s) for s in table.feasible_sets(a)],
            b: [sorted(s) for s in table.feasible_sets(b)],
        },
        "common_feasible": None,
    }


def _stuck_witness(experiment: Experiment, table: FeasibilityTable,
                   order: SurveyOrder, candidates: Sequence[str],
                   usable_points: Sequence[Any]) -> dict[str, Any]:
    partitions = []
    for point in usable_points:
        grouped: dict[int, list[str]] = {}
        for slice_id in candidates:
            grouped.setdefault(experiment.reading(slice_id, point.id), []).append(slice_id)
        partitions.append({
            "point_id": point.id,
            "cost": point.cost,
            "partition": {str(value): sorted(ids) for value, ids in sorted(grouped.items())},
            "splits": len(grouped),
        })
    return {
        "type": "stuck_candidate_set",
        "candidates": list(candidates),
        "common_feasible": None,
        "point_partitions": partitions,
    }


def _no_feasible_witness(table: FeasibilityTable, slice_id: str, order: SurveyOrder) -> dict[str, Any]:
    attempts = []
    for subset in table.subsets:
        key = frozenset(o.id for o in subset)
        report = table.water_report(slice_id, key)
        attempts.append({
            "opened": sorted(key),
            "total_storage": report["total_storage"],
            "violations": report["violations"],
        })
    return {
        "type": "slice_without_feasible_outlet",
        "slice": slice_id,
        "order": order.to_dict(),
        "attempts": attempts,
        "exhaustive": True,
    }


# ---------------------------------------------------------------------------
# Persistent, forkable survey sessions
# ---------------------------------------------------------------------------
class SessionError(Exception):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message, "details": self.details}


class SessionStore:
    """SQLite persistence for executable survey sessions."""

    def __init__(self, path: str | Path, experiments: ExperimentStore):
        self.path = str(path)
        self.experiments = experiments
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open_session(self, experiment_id: str, order: SurveyOrder,
                     plan: PlanResult, label: str | None = None) -> "SurveySession":
        experiment = self.experiments.load(experiment_id)
        session_id = "S-" + sha256_hex(
            {"experiment_id": experiment_id, "order": order.to_dict(),
             "label": label or ""}
        )[:16]
        session = SurveySession(session_id, experiment_id, order, plan, receipts=[])
        row = self._conn.execute(
            "SELECT payload FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO sessions(session_id,experiment_id,order_hash,payload,status) "
                "VALUES (?,?,?,?,?)",
                (session_id, experiment_id, plan.diagnostic.get("code", ""),
                 json.dumps(session.to_dict(), sort_keys=True), session.status),
            )
        return session

    def save(self, session: "SurveySession") -> None:
        self._conn.execute(
            "INSERT INTO sessions(session_id,experiment_id,order_hash,payload,status) "
            "VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "payload=excluded.payload, status=excluded.status",
            (session.session_id, session.experiment_id,
             session.plan.diagnostic.get("code", ""),
             json.dumps(session.to_dict(), sort_keys=True), session.status),
        )

    def load_session(self, session_id: str) -> "SurveySession":
        row = self._conn.execute(
            "SELECT payload FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such session: {session_id}")
        return SurveySession.from_dict(json.loads(row[0]))

    def list_sessions(self) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT session_id, experiment_id, status FROM sessions ORDER BY session_id"
        ).fetchall()
        return [{"session_id": r[0], "experiment_id": r[1], "status": r[2]} for r in rows]


class SurveySession:
    """A walk down one branch of a survey sheet, driven by actual readings."""

    def __init__(self, session_id: str, experiment_id: str, order: SurveyOrder,
                 plan: PlanResult, receipts: list[dict[str, Any]]):
        self.session_id = session_id
        self.experiment_id = experiment_id
        self.order = order
        self.plan = plan
        self.receipts: list[dict[str, Any]] = list(receipts)
        self.status = "open"
        self.release: dict[str, Any] | None = None
        self.abort: dict[str, Any] | None = None
        for entry in self.receipts:
            if entry.get("abort"):
                self.status = "aborted"
                self.abort = entry
            if entry.get("release"):
                self.status = "signed"
                self.release = entry["release"]

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "experiment_id": self.experiment_id,
            "order": self.order.to_dict(),
            "plan": self.plan.to_dict(),
            "receipts": self.receipts,
            "status": self.status,
            "release": self.release,
            "abort": self.abort,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SurveySession":
        order = SurveyOrder.from_dict(raw["order"])
        plan_raw = raw["plan"]
        node = _node_from_dict(plan_raw["sheet"]) if plan_raw.get("sheet") else None
        plan = PlanResult(
            ok=bool(plan_raw["ok"]),
            reason=plan_raw.get("reason"),
            node=node,
            achievable_cost=plan_raw.get("achievable_cost"),
            budget=plan_raw.get("budget"),
            witnesses=plan_raw.get("witnesses", []),
            diagnostic=plan_raw.get("diagnostic", {}),
        )
        session = cls(
            session_id=raw["session_id"],
            experiment_id=raw["experiment_id"],
            order=order,
            plan=plan,
            receipts=raw.get("receipts", []),
        )
        session.status = raw.get("status", "open")
        session.release = raw.get("release")
        session.abort = raw.get("abort")
        return session

    def fork(self, label: str) -> "SurveySession":
        """Open an independent walk of the same sheet with empty receipts."""

        session_id = "S-" + sha256_hex(
            {"parent": self.session_id, "label": label, "kind": "fork"}
        )[:16]
        return SurveySession(session_id, self.experiment_id, self.order, self.plan, receipts=[])

    # -- execution ---------------------------------------------------------
    @property
    def spent(self) -> int:
        return sum(entry["cost"] for entry in self.receipts if "cost" in entry)

    def current_node(self) -> PlanNode:
        if self.plan.node is None:
            raise SessionError("plan_not_feasible", "the order has no executable survey sheet")
        node = self.plan.node
        for entry in self.receipts:
            value = entry["value"]
            if value not in node.branches:
                raise SessionError(
                    "session_drift", "receipts no longer match the planned tree"
                )
            node = node.branches[value]
        return node

    def remaining_candidates(self, experiment: Experiment) -> tuple[str, ...]:
        receipt = [(entry["point_id"], entry["value"]) for entry in self.receipts]
        return experiment.possible_slices(receipt)

    def next_step(self, experiment: Experiment) -> dict[str, Any]:
        """What the field should do next: measure a point or sign the release."""

        if self.status == "aborted":
            return {"action": "stopped", "reason": "out_of_draft_reading", "abort": self.abort}
        if self.status == "signed":
            return {"action": "done", "release": self.release}
        node = self.current_node()
        if node.is_leaf():
            return {
                "action": "sign",
                "open_outlets": list(node.release),
                "candidates": list(node.candidate_ids),
            }
        return {
            "action": "measure",
            "point_id": node.point_id,
            "cost": node.point_cost,
            "candidates": list(node.candidate_ids),
            "spent": self.spent,
            "budget": self.order.budget,
        }

    def record(self, experiment: Experiment, point_id: str, value: int) -> dict[str, Any]:
        """Apply one field reading and return the new state.

        A value no sealed slice gives at this point aborts the session: the
        release is never signed and the original receipts are retained verbatim.
        """

        if self.status != "open":
            raise SessionError("session_closed", f"session is already {self.status}")
        if not is_int(value) or value < 0:
            raise SessionError("bad_reading", "reading must be a non-negative integer")
        node = self.current_node()
        if node.is_leaf():
            raise SessionError("no_measurement_due", "the sheet already reached a signing leaf")
        if point_id != node.point_id:
            raise SessionError(
                "unexpected_point",
                "the sheet requires another point now",
                {"required": node.point_id, "given": point_id},
            )
        point = experiment.point(point_id)
        # Out-of-draft value: stop and keep every prior receipt.
        if value not in experiment.observed_values(point_id):
            entry = {
                "point_id": point_id,
                "value": value,
                "cost": point.cost,
                "abort": True,
                "possible_slices": [],
                "observed_values": sorted(experiment.observed_values(point_id)),
            }
            self.receipts.append(entry)
            self.abort = entry
            self.status = "aborted"
            return {"action": "stopped", "reason": "out_of_draft_reading", "entry": entry}
        if value not in node.branches:
            # Defensive: the value is known to some slice but excluded by receipts.
            entry = {
                "point_id": point_id,
                "value": value,
                "cost": point.cost,
                "abort": True,
                "possible_slices": [],
            }
            self.receipts.append(entry)
            self.abort = entry
            self.status = "aborted"
            return {"action": "stopped", "reason": "inconsistent_with_receipts", "entry": entry}
        child = node.branches[value]
        entry = {
            "point_id": point_id,
            "value": value,
            "cost": point.cost,
            "possible_slices": list(child.candidate_ids),
        }
        self.receipts.append(entry)
        if child.is_leaf():
            # Keep the release receipt free of the (self-referential) full
            # receipt list; the session holds the authoritative receipts.
            self.release = {
                "open_outlets": list(child.release),
                "candidates": list(child.candidate_ids),
                "receipt_count": len(self.receipts),
                "total_cost": self.spent,
            }
            entry["release"] = self.release
            self.status = "signed"
            return {"action": "signed", "release": self.release}
        return {"action": "measured", "entry": entry, "next": self.next_step(experiment)}


def _node_from_dict(raw: Mapping[str, Any]) -> PlanNode:
    node = PlanNode(
        candidate_ids=tuple(raw["candidates"]),
        point_id=raw.get("point_id"),
        point_cost=raw.get("point_cost", 0),
        worst_cost=raw["worst_cost"],
        release=tuple(raw["release"]) if raw.get("release") is not None else None,
        branches={},
    )
    node.branches = {int(value): _node_from_dict(child) for value, child in raw["branches"].items()}
    return node
