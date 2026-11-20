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

    def refresh(self) -> None:
        """Recompute aggregates. An emptied patch keeps its last known values."""
        if not self.pixels:
            return
        dates = [p.alert_date for p in self.pixels.values()]
        self.first_alert, self.last_alert = min(dates), max(dates)
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


def _collapse(batch: Iterable[Pixel]) -> Dict[Cell, Pixel]:
    """One pixel per cell: earliest date, highest confidence."""
    by_cell: Dict[Cell, Pixel] = {}
    for p in batch:
        seen = by_cell.get(p.cell)
        if seen is None:
            by_cell[p.cell] = p
        else:
            by_cell[p.cell] = Pixel(p.row, p.col, min(seen.alert_date, p.alert_date),
                                    max(seen.confidence, p.confidence))
    return by_cell


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
    owner: Dict[Cell, str] = {}
    for pid, patch in work.items():
        if patch.status in (OPEN, CLOSED):
            for cell, pixel in patch.pixels.items():
                # A cell disturbed again long after a closed patch can sit in
                # two patches; the most recent one owns it.
                held = owner.get(cell)
                if held is None or work[held].pixels[cell].alert_date < pixel.alert_date:
                    owner[cell] = pid

    incoming = _collapse(batch)

    # 1. Withdrawals.
    if withdraw_from is not None:
        for pid in sorted(set(owner.values()), key=lambda i: work[i].created_order):
            patch = work[pid]
            gone = [c for c, p in patch.pixels.items()
                    if p.alert_date >= withdraw_from and c not in incoming]
            if not gone:
                continue
            for cell in gone:
                del patch.pixels[cell]
                if owner.get(cell) == pid:
                    del owner[cell]
            if patch.pixels:
                patch.refresh()
                kinds.setdefault(pid, set()).add("shrunk")
            else:
                patch.status = WITHDRAWN
                kinds.setdefault(pid, set()).add("withdrawn")

    # 2. Reconcile pixels we already hold; what is left is new.
    fresh: Dict[date, Dict[Cell, Pixel]] = {}
    for cell, p in incoming.items():
        pid = owner.get(cell)
        if pid is None:
            fresh.setdefault(p.alert_date, {})[cell] = p
            continue
        patch = work[pid]
        stored = patch.pixels[cell]
        if abs((p.alert_date - stored.alert_date).days) < params.reuse_days:
            merged = Pixel(p.row, p.col, min(stored.alert_date, p.alert_date),
                           max(stored.confidence, p.confidence))
        elif patch.status == OPEN:
            merged = p  # same cell disturbed again inside a long-lived patch
        else:
            # A new disturbance on a cell of a closed patch: it can seed or
            # join another patch. The closed patch keeps its old pixel.
            fresh.setdefault(p.alert_date, {})[cell] = p
            continue
        if merged == stored:
            continue
        patch.pixels[cell] = merged
        patch.refresh()
        kinds.setdefault(pid, set()).add(
            "confidence_changed" if merged.alert_date == stored.alert_date else "grown")

    # 3. Group new pixels, oldest date first.
    def joinable(patch: Patch, d: date) -> bool:
        return (patch.status == OPEN
                and patch.first_alert - window <= d <= patch.last_alert + window)

    for d in sorted(fresh):
        cells = fresh[d]
        uf = _UnionFind()
        touched: Set[str] = set()
        for cell in sorted(cells):
            uf.find(("c", cell))
            r, c = cell
            for dr in range(-k, k + 1):
                for dc in range(-k, k + 1):
                    if dr == 0 and dc == 0:
                        continue
                    near = (r + dr, c + dc)
                    if near in cells:
                        uf.union(("c", cell), ("c", near))
                    pid = owner.get(near)
                    if pid is not None and joinable(work[pid], d):
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
                        target.pixels[cell] = p
                        owner[cell] = target.patch_id
                    other.pixels = {}
                    other.status = MERGED
                    other.merged_into = target.patch_id
                    kinds.setdefault(other_id, set()).add("merged")
            for cell in new_cells:
                target.pixels[cell] = cells[cell]
                owner[cell] = target.patch_id
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
