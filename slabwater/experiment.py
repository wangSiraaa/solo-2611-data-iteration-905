"""Sealed survey experiments: candidate slices, re-measurement points, readings.

An experiment extends the append-only :mod:`slabwater.catalog` idea to the
"which survey draft was actually laid" problem.  Under one experiment id it
freezes:

* a finite set of candidate elevation slices (each a fully-covered synthetic
  H x W grid; each cell carries the same geometry it would have in a catalog
  slice, so a reading receipt can name a concrete cell);
* a finite set of re-measurement points, each with a positive integer cost;
* the exact reading every slice gives at every point.

A reading is the *storage* (water level minus ground height) at that cell when
no outlet has been opened yet, i.e. every cell ponded up to the ceiling.  This
is a property of the sealed slice alone, so one receipt can be queried against
the archive without any field order.  Only synthetic grids are stored here.

The store is SQLite-backed and append-only: an experiment id, once sealed, can
never be overwritten.  Re-creating the same id with identical content replays
the stored record; different content is an error.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import Cell, Outlet, is_int, sha256_hex
from .solver import _INF, _materialize, _run_minimax

# Hard limits fixed by the task statement.
MAX_SLICES = 16
MAX_POINTS = 12
MAX_OUTLETS = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class ExperimentError(Exception):
    """Raised when an experiment definition is invalid or an id is reused."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message, "details": self.details}


class _GridSurface:
    """Minimal surface accepted by the solver's minimax/materialize helpers."""

    def __init__(self, heights: Sequence[Sequence[int]], ceiling: int):
        self.height = len(heights)
        self.width = len(heights[0])
        self.ceiling = ceiling
        self.heights = tuple(tuple(int(v) for v in row) for row in heights)
        self.elevation_hash = sha256_hex(
            {"height": self.height, "width": self.width, "ceiling": ceiling,
             "heights": [list(r) for r in self.heights]}
        )


def sealed_reading(surface: _GridSurface, cell: Cell) -> int:
    """Storage at ``cell`` with no outlet open: every cell ponded to the ceiling.

    With an empty outlet set the minimax map is all ``+inf`` and the
    materializer substitutes the ceiling, so ``reading = ceiling - height``.
    The solver is still used (rather than subtraction alone) so the reading is
    produced by the exact same engine that evaluates opened-outlet plans.
    """

    dist, _ = _run_minimax(surface, ())
    assert all(value == _INF for value in dist)
    water = _materialize(surface, dist, ())
    return water.storage_at(cell[0], cell[1])


@dataclass(frozen=True)
class SurveyPoint:
    """A re-measurement point: a cell and a positive integer survey cost."""

    id: str
    cell: Cell
    cost: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "cell": [self.cell[0], self.cell[1]], "cost": self.cost}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SurveyPoint":
        if not isinstance(raw.get("id"), str) or not raw.get("id"):
            raise ExperimentError("bad_point", "point id must be a non-empty string")
        cell = raw.get("cell")
        if not isinstance(cell, (list, tuple)) or len(cell) != 2:
            raise ExperimentError("bad_point", f"point {raw['id']} cell must be [row, col]")
        row, col = cell
        if not is_int(row) or not is_int(col):
            raise ExperimentError("bad_point", f"point {raw['id']} coordinates must be integers")
        cost = raw.get("cost")
        if not is_int(cost) or cost <= 0:
            raise ExperimentError("bad_point", f"point {raw['id']} cost must be a positive integer")
        return cls(id=raw["id"], cell=(row, col), cost=cost)


