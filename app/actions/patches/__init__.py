"""Patch lifecycle core: group alert pixels into stable, Gundi-owned patches.

Pure logic only (stdlib, no I/O). The store, jobs and delivery build on it.
Design: "GNW Event Lifecycle: Specification and Implementation Plan".
"""
from .core import (  # noqa: F401
    CONFIDENCE_RANK, Change, Params, Patch, Pixel, Result, apply_batch,
)
from .grid import (  # noqa: F401
    CELLS_PER_DEGREE, cell_center, cell_index, pixel_area_m2, tile_id,
)
