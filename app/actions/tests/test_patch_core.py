import itertools
import random
from datetime import date, timedelta

from app.actions.patches.core import (
    CLOSED, MERGED, OPEN, WITHDRAWN, Params, Pixel, apply_batch,
)

D0 = date(2026, 1, 1)


def day(n):
    return D0 + timedelta(days=n - 1)


def px(cells, n, confidence=1):
    return [Pixel(r, c, day(n), confidence) for r, c in cells]


def ids():
    counter = itertools.count()
    return lambda: f"p{next(counter)}"


def run(batches, params=None, new_id=None, **kwargs):
    """Apply (today_day, pixels) batches in order; return final patches and all changes."""
    new_id = new_id or ids()
    patches, changes, next_order = {}, [], 0
    for today_n, pixels in batches:
        result = apply_batch(patches, pixels, today=day(today_n), params=params,
                             new_id=new_id, next_order=next_order, **kwargs)
        patches, next_order = result.patches, result.next_order
        changes.extend(result.changes)
    return patches, changes


def partition(patches):
    """Comparable view: status and cells per live patch, ignoring ids and versions."""
    return sorted((p.status, tuple(sorted(p.pixels))) for p in patches.values()
                  if p.status in (OPEN, CLOSED))


# The worked example from the design deck: day 1, day 12, day 40.
A_DAY1 = [(2, 2), (2, 3), (3, 2), (3, 3), (3, 4)]
A_DAY12 = [(2, 4), (4, 3), (4, 4)]
B_DAY12 = [(4, 7), (4, 8), (5, 7), (5, 8)]
BRIDGE_DAY40 = [(4, 5), (4, 6)]


def test_deck_example_create_grow_merge():
    patches, _ = run([(1, px(A_DAY1, 1))])
    assert [(p.patch_id, p.pixel_count) for p in patches.values()] == [("p0", 5)]

    r2 = apply_batch(patches, px(A_DAY12 + B_DAY12, 12), today=day(12),
                     new_id=lambda: "p1", next_order=1)
    assert r2.patches["p0"].pixel_count == 8
    assert r2.patches["p1"].pixel_count == 4
    assert [(c.patch_id, c.kinds) for c in r2.changes] == [("p0", ("grown",)), ("p1", ("created",))]

    r3 = apply_batch(r2.patches, px(BRIDGE_DAY40, 40), today=day(40), next_order=r2.next_order)
    a, b = r3.patches["p0"], r3.patches["p1"]
    assert (a.status, a.pixel_count) == (OPEN, 14)
    assert (b.status, b.merged_into, b.pixel_count) == (MERGED, "p0", 0)
    assert [(c.patch_id, c.kinds, c.merged_into) for c in r3.changes] == [
        ("p0", ("grown",), None), ("p1", ("merged",), "p0")]
    assert a.seed == (2, 2) and a.first_alert == day(1) and a.last_alert == day(40)


def test_merge_survivor_is_earliest_created_not_earliest_dated():
    # B is created first but holds later-dated pixels than A.
    patches, _ = run([(20, px([(0, 3)], 20)), (20, px([(0, 0)], 10))])
    assert patches["p0"].first_alert == day(20) and patches["p1"].first_alert == day(10)
    result = apply_batch(patches, px([(0, 1), (0, 2)], 21), today=day(21))
    assert result.patches["p0"].status == OPEN and result.patches["p0"].pixel_count == 4
    assert result.patches["p1"].merged_into == "p0"


def test_diagonal_pixels_connect_with_k1_and_gap_needs_k2():
    patches, _ = run([(1, px([(0, 0), (1, 1), (2, 2)], 1))])
    assert len(patches) == 1
    gapped = px([(0, 0), (0, 2)], 1)
    assert len(run([(1, gapped)])[0]) == 2
    assert len(run([(1, gapped)], params=Params(adjacency_k=2))[0]) == 1


def test_adjacent_pixel_outside_window_starts_new_patch():
    patches, _ = run([(1, px([(0, 0)], 1)), (20, px([(0, 1)], 20))], params=Params(window_days=10))
    assert partition(patches) == [(CLOSED, ((0, 0),)), (OPEN, ((0, 1),))]


def test_patch_closes_after_window_and_closed_is_final():
    patches, changes = run([(1, px([(0, 0)], 1)), (40, [])])
    assert patches["p0"].status == CLOSED
    assert changes[-1].kinds == ("closed",) and changes[-1].version == 2
    # A pixel dated inside the window but arriving after the close starts a new patch.
    patches, _ = run([(1, px([(0, 0)], 1)), (40, []), (41, px([(0, 1)], 25))])
    assert partition(patches) == [(CLOSED, ((0, 0),)), (OPEN, ((0, 1),))]