@dataclass(frozen=True)
class Experiment:
    """A sealed archive of slices, points, outlets and exact readings."""

    experiment_id: str
    height: int
    width: int
    ceiling: int
    slices: tuple[tuple[str, tuple[tuple[int, ...], ...], str], ...]
    points: tuple[SurveyPoint, ...]
    outlets: tuple[Outlet, ...]
    readings: Mapping[str, Mapping[str, int]]
    content_hash: str = ""

    # -- views -------------------------------------------------------------
    @property
    def slice_ids(self) -> tuple[str, ...]:
        return tuple(slice_id for slice_id, _, _ in self.slices)

    def surface(self, slice_id: str) -> _GridSurface:
        for sid, heights, _ in self.slices:
            if sid == slice_id:
                return _GridSurface(heights, self.ceiling)
        raise KeyError(f"no such slice: {slice_id}")

    def heights_of(self, slice_id: str) -> tuple[tuple[int, ...], ...]:
        return self.surface(slice_id).heights

    def point(self, point_id: str) -> SurveyPoint:
        for point in self.points:
            if point.id == point_id:
                return point
        raise KeyError(f"no such point: {point_id}")

    def outlet(self, outlet_id: str) -> Outlet:
        for outlet in self.outlets:
            if outlet.id == outlet_id:
                return outlet
        raise KeyError(f"no such outlet: {outlet_id}")

    def reading(self, slice_id: str, point_id: str) -> int:
        return int(self.readings[slice_id][point_id])

    def possible_slices(self, receipt: Sequence[tuple[str, int]]) -> tuple[str, ...]:
        """Slice ids still consistent with every (point_id, value) receipt.

        Information never removes a possibility unless a reading rules it out,
        so slices absent from every receipt entry are retained.  A value that no
        slice produces at that point yields the empty tuple -- the caller treats
        that as an out-of-draft reading and must stop issuing.
        """

        receipt = tuple(receipt)
        result = []
        for slice_id in self.slice_ids:
            if all(self.reading(slice_id, point_id) == value for point_id, value in receipt):
                result.append(slice_id)
        return tuple(result)

    def observed_values(self, point_id: str) -> frozenset[int]:
        return frozenset(self.reading(slice_id, point_id) for slice_id in self.slice_ids)

    # -- (de)serialization -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "height": self.height,
            "width": self.width,
            "ceiling": self.ceiling,
            "slices": [
                {"id": slice_id, "heights": [list(row) for row in heights], "surface_hash": h}
                for slice_id, heights, h in self.slices
            ],
            "points": [point.to_dict() for point in self.points],
            "outlets": [outlet.to_dict() for outlet in self.outlets],
            "readings": {
                slice_id: {point_id: value for point_id, value in sorted(by_point.items())}
                for slice_id, by_point in self.readings.items()
            },
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Experiment":
        slices = tuple(
            (item["id"], tuple(tuple(int(v) for v in row) for row in item["heights"]),
             item.get("surface_hash", ""))
            for item in raw["slices"]
        )
        points = tuple(SurveyPoint.from_dict(item) for item in raw["points"])
        outlets = tuple(Outlet.from_dict(item) for item in raw["outlets"])
        readings = {
            slice_id: {point_id: int(value) for point_id, value in by_point.items()}
            for slice_id, by_point in raw["readings"].items()
        }
        return cls(
            experiment_id=raw["experiment_id"],
            height=int(raw["height"]),
            width=int(raw["width"]),
            ceiling=int(raw["ceiling"]),
            slices=slices,
            points=points,
            outlets=outlets,
            readings=readings,
            content_hash=raw.get("content_hash", ""),
        )


