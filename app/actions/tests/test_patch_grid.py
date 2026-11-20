import math

import pytest

from app.actions.patches.grid import cell_center, cell_index, pixel_area_m2, tile_id


def test_cell_index_origin_and_known_pixel():
    assert cell_index(89.99995, -179.99995) == (0, 0)
    # equator / prime meridian: first cell south-east of (0, 0)
    assert cell_index(-0.00005, 0.00005) == (900_000, 1_800_000)


def test_cell_index_round_trips_through_center():
    for cell in [(0, 0), (900_000, 1_800_000), (912_345, 2_067_891), (1_799_999, 3_599_999)]:
        assert cell_index(*cell_center(*cell)) == cell


def test_cell_index_is_stable_against_float_noise():
    lat, lon = cell_center(912_345, 2_067_891)
    for eps in (-1e-9, 1e-9):
        assert cell_index(lat + eps, lon + eps) == (912_345, 2_067_891)


def test_adjacent_centers_are_adjacent_cells():
    row, col = cell_index(-1.23455, 23.45675)
    assert cell_index(-1.23455, 23.45685) == (row, col + 1)
    assert cell_index(-1.23465, 23.45675) == (row + 1, col)


def test_far_edges_fold_into_last_cell():
    assert cell_index(-90.0, 180.0) == (1_799_999, 3_599_999)


def test_out_of_range_rejected():
    with pytest.raises(ValueError):
        cell_index(91.0, 0.0)


def test_pixel_area_shrinks_with_latitude():
    equator = pixel_area_m2(900_000)
    assert 123.0 < equator < 124.5           # ~11.1 m x 11.1 m
    at_30 = pixel_area_m2(cell_index(30.00005, 0.0)[0])
    assert math.isclose(at_30 / equator, math.cos(math.radians(30)), rel_tol=1e-4)


def test_tile_id_uses_top_left_corner():
    assert tile_id(*cell_index(5.5, 15.5)) == "10N_010E"
    assert tile_id(*cell_index(-3.2, -62.1)) == "00N_070W"
    assert tile_id(*cell_index(-12.0, 24.0)) == "10S_020E"