def test_rerun_is_a_noop_even_after_close():
    batch = px(A_DAY1, 1)
    patches, _ = run([(1, batch), (60, [])])
    result = apply_batch(patches, batch, today=day(60))
    assert result.changes == []
    assert partition(result.patches) == partition(patches)


def test_confidence_only_rises_and_bumps_version_once():
    patches, _ = run([(1, px([(0, 0), (0, 1)], 1, confidence=1))])
    result = apply_batch(patches, px([(0, 0)], 1, confidence=3), today=day(2))
    patch = result.patches["p0"]
    assert patch.max_confidence == 3 and patch.version == 2
    assert result.changes[0].kinds == ("confidence_changed",)
    again = apply_batch(result.patches, px([(0, 0)], 1, confidence=1), today=day(3))
    assert again.changes == [] and again.patches["p0"].pixels[(0, 0)].confidence == 3


def test_earlier_date_keeps_patch_identity():
    patches, _ = run([(10, px([(0, 0), (0, 1)], 10))])
    result = apply_batch(patches, px([(0, 0)], 4), today=day(11))
    patch = result.patches["p0"]
    assert patch.first_alert == day(4) and patch.pixel_count == 2
    assert [c.patch_id for c in result.changes] == ["p0"]


def test_same_cell_after_reuse_window_in_closed_patch_is_new_disturbance():
    patches, _ = run([(1, px([(0, 0)], 1)), (60, [])])
    again = px([(0, 0)], 200)
    result = apply_batch(patches, again, today=day(200), new_id=lambda: "p1")
    assert result.patches["p0"].status == CLOSED
    assert result.patches["p0"].pixels[(0, 0)].alert_date == day(1)
    assert result.patches["p1"].pixels[(0, 0)].alert_date == day(200)
    # Re-fetching the new pixel reconciles with the new patch, not the old one.
    assert apply_batch(result.patches, again, today=day(201)).changes == []


def test_withdrawal_shrinks_then_withdraws_and_never_splits():
    line = [(0, 0), (0, 1), (0, 2)]
    patches, _ = run([(1, px(line, 1))])
    # Middle pixel withdrawn: patch shrinks but stays one patch.
    r1 = apply_batch(patches, px([(0, 0), (0, 2)], 1), today=day(2), withdraw_from=day(1))
    assert r1.patches["p0"].pixel_count == 2 and len(r1.patches) == 1
    assert r1.changes[0].kinds == ("shrunk",)
    # Everything withdrawn.
    r2 = apply_batch(r1.patches, [], today=day(3), withdraw_from=day(1))
    assert r2.patches["p0"].status == WITHDRAWN
    assert r2.changes[0].kinds == ("withdrawn",)
    assert r2.patches["p0"].last_alert == day(1)  # last known values kept


def test_withdrawal_ignores_pixels_older_than_lookback():
    patches, _ = run([(1, px([(0, 0)], 1)), (10, px([(0, 1)], 10))])
    result = apply_batch(patches, px([(0, 1)], 10), today=day(11), withdraw_from=day(5))
    assert result.changes == [] and result.patches["p0"].pixel_count == 2


def test_partial_batch_without_withdraw_from_removes_nothing():
    patches, _ = run([(1, px(A_DAY1, 1))])
    assert apply_batch(patches, [], today=day(2)).changes == []


def test_input_is_not_mutated():
    patches, _ = run([(1, px([(0, 0)], 1))])
    apply_batch(patches, px([(0, 1)], 2), today=day(2))
    assert patches["p0"].pixel_count == 1 and patches["p0"].version == 1


def _random_history(rng, days=45, size=14):
    history = {}
    for n in range(1, days + 1):
        cells = {(rng.randrange(size), rng.randrange(size)) for _ in range(rng.randrange(0, 7))}
        history[n] = px(sorted(cells), n)
    return history


def test_property_batch_order_does_not_matter():
    rng = random.Random(7)
    for _ in range(40):
        pixels = [p for batch in _random_history(rng).values() for p in batch]
        expected = run([(45, pixels)])[0]
        shuffled = pixels[:]
        rng.shuffle(shuffled)
        got = run([(45, shuffled)])[0]
        assert {i: (p.status, sorted(p.pixels), p.merged_into) for i, p in got.items()} == \
               {i: (p.status, sorted(p.pixels), p.merged_into) for i, p in expected.items()}


