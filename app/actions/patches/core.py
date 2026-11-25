"""Group alert pixels into patches and track each patch's lifecycle.

`apply_batch` is a pure function: given the patches known so far and one
batch of observed pixels, it returns the updated patches and the list of
changes to record. It never does I/O and never reads the clock.

Rules (spec, "The patch" / FR-5..FR-8):
- A new pixel joins an open patch when it is within `adjacency_k` cells
  (Chebyshev distance) of one of its pixels and its alert date is within
  `window_days` of the patch's first..last alert range.
- New pixels touching several patches merge them into the earliest-created
  one. Absorbed patches are kept, marked `merged`, pointing at the survivor.
- A patch closes after `window_days` without a new pixel. Closed is final:
  later adjacent alerts start a new patch.
- Patch IDs are minted at creation and never derived from pixels, because a
  pixel's date can move earlier after it was first seen.
- Pixels GNW has withdrawn shrink their patch; a patch left empty is marked
  `withdrawn`. Patches never split.

Determinism: pixels are processed in (alert_date, row, col) order, so the
order of a batch does not matter, and replaying history one day at a time
gives the same patches as one large batch. A pixel that arrives late, with a
date earlier than pixels already processed, is placed best-effort.

One cell can be disturbed more than once. Observations of a cell less than
`reuse_days` apart are the same disturbance (earliest date, highest
confidence); further apart they are separate ones, each matched by date to
the patch that holds it. So a cell can sit in a closed patch and again in a
newer one, and re-fetching either observation changes nothing.

A patch keeps one pixel per cell. A cell disturbed again inside a patch that
is still open replaces its older pixel there, and the older date is then
forgotten: a sighting dated between the two, seen again later, is read as
the newer alert's date moving earlier.

NOT here yet (open decisions in the design doc): the patch size cap and
split rule, and the emission threshold (a delivery concern).
"""
import copy
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

Cell = Tuple[int, int]

# gfw_integrated_alerts__confidence values, lowest to highest.
CONFIDENCE_RANK = {"nominal": 1, "low": 1, "high": 2, "highest": 3}

OPEN, CLOSED, MERGED, WITHDRAWN = "open", "closed", "merged", "withdrawn"

# Change kinds, in the order they are reported for one patch.
KIND_ORDER = ("created", "grown", "shrunk", "confidence_changed", "merged", "withdrawn", "closed")


@dataclass(frozen=True)
class Pixel:
    row: int
    col: int
    alert_date: date
    confidence: int

    @property
    def cell(self) -> Cell:
        return (self.row, self.col)


@dataclass(frozen=True)
class Params:
    window_days: int = 30     # N: temporal adjacency and close-after window
    adjacency_k: int = 1      # k: Chebyshev distance; 1 = 8-connected
    reuse_days: int = 180     # same cell, new date this far apart = new disturbance


@dataclass
class Patch:
    patch_id: str
    created_order: int        # monotonic; decides the survivor of a merge
    seed: Cell                # fixed at creation; the ER event location
    pixels: Dict[Cell, Pixel] = field(default_factory=dict)
    status: str = OPEN
    merged_into: Optional[str] = None
    version: int = 1
    first_alert: Optional[date] = None
    last_alert: Optional[date] = None
    max_confidence: int = 0

    @property
    def pixel_count(self) -> int:
        return len(self.pixels)

    def refresh(self, shrunk: bool = False) -> None:
        """Recompute aggregates. An emptied patch keeps its last known values.

        `first_alert` is when the patch began. It does not move later when a
        cell's pixel is replaced by a newer disturbance, only when pixels
        are withdrawn (`shrunk`).
        """
        if not self.pixels:
            return
        dates = [p.alert_date for p in self.pixels.values()]
        first = min(dates)
        if self.first_alert is not None and not shrunk:
            first = min(first, self.first_alert)
        self.first_alert, self.last_alert = first, max(dates)
        self.max_confidence = max(p.confidence for p in self.pixels.values())


@dataclass(frozen=True)
class Change:
    patch_id: str
    version: int
    kinds: Tuple[str, ...]
    merged_into: Optional[str] = None


@dataclass
class Result:
    patches: Dict[str, Patch]
    changes: List[Change]
    next_order: int


