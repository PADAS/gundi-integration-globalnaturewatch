"""Integer addressing for the GNW 10/100000 raster grid.

Pixels are addressed by a global (row, col) so adjacency is integer
arithmetic and never a float comparison. Row 0 is the north edge (90N),
col 0 the west edge (180W).

ASSUMPTION (spec P0-4, not yet confirmed with GNW): the grid is 10-degree
tiles of 100,000 pixels, i.e. 0.0001 degrees per pixel, and query
coordinates are pixel centres. `cells_per_degree` is a parameter so the
answer can change without touching callers.
"""
import math
from typing import Tuple

CELLS_PER_DEGREE = 10_000
TILE_DEGREES = 10
EARTH_RADIUS_M = 6_371_008.8


def _floor_scaled(value: float) -> int:
    # Scaled coordinates carry float noise (~1e-9). Rounding to 3 decimals
    # removes it while a pixel centre (x.5) stays clear of both edges.
    return math.floor(round(value, 3))


def cell_index(lat: float, lon: float, cells_per_degree: int = CELLS_PER_DEGREE) -> Tuple[int, int]:
    """(row, col) of the pixel containing a coordinate."""
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError(f"Coordinate out of range: lat={lat}, lon={lon}")
    row = _floor_scaled((90.0 - lat) * cells_per_degree)
    col = _floor_scaled((lon + 180.0) * cells_per_degree)
    # lat=-90 / lon=180 sit on the far edge; fold them into the last cell.
    row = min(row, 180 * cells_per_degree - 1)
    col = min(col, 360 * cells_per_degree - 1)
    return row, col


def cell_center(row: int, col: int, cells_per_degree: int = CELLS_PER_DEGREE) -> Tuple[float, float]:
    """(lat, lon) of a pixel's centre."""
    lat = 90.0 - (row + 0.5) / cells_per_degree
    lon = (col + 0.5) / cells_per_degree - 180.0
    return lat, lon


def pixel_area_m2(row: int, cells_per_degree: int = CELLS_PER_DEGREE) -> float:
    """Ground area of one pixel in the given row. Pixels are equal-angle,
    not equal-area: east-west size shrinks with cos(latitude)."""
    lat, _ = cell_center(row, 0, cells_per_degree)
    side = math.radians(1.0 / cells_per_degree) * EARTH_RADIUS_M
    return side * side * math.cos(math.radians(lat))


def tile_id(row: int, col: int, cells_per_degree: int = CELLS_PER_DEGREE) -> str:
    """GNW tile name for a pixel, named by the tile's top-left corner
    (e.g. '10N_010E', '00N_060W')."""
    cells_per_tile = TILE_DEGREES * cells_per_degree
    top = 90 - TILE_DEGREES * (row // cells_per_tile)
    left = -180 + TILE_DEGREES * (col // cells_per_tile)
    ns = "N" if top >= 0 else "S"
    ew = "E" if left >= 0 else "W"
    return f"{abs(top):02d}{ns}_{abs(left):03d}{ew}"