class ExperimentStore:
    """Append-only SQLite archive of sealed experiments."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ExperimentStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reads -------------------------------------------------------------
    def list_experiments(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT experiment_id FROM experiments ORDER BY experiment_id"
        ).fetchall()
        return [row[0] for row in rows]

    def load(self, experiment_id: str) -> Experiment:
        row = self._conn.execute(
            "SELECT payload FROM experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such experiment: {experiment_id}")
        return Experiment.from_dict(json.loads(row[0]))

    def exists(self, experiment_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone() is not None

    # -- writes ------------------------------------------------------------
    def seal(self, spec: Mapping[str, Any]) -> Experiment:
        """Validate and seal one experiment, or replay an identical one."""

        experiment = build_experiment(spec)
        row = self._conn.execute(
            "SELECT payload, content_hash FROM experiments WHERE experiment_id=?",
            (experiment.experiment_id,),
        ).fetchone()
        if row is not None:
            if row[1] != experiment.content_hash:
                raise ExperimentError(
                    "experiment_id_conflict",
                    f"experiment {experiment.experiment_id} is already sealed with different content",
                    {"experiment_id": experiment.experiment_id},
                )
            return Experiment.from_dict(json.loads(row[0]))
        self._conn.execute(
            "INSERT INTO experiments(experiment_id,payload,content_hash) VALUES (?,?,?)",
            (
                experiment.experiment_id,
                json.dumps(experiment.to_dict(), sort_keys=True, ensure_ascii=False),
                experiment.content_hash,
            ),
        )
        return experiment


def _parse_grid(raw: Any, height: int, width: int, ceiling: int, where: str) -> tuple[tuple[int, ...], ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) != height:
        raise ExperimentError("bad_slice", f"{where} must be a {height}x{width} grid")
    grid: list[tuple[int, ...]] = []
    for r, line in enumerate(raw):
        if not isinstance(line, (list, tuple)) or len(line) != width:
            raise ExperimentError("bad_slice", f"{where} row {r} must have {width} integer heights")
        parsed: list[int] = []
        for value in line:
            if not is_int(value) or value < 0 or value > ceiling:
                raise ExperimentError(
                    "bad_slice", f"{where} contains a height outside 0..ceiling={ceiling}"
                )
            parsed.append(value)
        grid.append(tuple(parsed))
    return tuple(grid)


def build_experiment(spec: Mapping[str, Any]) -> Experiment:
    """Validate a raw experiment specification and compute every reading.

    The readings are never trusted from the input: they are recomputed by the
    solver engine and the input's ``readings`` block, if present, must match.
    """

    if not isinstance(spec, Mapping):
        raise ExperimentError("bad_spec", "experiment spec must be an object")
    experiment_id = spec.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise ExperimentError("bad_spec", "experiment_id must be a non-empty string")
    height, width, ceiling = spec.get("height"), spec.get("width"), spec.get("ceiling")
    if not (is_int(height) and is_int(width) and is_int(ceiling)):
        raise ExperimentError("bad_spec", "height, width and ceiling must be integers")
    if height <= 0 or width <= 0 or ceiling < 0:
        raise ExperimentError("bad_spec", "dimensions must be positive and ceiling non-negative")

    raw_slices = spec.get("slices")
    if not isinstance(raw_slices, (list, tuple)):
        raise ExperimentError("bad_slice", "slices must be a list of candidate slices")
    if not raw_slices:
        raise ExperimentError("bad_slice", "at least one candidate slice is required")
    if len(raw_slices) > MAX_SLICES:
        raise ExperimentError("too_many_slices", f"at most {MAX_SLICES} candidate slices")
    slice_ids: set[str] = set()
    slices: list[tuple[str, tuple[tuple[int, ...], ...], str]] = []
    seen_grids: set[tuple[tuple[int, ...], ...]] = set()
    for index, item in enumerate(raw_slices):
        if not isinstance(item, Mapping):
            raise ExperimentError("bad_slice", f"slices[{index}] must be an object")
        slice_id = item.get("id")
        if not isinstance(slice_id, str) or not slice_id:
            raise ExperimentError("bad_slice", f"slices[{index}] id must be a non-empty string")
        if slice_id in slice_ids:
            raise ExperimentError("duplicate_slice", f"slice id {slice_id} is repeated")
        grid = _parse_grid(item.get("heights"), height, width, ceiling, f"slice {slice_id}")
        if grid in seen_grids:
            raise ExperimentError(
                "duplicate_surface",
                f"slice {slice_id} duplicates another slice's grid; indistinguishable drafts must be merged",
                {"slice_id": slice_id},
            )
        slice_ids.add(slice_id)
        seen_grids.add(grid)
        surface = _GridSurface(grid, ceiling)
        slices.append((slice_id, grid, surface.elevation_hash))

    raw_points = spec.get("points")
    if not isinstance(raw_points, (list, tuple)) or not raw_points:
        raise ExperimentError("bad_point", "at least one re-measurement point is required")
    if len(raw_points) > MAX_POINTS:
        raise ExperimentError("too_many_points", f"at most {MAX_POINTS} points")
    points: list[SurveyPoint] = []
    point_ids: set[str] = set()
    point_cells: set[Cell] = set()
    for item in raw_points:
        point = SurveyPoint.from_dict(item)
        if point.id in point_ids:
            raise ExperimentError("duplicate_point", f"point id {point.id} is repeated")
        r, c = point.cell
        if not (0 <= r < height and 0 <= c < width):
            raise ExperimentError(
                "point_out_of_bounds", f"point {point.id} cell is outside the surface"
            )
        if point.cell in point_cells:
            raise ExperimentError("duplicate_point_cell", f"cell {point.cell} is used by two points")
        point_ids.add(point.id)
        point_cells.add(point.cell)
        points.append(point)

    raw_outlets = spec.get("outlets", [])
    if not isinstance(raw_outlets, (list, tuple)) or len(raw_outlets) > MAX_OUTLETS:
        raise ExperimentError("bad_outlet", f"outlets must be a list of at most {MAX_OUTLETS}")
    outlets: list[Outlet] = []
    outlet_ids: set[str] = set()
    boundary = lambda r, c: r in (0, height - 1) or c in (0, width - 1)
    for item in raw_outlets:
        outlet = Outlet.from_dict(item)
        if outlet.id in outlet_ids:
            raise ExperimentError("duplicate_outlet", f"outlet id {outlet.id} is repeated")
        r, c = outlet.cell
        if not (0 <= r < height and 0 <= c < width):
            raise ExperimentError(
                "outlet_out_of_bounds", f"outlet {outlet.id} cell is outside the surface"
            )
        if not boundary(r, c):
            raise ExperimentError(
                "outlet_not_boundary", f"outlet {outlet.id} cell must be on the boundary"
            )
        if outlet.sill < 0:
            raise ExperimentError("bad_outlet", f"outlet {outlet.id} sill must be non-negative")
        outlet_ids.add(outlet.id)
        outlets.append(outlet)

    # Recompute every reading with the solver instead of trusting the input.
    readings: dict[str, dict[str, int]] = {}
    for slice_id, grid, _ in slices:
        surface = _GridSurface(grid, ceiling)
        readings[slice_id] = {
            point.id: sealed_reading(surface, point.cell) for point in points
        }
    declared = spec.get("readings")
    if declared is not None and declared != readings:
        raise ExperimentError(
            "readings_mismatch",
            "declared readings do not match the recomputed no-outlet storage readings",
            {"declared": declared, "recomputed": readings},
        )

    content_hash = sha256_hex(
        {
            "experiment_id": experiment_id,
            "height": height,
            "width": width,
            "ceiling": ceiling,
            "slices": [
                {"id": sid, "heights": [list(r) for r in grid], "surface_hash": h}
                for sid, grid, h in slices
            ],
            "points": [point.to_dict() for point in points],
            "outlets": [outlet.to_dict() for outlet in outlets],
            "readings": readings,
        }
    )
    return Experiment(
        experiment_id=experiment_id,
        height=height,
        width=width,
        ceiling=ceiling,
        slices=tuple(slices),
        points=tuple(points),
        outlets=tuple(sorted(outlets, key=lambda outlet: outlet.id)),
        readings=readings,
        content_hash=content_hash,
    )
