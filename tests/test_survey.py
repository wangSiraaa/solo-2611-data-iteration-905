"""Tests for sealed survey experiments and the adaptive survey planner.

Covers:

* exact no-outlet readings recomputed by the shared solver engine;
* the 1x5 statement demo (one cheap reading, middle-only impossibility, the
  relaxed limit that needs no measurement);
* the 2x3 two-layer demo whose second point depends on the first answer;
* the two distinct failure reasons (no distinguishing point vs. budget);
* step-by-step sessions, persistence, forking and out-of-draft aborts;
* the hard 16-slice / 12-point / 8-outlet limits and append-only sealing.

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slabwater.experiment import (
    MAX_OUTLETS,
    MAX_POINTS,
    MAX_SLICES,
    ExperimentError,
    ExperimentStore,
    build_experiment,
)
from slabwater.surveyor import (
    FeasibilityTable,
    PlanNode,
    SessionError,
    SessionStore,
    SurveyOrder,
    plan_survey,
)

ROOT = Path(__file__).resolve().parent.parent
SURVEY = ROOT / "materials" / "survey"


def load(name: str):
    return json.loads((SURVEY / name).read_text(encoding="utf-8"))


T001_SPEC = load("T001_experiment.json")
T001_ORDERS = load("T001_orders.json")["orders"]
T002_SPEC = load("T002_experiment.json")
T002_ORDERS = load("T002_orders.json")["orders"]


class ExperimentTests(unittest.TestCase):
    def test_readings_are_sealed_storage(self):
        exp = build_experiment(T001_SPEC)
        # ceiling 3, no outlet: reading = ceiling - ground height.
        self.assertEqual(exp.reading("A", "P1"), 3)  # height 0
        self.assertEqual(exp.reading("A", "P2"), 1)  # height 2
        self.assertEqual(exp.reading("B", "P1"), 1)
        self.assertEqual(exp.reading("B", "P2"), 3)
        # The middle cell reads identically on both drafts: it cannot separate them.
        self.assertEqual(exp.reading("A", "PM"), exp.reading("B", "PM"))

    def test_receipt_query_keeps_all_possibilities_until_ruled_out(self):
        exp = build_experiment(T001_SPEC)
        self.assertEqual(exp.possible_slices(()), ("A", "B"))
        self.assertEqual(exp.possible_slices([("P1", 3)]), ("A",))
        self.assertEqual(exp.possible_slices([("P1", 1)]), ("B",))
        # A value no draft produces: empty, never a guess.
        self.assertEqual(exp.possible_slices([("P1", 99)]), ())

    def test_declared_readings_must_match_recomputed_ones(self):
        spec = json.loads(json.dumps(T001_SPEC))
        spec["readings"] = {"A": {"P1": 0, "P2": 1, "PM": 3},
                            "B": {"P1": 1, "P2": 3, "PM": 3}}
        with self.assertRaises(ExperimentError) as ctx:
            build_experiment(spec)
        self.assertEqual(ctx.exception.code, "readings_mismatch")

    def test_duplicate_surface_rejected(self):
        spec = json.loads(json.dumps(T001_SPEC))
        spec["slices"][1]["heights"] = spec["slices"][0]["heights"]
        with self.assertRaises(ExperimentError) as ctx:
            build_experiment(spec)
        self.assertEqual(ctx.exception.code, "duplicate_surface")

    def test_point_cost_must_be_positive(self):
        spec = json.loads(json.dumps(T001_SPEC))
        spec["points"][0]["cost"] = 0
        with self.assertRaises(ExperimentError):
            build_experiment(spec)

    def test_hard_limits_enforced(self):
        too_many_slices = {
            "experiment_id": "LIMIT-SLICES",
            "height": 1, "width": 5, "ceiling": 1,
            # 17 unique binary 1x5 grids: enough distinct surfaces to exceed 16.
            "slices": [
                {"id": f"S{i:02d}", "heights": [[(i >> k) & 1 for k in range(5)]]}
                for i in range(MAX_SLICES + 1)
            ],
            "points": [{"id": "P", "cell": [0, 0], "cost": 1}],
            "outlets": [{"id": "L", "cell": [0, 0], "sill": 0}],
        }
        with self.assertRaises(ExperimentError) as ctx:
            build_experiment(too_many_slices)
        self.assertEqual(ctx.exception.code, "too_many_slices")
        too_many_points = {
            "experiment_id": "LIMIT-POINTS",
            "height": 1, "width": MAX_POINTS + 1, "ceiling": 1,
            "slices": [{"id": "X", "heights": [[0] * (MAX_POINTS + 1)]}],
            "points": [
                {"id": f"P{i}", "cell": [0, i], "cost": 1} for i in range(MAX_POINTS + 1)
            ],
            "outlets": [{"id": "L", "cell": [0, 0], "sill": 0}],
        }
        with self.assertRaises(ExperimentError) as ctx:
            build_experiment(too_many_points)
        self.assertEqual(ctx.exception.code, "too_many_points")
        too_many_outlets = {
            "experiment_id": "LIMIT-OUTLETS",
            "height": 1, "width": MAX_OUTLETS + 1, "ceiling": 1,
            "slices": [{"id": "X", "heights": [[0] * (MAX_OUTLETS + 1)]}],
            "points": [{"id": "P", "cell": [0, 0], "cost": 1}],
            "outlets": [
                {"id": f"O{i}", "cell": [0, i], "sill": 0} for i in range(MAX_OUTLETS + 1)
            ],
        }
        with self.assertRaises(ExperimentError) as ctx:
            build_experiment(too_many_outlets)
        self.assertEqual(ctx.exception.code, "bad_outlet")


class SealedStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ExperimentStore(Path(self.tmp.name) / "exp.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_seal_is_append_only_and_replays_identical_content(self):
        first = self.store.seal(T001_SPEC)
        again = self.store.seal(T001_SPEC)
        self.assertEqual(first.content_hash, again.content_hash)
        self.assertEqual(self.store.list_experiments(), ["T001"])
        changed = json.loads(json.dumps(T001_SPEC))
        changed["ceiling"] = 4
        with self.assertRaises(ExperimentError) as ctx:
            self.store.seal(changed)
        self.assertEqual(ctx.exception.code, "experiment_id_conflict")
        # Original record untouched.
        self.assertEqual(self.store.load("T001").ceiling, 3)


class OneByFiveDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exp = build_experiment(T001_SPEC)

    def plan(self, index: int) -> PlanNode:
        order = SurveyOrder.from_dict(T001_ORDERS[index])
        return plan_survey(self.exp, order)

    def test_one_cheap_reading_picks_left_or_right(self):
        result = self.plan(0)  # middle cell must stay dry, budget 1
        self.assertTrue(result.ok)
        self.assertEqual(result.achievable_cost, 1)
        root = result.node
        self.assertEqual(root.point_id, "P1")
        # Reading 1 -> draft B -> right outlet; reading 3 -> draft A -> left.
        self.assertEqual(root.branches[1].release, ("R",))
        self.assertEqual(root.branches[3].release, ("L",))
        for leaf in root.branches.values():
            self.assertTrue(leaf.is_leaf())

    def test_middle_only_cannot_guarantee_at_any_budget(self):
        result = self.plan(1)  # allowed_points = [PM], huge budget
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_distinguishing_point")
        witness = result.witnesses[0]
        self.assertEqual(witness["type"], "indistinguishable_pair")
        self.assertEqual(witness["slices"], ["A", "B"])
        self.assertEqual(witness["readings"][0]["values"], [3, 3])
        self.assertIsNone(witness["common_feasible"])
        # The diagnostic must name the only point the crew was allowed to use.
        self.assertEqual(result.diagnostic["usable_points"], ["PM"])

    def test_relaxed_limit_shares_one_outlet_without_measurement(self):
        result = self.plan(2)  # middle cell may hold up to 2, budget 0
        self.assertTrue(result.ok)
        self.assertEqual(result.achievable_cost, 0)
        self.assertTrue(result.node.is_leaf())
        self.assertEqual(result.node.release, ("L",))

    def test_budget_shortfall_is_distinguished_from_missing_points(self):
        order = SurveyOrder.from_dict(
            {"order_id": "B0", "outlet_ids": ["L", "R"], "opens": 1,
             "cell_max": [{"cell": [0, 2], "max": 0}], "budget": 0}
        )
        result = plan_survey(self.exp, order)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "insufficient_budget")
        self.assertEqual(result.achievable_cost, 1)
        self.assertEqual(result.diagnostic["shortfall"], 1)


class TwoLayerDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exp = build_experiment(T002_SPEC)

    def test_needs_two_layers_and_second_point_varies_with_first_answer(self):
        order = SurveyOrder.from_dict(T002_ORDERS[0])  # budget 2
        result = plan_survey(self.exp, order)
        self.assertTrue(result.ok)
        self.assertEqual(result.achievable_cost, 2)
        root = result.node
        self.assertEqual(root.point_id, "Q00")
        self.assertEqual(set(root.branches), {2, 5, 6})
        second_points = {value: node.point_id for value, node in root.branches.items()}
        # Answer 2 continues at Q01; answers 5 and 6 continue at Q02.
        self.assertEqual(second_points, {2: "Q01", 5: "Q02", 6: "Q02"})
        self.assertEqual(len(set(second_points.values())), 2)
        # Every branch is itself one layer deep before reaching a signing leaf.
        for node in root.branches.values():
            self.assertFalse(node.is_leaf())
            self.assertEqual(node.worst_cost, 1)
            self.assertTrue(all(child.is_leaf() for child in node.branches.values()))
        # Spot-check the leaves named in the searched fixture.
        self.assertEqual(root.branches[2].branches[2].release, ("O2",))
        self.assertEqual(root.branches[2].branches[4].release, ("O3",))
        self.assertEqual(root.branches[6].branches[4].release, ("O0",))
        self.assertEqual(root.branches[6].branches[5].release, ("O1",))

    def test_no_single_measurement_suffices(self):
        order = SurveyOrder.from_dict(T002_ORDERS[0])
        # Budget 1: the tree genuinely needs depth 2, so this is a budget failure.
        limited = plan_survey(self.exp, SurveyOrder.from_dict(T002_ORDERS[1]))
        self.assertFalse(limited.ok)
        self.assertEqual(limited.reason, "insufficient_budget")
        self.assertEqual(limited.achievable_cost, 2)
        # Independently, no root point splits into immediately-common groups.
        table = FeasibilityTable(self.exp, order)
        for point in self.exp.points:
            grouped: dict[int, list[str]] = {}
            for slice_id in self.exp.slice_ids:
                grouped.setdefault(self.exp.reading(slice_id, point.id), []).append(slice_id)
            self.assertTrue(
                all(table.common_feasible(ids) is None for ids in grouped.values() if len(ids) > 1),
                f"point {point.id} unexpectedly resolves in one layer",
            )

    def test_every_leaf_release_is_feasible_for_all_remaining_slices(self):
        order = SurveyOrder.from_dict(T002_ORDERS[0])
        result = plan_survey(self.exp, order)
        table = FeasibilityTable(self.exp, order)

        def walk(node: PlanNode):
            if node.is_leaf():
                opened = frozenset(node.release)
                for slice_id in node.candidate_ids:
                    self.assertIn(opened, table.feasible_sets(slice_id))
                return
            for child in node.branches.values():
                walk(child)

        walk(result.node)


class MinimaxCostTests(unittest.TestCase):
    def test_worst_branch_cost_scales_and_accumulates(self):
        spec = json.loads(json.dumps(T002_SPEC))
        for point in spec["points"]:
            point["cost"] = 3
        exp = build_experiment(spec)
        order = SurveyOrder.from_dict(T002_ORDERS[0])  # budget 2
        # The same two-layer tree now costs 6, which the budget of 2 cannot cover.
        result = plan_survey(exp, order)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "insufficient_budget")
        self.assertEqual(result.achievable_cost, 6)
        # The unconstrained sheet keeps the same structure with per-node costs.
        unlimited = SurveyOrder.from_dict({**T002_ORDERS[0], "order_id": "U", "budget": None})
        tree = plan_survey(exp, unlimited).node
        self.assertEqual(tree.point_id, "Q00")
        self.assertEqual(tree.point_cost, 3)
        self.assertEqual(tree.branches[2].point_cost, 3)
        self.assertEqual(tree.branches[2].worst_cost, 3)
        self.assertEqual(tree.worst_cost, 6)

    def test_cheaper_of_two_equivalent_first_points_is_chosen(self):
        # Two points that split the same candidate groups: the planner must take
        # the cheaper one.  T001's P2 separates A/B exactly like P1 (readings
        # swapped); raise P1's price and P2 should become the root.
        spec = json.loads(json.dumps(T001_SPEC))
        for point in spec["points"]:
            if point["id"] == "P1":
                point["cost"] = 7
        exp = build_experiment(spec)
        order = SurveyOrder.from_dict({
            "order_id": "PRICE", "outlet_ids": ["L", "R"], "opens": 1,
            "cell_max": [{"cell": [0, 2], "max": 0}], "budget": 7,
        })
        result = plan_survey(exp, order)
        self.assertTrue(result.ok)
        self.assertEqual(result.node.point_id, "P2")
        self.assertEqual(result.achievable_cost, 1)


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.experiments = ExperimentStore(self.dir / "exp.db")
        self.exp = self.experiments.seal(T002_SPEC)
        self.order = SurveyOrder.from_dict(T002_ORDERS[0])
        self.plan = plan_survey(self.exp, self.order)
        self.sessions = SessionStore(self.dir / "sess.db", self.experiments)

    def tearDown(self) -> None:
        self.sessions.close()
        self.experiments.close()
        self.tmp.cleanup()

    def test_walk_signs_and_persists_receipts(self):
        session = self.sessions.open_session("T002", self.order, self.plan)
        self.assertEqual(session.next_step(self.exp)["point_id"], "Q00")
        session.record(self.exp, "Q00", 2)
        session.record(self.exp, "Q01", 4)
        self.assertEqual(session.status, "signed")
        self.assertEqual(session.release["open_outlets"], ["O3"])
        self.assertEqual(session.spent, 2)
        self.sessions.save(session)
        # Reopen as if in another process; receipts and status survive.
        reopened = self.sessions.load_session(session.session_id)
        self.assertEqual(reopened.status, "signed")
        self.assertEqual(len(reopened.receipts), 2)
        self.assertEqual(reopened.release["open_outlets"], ["O3"])

    def test_fork_walks_a_different_branch_with_same_material(self):
        session = self.sessions.open_session("T002", self.order, self.plan)
        session.record(self.exp, "Q00", 2)
        session.record(self.exp, "Q01", 4)
        self.sessions.save(session)
        forked = session.fork("answer-5")
        self.sessions.save(forked)
        reloaded = self.sessions.load_session(forked.session_id)
        self.assertEqual(reloaded.receipts, [])
        reloaded.record(self.exp, "Q00", 5)
        self.assertEqual(reloaded.next_step(self.exp)["point_id"], "Q02")
        reloaded.record(self.exp, "Q02", 5)
        self.assertEqual(reloaded.release["open_outlets"], ["O1"])
        # The original branch is untouched.
        self.assertEqual(self.sessions.load_session(session.session_id).release["open_outlets"], ["O3"])

    def test_out_of_draft_reading_aborts_and_keeps_receipts(self):
        session = self.sessions.open_session("T002", self.order, self.plan)
        state = session.record(self.exp, "Q00", 99)
        self.assertEqual(state["action"], "stopped")
        self.assertEqual(session.status, "aborted")
        # No release is ever signed; the original receipt stays in the record.
        self.assertIsNone(session.release)
        self.assertEqual(session.receipts[0]["value"], 99)
        self.assertEqual(session.receipts[0]["observed_values"], [2, 5, 6])
        self.sessions.save(session)
        reloaded = self.sessions.load_session(session.session_id)
        self.assertEqual(reloaded.status, "aborted")
        with self.assertRaises(SessionError):
            reloaded.record(self.exp, "Q02", 2)

    def test_wrong_point_rejected(self):
        session = self.sessions.open_session("T002", self.order, self.plan)
        with self.assertRaises(SessionError) as ctx:
            session.record(self.exp, "Q02", 2)
        self.assertEqual(ctx.exception.code, "unexpected_point")
        self.assertEqual(session.receipts, [])


class InfeasibleSliceTests(unittest.TestCase):
    def test_slice_without_any_outlet_is_reported_with_exhaustive_evidence(self):
        # One-cell surface that must stay dry.  Ground height 1 with sill 2 pins
        # the level at 2, so storage is 1 and the only outlet cannot help.
        spec = {
            "experiment_id": "HOPELESS",
            "height": 1, "width": 1, "ceiling": 2,
            "slices": [{"id": "H", "heights": [[1]]}],
            "points": [{"id": "P", "cell": [0, 0], "cost": 1}],
            "outlets": [{"id": "L", "cell": [0, 0], "sill": 2}],
        }
        exp = build_experiment(spec)
        order = SurveyOrder.from_dict({
            "order_id": "X", "outlet_ids": ["L"], "opens": 1,
            "cell_max": [{"cell": [0, 0], "max": 0}], "budget": 10,
        })
        result = plan_survey(exp, order)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "no_distinguishing_point")
        witness = result.witnesses[0]
        self.assertTrue(witness["exhaustive"])
        self.assertTrue(witness["attempts"])

    def test_indistinguishable_slices_that_share_a_feasible_set_need_no_measurement(self):
        # Two surfaces that read identically at the only point, but one outlet
        # set satisfies both: the planner must release immediately, never report
        # a missing distinguishing point.
        spec = {
            "experiment_id": "SHARED",
            "height": 1, "width": 2, "ceiling": 3,
            # Cell 0 has height 0 on both drafts (identical reading there); the
            # drafts differ only at cell 1, which is never offered as a point.
            "slices": [
                {"id": "A", "heights": [[0, 0]]},
                {"id": "B", "heights": [[0, 1]]},
            ],
            "points": [{"id": "P0", "cell": [0, 0], "cost": 1}],
            "outlets": [
                {"id": "L", "cell": [0, 0], "sill": 0},
                {"id": "R", "cell": [0, 1], "sill": 0},
            ],
        }
        exp = build_experiment(spec)
        # The single point cannot separate the drafts by construction.
        self.assertEqual(exp.reading("A", "P0"), exp.reading("B", "P0"))
        order = SurveyOrder.from_dict({
            "order_id": "SH", "outlet_ids": ["L", "R"], "opens": 1,
            "cell_max": [{"cell": [0, 0], "max": 2}, {"cell": [0, 1], "max": 2}],
            "budget": 0,
        })
        result = plan_survey(exp, order)
        self.assertTrue(result.ok)
        self.assertEqual(result.achievable_cost, 0)
        self.assertTrue(result.node.is_leaf())


class CrossEngineTests(unittest.TestCase):
    def test_feasibility_table_matches_the_published_solver(self):
        from slabwater.solver import solve

        exp = build_experiment(T002_SPEC)
        order = SurveyOrder.from_dict(T002_ORDERS[0])
        table = FeasibilityTable(exp, order)
        outlets = [exp.outlet(oid) for oid in order.outlet_ids]
        for slice_id in exp.slice_ids:
            surface = exp.surface(slice_id)
            result = solve(
                surface=surface, outlets=outlets, opens=order.opens,
                total_max=10**9, cell_max=order.cell_max,
                strategy="recompute", stop_on_first=False,
            )
            solver_sets = {
                frozenset(attempt.opened)
                for attempt in result.attempts if attempt.feasible
            }
            self.assertEqual(set(table.feasible_sets(slice_id)), solver_sets, slice_id)


if __name__ == "__main__":
    unittest.main()