def test_property_day_by_day_equals_one_batch():
    rng = random.Random(11)
    for params in (Params(window_days=5), Params(window_days=30), Params(window_days=10, adjacency_k=2)):
        for _ in range(30):
            history = _random_history(rng)
            everything = [p for batch in history.values() for p in batch]
            one = run([(45, everything)], params=params)[0]
            daily = run([(n, history[n]) for n in sorted(history)], params=params)[0]
            assert partition(daily) == partition(one)


def test_property_versions_only_increase_and_cells_are_owned_once():
    rng = random.Random(3)
    history = _random_history(rng)
    patches, next_order, last_version = {}, 0, {}
    new_id = ids()
    for n in sorted(history):
        result = apply_batch(patches, history[n], today=day(n), new_id=new_id, next_order=next_order)
        patches, next_order = result.patches, result.next_order
        for change in result.changes:
            assert change.version >= last_version.get(change.patch_id, 0)
            last_version[change.patch_id] = change.version
        open_cells = [c for p in patches.values() if p.status == OPEN for c in p.pixels]
        assert len(open_cells) == len(set(open_cells))


# --- The same cell disturbed again after the reuse window ---------------------

X = (5, 5)


def _closed_then_open_on_one_cell():
    """Cell X in a closed day-1 patch and again in an open day-200 patch."""
    patches, _ = run([(1, px([X], 1)), (40, []), (200, px([X], 200))])
    assert sorted((p.status, p.pixels[X].alert_date) for p in patches.values()) == [
        (CLOSED, day(1)), (OPEN, day(200))]
    return patches


def test_replaying_an_old_observation_leaves_the_newer_patch_alone():
    patches = _closed_then_open_on_one_cell()

    result = apply_batch(patches, px([X], 1), today=day(200))

    assert result.changes == []
    assert partition(result.patches) == partition(patches)
    assert sorted(p.pixels[X].alert_date for p in result.patches.values()) == [day(1), day(200)]


def test_replaying_an_old_observation_when_both_patches_are_closed_adds_nothing():
    patches = _closed_then_open_on_one_cell()
    patches = apply_batch(patches, [], today=day(240)).patches
    assert [p.status for p in patches.values()] == [CLOSED, CLOSED]

    result = apply_batch(patches, px([X], 1) + px([X], 200), today=day(240))

    assert result.changes == []
    assert len(result.patches) == 2


def test_an_old_observation_improves_the_occurrence_it_belongs_to():
    patches = _closed_then_open_on_one_cell()

    result = apply_batch(patches, px([X], 1, confidence=3), today=day(200))

    by_date = {p.pixels[X].alert_date: p for p in result.patches.values()}
    assert by_date[day(1)].max_confidence == 3
    assert by_date[day(200)].max_confidence == 1
    assert [c.kinds for c in result.changes] == [("confidence_changed",)]


def test_an_unseen_old_observation_does_not_overwrite_a_newer_pixel():
    # Only the day-200 patch is known; the day-1 alert arrives late.
    patches, _ = run([(200, px([X], 200))])

    result = apply_batch(patches, px([X], 1), today=day(200))

    assert sorted((p.status, p.pixels[X].alert_date) for p in result.patches.values()) == [
        (CLOSED, day(1)), (OPEN, day(200))]
    again = apply_batch(result.patches, px([X], 1), today=day(200))
    assert again.changes == []


def test_one_batch_keeps_two_disturbances_of_a_cell_apart():
    pixels = px([X], 1) + px([X], 200)

    one_batch, _ = run([(200, pixels)])
    daily, _ = run([(1, px([X], 1)), (40, []), (200, px([X], 200))])

    assert sorted((p.status, p.pixels[X].alert_date) for p in one_batch.values()) == [
        (CLOSED, day(1)), (OPEN, day(200))]
    assert partition(one_batch) == partition(daily)


def test_one_batch_still_collapses_dates_inside_the_reuse_window():
    # Days 1 and 100 are one disturbance; day 250 is another.
    patches, _ = run([(250, px([X], 100, confidence=2) + px([X], 1) + px([X], 250))])

    assert sorted((p.pixels[X].alert_date, p.max_confidence) for p in patches.values()) == [
        (day(1), 2), (day(250), 1)]


def test_dates_in_one_batch_are_split_by_what_is_already_stored():
    # Day 178 is the stored day-1 disturbance seen again; day 181 is past the
    # reuse window, so it is a new one, though the two are 3 days apart.
    patches, _ = run([(1, px([X], 1)), (40, [])])

    together = apply_batch(patches, px([X], 178, confidence=2) + px([X], 181), today=day(181))
    apart, _ = run([(1, px([X], 1)), (40, []), (178, px([X], 178, confidence=2)),
                    (181, px([X], 181))])

    assert sorted((p.status, p.pixels[X].alert_date, p.max_confidence)
                  for p in together.patches.values()) == [(CLOSED, day(1), 2), (OPEN, day(181), 1)]
    assert _states(together.patches) == _states(apart)


