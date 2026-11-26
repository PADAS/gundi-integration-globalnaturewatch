import random

from app.actions.patches.drivers import summarise


def test_majority_class_and_its_share_of_labelled_pixels():
    result = summarise(["mining"] * 6 + ["wildfire"] * 2 + [None] * 2)
    assert result.driver == "mining"
    assert result.share == 0.75          # 6 of the 8 labelled, not of all 10
    assert (result.labelled, result.total) == (8, 10)


def test_unlabeled_is_never_the_driver_even_when_most_common():
    result = summarise(["unlabeled"] * 9 + ["mining"], unlabeled=["unlabeled"])
    assert result.driver == "mining"
    assert result.share == 1.0
    assert (result.labelled, result.total) == (1, 10)


def test_all_unlabeled_patch_has_no_driver():
    result = summarise([None, "unlabeled", None], unlabeled=["unlabeled"])
    assert result.driver is None
    assert result.share is None
    assert (result.labelled, result.total) == (0, 3)


def test_patch_with_no_pixels_has_no_driver():
    result = summarise([])
    assert result.driver is None
    assert (result.labelled, result.total) == (0, 0)


def test_single_pixel_patch():
    result = summarise(["wildfire"])
    assert (result.driver, result.share) == ("wildfire", 1.0)


def test_tie_goes_to_the_higher_priority_class():
    priority = {"mining": 300, "wildfire": 100}.get
    result = summarise(["wildfire", "mining", "wildfire", "mining"], priority_of=priority)
    assert result.driver == "mining"
    assert result.share == 0.5


def test_priority_only_breaks_ties_it_does_not_beat_a_majority():
    priority = {"mining": 300, "wildfire": 100}.get
    result = summarise(["wildfire"] * 3 + ["mining"] * 2, priority_of=priority)
    assert result.driver == "wildfire"


def test_tie_with_equal_priority_does_not_depend_on_pixel_order():
    classes = ["flooding", "landslide"] * 5
    rng = random.Random(7)
    seen = set()
    for _ in range(20):
        rng.shuffle(classes)
        seen.add(summarise(classes).driver)
    assert seen == {"flooding"}


def test_integer_class_codes_work_as_keys():
    result = summarise([3, 3, 7, 0], unlabeled=[0])
    assert (result.driver, result.labelled, result.total) == (3, 3, 4)


def test_a_class_with_no_known_priority_does_not_break_the_summary():
    # The class list is not confirmed, so a class missing from the priority
    # table is expected. `dict.get` answers None for it.
    priority_of = {"mining": 300, "wildfire": 100}.get

    result = summarise(["mining", "mining", "new_class"], priority_of=priority_of)
    assert result.driver == "mining"

    # In a tie it ranks below every class that has a priority, 0 included.
    tie = summarise(["new_class", "wildfire"], priority_of={"wildfire": 0}.get)
    assert tie.driver == "wildfire"
    # Two unknown classes tie on the class key, whatever the pixel order.
    assert summarise(["b_new", "a_new"], priority_of=priority_of).driver == "a_new"
    assert summarise(["a_new", "b_new"], priority_of=priority_of).driver == "a_new"


def test_the_event_config_can_rank_classes_for_the_tie_break():
    from app.actions.patches.events import EventConfig

    config = EventConfig(default_priority=None, driver_priority_overrides={"wildfire": 300})
    result = summarise(["mining", "wildfire", "new_class"], priority_of=config.priority_for)
    # mining and wildfire both rank 300 for this connection; the key decides.
    assert result.driver == "mining"