class _UnionFind:
    def __init__(self):
        self._parent = {}

    def find(self, x):
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _dedupe(batch: Iterable[Pixel]) -> List[Pixel]:
    """One pixel per cell and alert date, with the highest confidence seen,
    in (alert_date, row, col) order. Dates of one cell are not merged here:
    whether two dates are one disturbance depends on what is already stored."""
    best: Dict[Tuple[date, int, int], Pixel] = {}
    for p in batch:
        key = (p.alert_date, p.row, p.col)
        seen = best.get(key)
        if seen is None or seen.confidence < p.confidence:
            best[key] = p
    return [best[key] for key in sorted(best)]


def apply_batch(
    patches: Mapping[str, Patch],
    batch: Iterable[Pixel],
    *,
    today: date,
    params: Optional[Params] = None,
    new_id: Optional[Callable[[], str]] = None,
    next_order: Optional[int] = None,
    withdraw_from: Optional[date] = None,
) -> Result:
    """Apply one batch of observed pixels.

    patches: every patch that still has stored pixels in the area the batch
        covers: open ones, and closed ones whose pixels are retained. Closed
        patches cannot grow, but their pixels must be known so a re-fetched
        pixel is not mistaken for a new one.
    batch: pixels observed for that area. Re-observing known pixels is a no-op.
    today: the processing date; decides which patches close.
    withdraw_from: if set, the batch is taken as the COMPLETE set of pixels
        dated on or after this date for the area. Stored pixels in that range
        that are missing from the batch are withdrawn. Leave None when the
        batch is partial.
    """
    params = params or Params()
    new_id = new_id or (lambda: uuid.uuid4().hex)
    if next_order is None:
        next_order = max((p.created_order for p in patches.values()), default=-1) + 1
    window = timedelta(days=params.window_days)
    k = params.adjacency_k

    work: Dict[str, Patch] = {}
    for pid, patch in patches.items():
        clone = copy.copy(patch)
        clone.pixels = dict(patch.pixels)
        work[pid] = clone

    kinds: Dict[str, Set[str]] = {}
    # Every patch holding a pixel on a cell. A cell disturbed again long
    # after a closed patch sits in that patch and in a newer one.
    holders: Dict[Cell, List[str]] = {}
    for pid, patch in work.items():
        if patch.status in (OPEN, CLOSED):
            for cell in patch.pixels:
                holders.setdefault(cell, []).append(pid)

    def spans(patch: Patch, d: date) -> bool:
        return patch.first_alert - window <= d <= patch.last_alert + window

    def joinable(patch: Patch, d: date) -> bool:
        return patch.status == OPEN and spans(patch, d)

    def hold(patch: Patch, pixel: Pixel) -> None:
        """Put a pixel in a patch, unless the patch already has a newer one
        on that cell."""
        cell = pixel.cell
        had = patch.pixels.get(cell)
        if had is None:
            holders.setdefault(cell, []).append(patch.patch_id)
        elif had.alert_date > pixel.alert_date:
            return
        patch.pixels[cell] = pixel

    observed = _dedupe(batch)
    incoming = {p.cell for p in observed}

    # 1. Withdrawals.
    if withdraw_from is not None:
        live = [pid for pid, patch in work.items() if patch.status in (OPEN, CLOSED)]
        for pid in sorted(live, key=lambda i: work[i].created_order):
            patch = work[pid]
            gone = [c for c, p in patch.pixels.items()
                    if p.alert_date >= withdraw_from and c not in incoming]
            if not gone:
                continue
            for cell in gone:
                del patch.pixels[cell]
                holders[cell].remove(pid)
            if patch.pixels:
                patch.refresh(shrunk=True)
                kinds.setdefault(pid, set()).add("shrunk")
            else:
                patch.status = WITHDRAWN
                kinds.setdefault(pid, set()).add("withdrawn")

    # 2. Reconcile pixels we already hold; what is left is new.
    # Observations go oldest first, so each one meets every disturbance of
    # its cell that is older: those stored, and those new in this batch.
    fresh: Dict[date, Dict[Cell, Pixel]] = {}
    for p in observed:
        cell = p.cell
        held = [(work[pid], work[pid].pixels[cell]) for pid in holders.get(cell, ())]
        new = [(None, fresh[d][cell]) for d in fresh if cell in fresh[d]]

        def gap(h, p=p) -> int:
            return abs((p.alert_date - h[1].alert_date).days)

        same = [h for h in held + new if gap(h) < params.reuse_days]
        if same:
            # A disturbance we already know, whichever patch has it. It is
            # the latest one that began on or before this date; failing
            # that, this is an earlier sighting of the next one to begin.
            # Nearest-in-date would be wrong: seen again, a late sighting of
            # an old disturbance would move to a newer one that began just
            # after it.
            began_before = [h for h in same if h[1].alert_date <= p.alert_date]
            order = lambda h: (h[1].alert_date, h[0].created_order if h[0] else -1)  # noqa: E731
            patch, stored = (max(began_before, key=order) if began_before
                             else min(same, key=order))
            merged = Pixel(p.row, p.col, min(stored.alert_date, p.alert_date),
                           max(stored.confidence, p.confidence))
            if patch is None:
                # New in this batch too, and older: only confidence can rise.
                fresh[stored.alert_date][cell] = merged
                continue
        else:
            # A disturbance we do not hold.
            if any(spans(h[0], p.alert_date) and h[1].alert_date > p.alert_date for h in held):
                # A patch that was alive on this date has since taken a
                # newer pixel on the cell, which replaced this one.
                continue
            alive = [h for h in held if joinable(h[0], p.alert_date)]
            if not alive:
                # It can seed or join another patch; patches holding the
                # cell keep their own pixel.
                fresh.setdefault(p.alert_date, {})[cell] = p
                continue
            # Same cell disturbed again inside a long-lived patch.
            patch, stored = max(alive, key=lambda h: (h[1].alert_date, -h[0].created_order))
            merged = p
        if merged == stored:
            continue
        patch.pixels[cell] = merged
        patch.refresh()
        kinds.setdefault(patch.patch_id, set()).add(
            "confidence_changed" if merged.alert_date == stored.alert_date else "grown")

    # 3. Group new pixels, oldest date first.
    for d in sorted(fresh):
        cells = fresh[d]
        uf = _UnionFind()
        touched: Set[str] = set()
        for cell in sorted(cells):
            uf.find(("c", cell))
            r, c = cell
            for dr in range(-k, k + 1):
                for dc in range(-k, k + 1):
                    # The cell itself counts: a patch alive on this date that
                    # holds an older pixel here takes the new one.
                    near = (r + dr, c + dc)
                    if near != cell and near in cells:
                        uf.union(("c", cell), ("c", near))
                    for pid in holders.get(near, ()):
                        if joinable(work[pid], d):
                            uf.union(("c", cell), ("p", pid))
                            touched.add(pid)

        members: Dict[object, Tuple[List[Cell], Set[str]]] = {}
        for cell in sorted(cells):
            members.setdefault(uf.find(("c", cell)), ([], set()))[0].append(cell)
        for pid in touched:
            members[uf.find(("p", pid))][1].add(pid)

        for new_cells, pids in sorted(members.values(), key=lambda m: m[0][0]):
            if not pids:
                pid = new_id()
                target = Patch(patch_id=pid, created_order=next_order, seed=new_cells[0])
                next_order += 1
                work[pid] = target
                kinds.setdefault(pid, set()).add("created")
            else:
                ordered = sorted(pids, key=lambda i: work[i].created_order)
                target = work[ordered[0]]
                kinds.setdefault(target.patch_id, set()).add("grown")
                for other_id in ordered[1:]:
                    other = work[other_id]
                    for cell, p in other.pixels.items():
                        holders[cell].remove(other_id)
                        hold(target, p)
                    other.pixels = {}
                    other.status = MERGED
                    other.merged_into = target.patch_id
                    kinds.setdefault(other_id, set()).add("merged")
            for cell in new_cells:
                hold(target, cells[cell])
            target.refresh()

    # 4. Close patches that have gone quiet.
    for pid, patch in work.items():
        if patch.status == OPEN and patch.last_alert < today - window:
            patch.status = CLOSED
            kinds.setdefault(pid, set()).add("closed")

    # 5. One change per touched patch, oldest patch first.
    changes: List[Change] = []
    for pid in sorted(kinds, key=lambda i: work[i].created_order):
        patch, found = work[pid], kinds[pid]
        if "created" in found:
            found -= {"grown", "confidence_changed"}
        else:
            patch.version += 1
        changes.append(Change(
            patch_id=pid, version=patch.version,
            kinds=tuple(kd for kd in KIND_ORDER if kd in found),
            merged_into=patch.merged_into,
        ))
    return Result(patches=work, changes=changes, next_order=next_order)