def test_a_cell_disturbed_again_inside_a_long_lived_patch_stays_in_it():
    # The patch grows by one cell every 20 days, so it is still open on day
    # 200 when X is disturbed again.
    history = [(1, px([X], 1))]
    history += [(n, px([(5, 6 + i)], n)) for i, n in enumerate(range(21, 201, 20))]
    history += [(200, px([X], 200))]
    daily, _ = run(history)
    one_batch, _ = run([(200, [p for _, batch in history for p in batch])])

    for patches in (daily, one_batch):
        assert len(patches) == 1
        (patch,) = patches.values()
        assert patch.status == OPEN
        assert patch.pixels[X].alert_date == day(200)
        # The patch still began on day 1, though no pixel carries that date.
        assert patch.first_alert == day(1)

    # The day-1 alert was replaced, so seeing it again changes nothing, even
    # once the patch has closed.
    assert apply_batch(daily, px([X], 1), today=day(200)).changes == []
    closed = apply_batch(daily, [], today=day(260)).patches
    assert apply_batch(closed, px([X], 1), today=day(260)).changes == []


def _long_history(rng, days=420, size=5, events=90):
    """Sparse alerts on a small grid over more than two reuse windows, so
    cells are disturbed again after long gaps."""
    history = {}
    for _ in range(events):
        n = rng.randint(1, days)
        history.setdefault(n, []).append(
            Pixel(rng.randrange(size), rng.randrange(size), day(n), rng.randint(1, 3)))
    return history


def _states(patches):
    """Comparable view that tells two disturbances of one cell apart."""
    return sorted(
        (p.status, tuple(sorted((cell, px_.alert_date, px_.confidence)
                                for cell, px_ in p.pixels.items())))
        for p in patches.values() if p.status in (OPEN, CLOSED))


def test_property_long_history_gives_the_same_patches_however_it_is_batched():
    rng = random.Random(23)
    all_params = (Params(), Params(window_days=10), Params(window_days=60, adjacency_k=2),
                  Params(reuse_days=90))
    for round_ in range(60):
        params = all_params[round_ % len(all_params)]
        history = _long_history(rng)
        last = max(history)
        daily, _ = run([(n, history.get(n, [])) for n in range(1, last + 1)], params=params)

        everything = [p for n in sorted(history) for p in history[n]]
        rng.shuffle(everything)
        one_batch, _ = run([(last, everything)], params=params)
        weekly, _ = run(
            [(min(n + 6, last), [p for d in range(n, n + 7) for p in history.get(d, [])])
             for n in range(1, last + 1, 7)], params=params)

        assert _states(one_batch) == _states(daily)
        assert _states(weekly) == _states(daily)


def test_property_long_history_seeing_stored_pixels_again_is_a_noop():
    # Every pixel the patches still hold, closed patches included, offered
    # again in one batch. A cell held twice is offered with both its dates.
    rng = random.Random(29)
    for _ in range(40):
        history = _long_history(rng)
        last = max(history)
        patches, _ = run([(n, history.get(n, [])) for n in range(1, last + 1)])
        stored = [p for patch in patches.values() if patch.status in (OPEN, CLOSED)
                  for p in patch.pixels.values()]
        assert len({p.cell for p in stored}) < len(stored)  # some cell is held twice
        rng.shuffle(stored)

        replay = apply_batch(patches, stored, today=day(last))

        assert replay.changes == []
        assert _states(replay.patches) == _states(patches)


def test_a_sighting_between_a_replaced_date_and_its_replacement_moves_the_date_earlier():
    # Known limit: a patch keeps one pixel per cell. Once day 253 replaced
    # day 43 here, the patch no longer knows that day 196 belonged to the
    # day-43 disturbance, so seeing day 196 again reads as the day-253 alert
    # moving earlier. GNW's raster holds one date per pixel, so this takes a
    # replay of several raster versions at once.
    history = [(43, px([X], 43))]
    history += [(n, px([(5, 6 + i)], n)) for i, n in enumerate(range(60, 260, 20))]
    history += [(196, px([X], 196)), (253, px([X], 253))]
    patches, _ = run(sorted(history))
    (patch,) = patches.values()
    assert patch.pixels[X].alert_date == day(253)

    result = apply_batch(patches, px([X], 43) + px([X], 196), today=day(253))

    assert result.patches[patch.patch_id].pixels[X].alert_date == day(196)
    assert [c.kinds for c in result.changes] == [("grown",)]
