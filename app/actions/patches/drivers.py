"""Summarise per-pixel driver classes into one driver for a patch.

Pure logic. The class keys are opaque here: the GNW class codes and names
are not confirmed yet (spec P0-5), so nothing in this module depends on them.
"""
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Hashable, Iterable, Optional


@dataclass(frozen=True)
class DriverSummary:
    driver: Optional[Hashable]   # None when no pixel carries a label
    share: Optional[float]       # majority count / labelled count
    labelled: int
    total: int


def summarise(
    classes: Iterable[Optional[Hashable]],
    *,
    priority_of: Optional[Callable[[Hashable], int]] = None,
    unlabeled: Iterable[Hashable] = (),
) -> DriverSummary:
    """Majority driver of a patch, one entry of `classes` per pixel.

    `None` and anything in `unlabeled` count towards `total` only. A tie goes
    to the class with the higher `priority_of`, so a tie never hides the more
    serious driver; a tie that remains is broken by the class key so the
    result does not depend on pixel order.
    """
    skip = set(unlabeled)
    total = 0
    counts: Counter = Counter()
    for cls in classes:
        total += 1
        if cls is not None and cls not in skip:
            counts[cls] += 1
    labelled = sum(counts.values())
    if not labelled:
        return DriverSummary(driver=None, share=None, labelled=0, total=total)
    rank = priority_of or (lambda _cls: 0)
    driver = min(counts, key=lambda cls: (-counts[cls], -rank(cls), str(cls)))
    return DriverSummary(
        driver=driver, share=counts[driver] / labelled, labelled=labelled, total=total,
    )
