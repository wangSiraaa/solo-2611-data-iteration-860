"""Fault catalog + minimum-stoppage reconstruction tests.

Covers the three task contrasts, incomparable masks matching at once, two
allowed time windows meeting at a single endpoint, and reading an old version
after a late correction.  Every assertion is exact (integers, no floats).
"""

from __future__ import annotations

import itertools

import pytest

from obsdelivery.faults import (
    FaultCatalog,
    FaultRule,
    KNOWN,
    UNKNOWN,
    resolve_payload,
)
from obsdelivery.notify import NotificationService
from obsdelivery.public import (
    public_notifications,
    public_reconstruction,
    verify_public_notifications,
    verify_public_reconstruction,
)
from obsdelivery.stoppages import solve
from obsdelivery.storage import Store


def rule(rule_id, mask, value, reasons):
    return {"rule_id": rule_id, "mask": mask, "value": value, "reasons": reasons}


def notif(nid, channel, reading, reasons, status=KNOWN, payload=1, bits="7fc00001"):
    return {
        "notification_id": nid,
        "channel": channel,
        "reading": reading,
        "bits": bits,
        "payload": payload,
        "rules": [],
        "reasons": reasons,
        "status": status,
    }


def reg(channel, header, lo, hi):
    return {"channel": channel, "header": header, "delay_lo": lo, "delay_hi": hi}


# ---------------------------------------------------------------------------
# catalog resolution
# ---------------------------------------------------------------------------

def test_specific_rule_covers_wide_rule():
    rules = [
        FaultRule.from_mapping(rule("wide", 0b1100, 0b0100, ["A"])),
        FaultRule.from_mapping(rule("specific", 0b1111, 0b0101, ["B"])),
    ]
    # payload 0b0101: both match; wide is covered by specific.
    match = resolve_payload(rules, 0b0101)
    assert match.matched_rule_ids == ("specific", "wide")
    assert match.retained_rule_ids == ("specific",)
    assert match.reasons == ("B",)
    assert match.status == KNOWN


def test_incomparable_masks_both_remain_and_reasons_union():
    rules = [
        FaultRule.from_mapping(rule("wide", 0b1100, 0b0100, ["A"])),
        FaultRule.from_mapping(rule("cross", 0b1010, 0b0010, ["B"])),
    ]
    # payload 0b0110 matches both; masks 1100 and 1010 are incomparable.
    match = resolve_payload(rules, 0b0110)
    assert match.retained_rule_ids == ("cross", "wide")
    assert match.reasons == ("A", "B")


def test_unmatched_payload_is_unknown_without_reasons():
    rules = [FaultRule.from_mapping(rule("wide", 0b1100, 0b0100, ["A"]))]
    match = resolve_payload(rules, 0b0011)
    assert match.status == UNKNOWN
    assert match.reasons == ()
    assert match.retained_rule_ids == ()


def test_rule_value_outside_mask_rejected():
    from obsdelivery.errors import ValidationError

    with pytest.raises(ValidationError):
        FaultRule.from_mapping(rule("bad", 0b0100, 0b0010, ["A"]))


def test_payload_distinctness_via_notification_products():
    store = Store(":memory:")
    notify = NotificationService(store)
    notify.publish_catalog(
        "v1",
        [
            rule("a", 0x7FFFFF, 0x400001, ["A"]),
            rule("b", 0x7FFFFF, 0x400002, ["B"]),
        ],
    )
    # 0x7fc00001 and 0x7fc00002 are two different NaN payloads -> A and B.
    product = notify.publish_notifications(
        "p1",
        "v1",
        [
            {
                "notification_id": "n1", "sample_id": "s1", "revision": 1,
                "channel": "c1", "reading": 10, "bits": "7fc00001",
            },
            {
                "notification_id": "n2", "sample_id": "s2", "revision": 1,
                "channel": "c2", "reading": 10, "bits": "7fc00002",
            },
        ],
    )
    lines = {l["notification_id"]: l for l in product["notifications"]}
    assert lines["n1"]["payload"] == 0x400001 and lines["n1"]["reasons"] == ["A"]
    assert lines["n2"]["payload"] == 0x400002 and lines["n2"]["reasons"] == ["B"]

    # A finite value is not a missing-measurement code and is refused.
    from obsdelivery.errors import ValidationError

    with pytest.raises(ValidationError):
        notify.publish_notifications(
            "p2",
            "v1",
            [
                {
                    "notification_id": "n3", "sample_id": "s3", "revision": 1,
                    "channel": "c3", "reading": 10, "bits": "3f800000",
                }
            ],
        )
    store.close()


# ---------------------------------------------------------------------------
# three contrasts from the task statement
# ---------------------------------------------------------------------------

THREE_HEADERS = [reg("c1", "h1", 0, 0), reg("c2", "h2", 0, 0), reg("c3", "h3", 0, 0)]


def test_contrast_one_three_pairwise_mergeable_needs_two():
    notifications = [
        notif("n1", "c1", 100, ["A", "B"]),
        notif("n2", "c2", 100, ["B", "C"]),
        notif("n3", "c3", 100, ["A", "C"]),
    ]
    result = solve(notifications, THREE_HEADERS, ["A", "B", "C"])
    assert result["feasible"] and result["min_stoppages"] == 2
    # Any pair shares a reason, but the three-way intersection is empty.
    for pair in itertools.combinations(notifications, 2):
        assert set(pair[0]["reasons"]) & set(pair[1]["reasons"])
    assert set.intersection(*(set(n["reasons"]) for n in notifications)) == set()
    witness = result["witness"]
    assert witness["min_stoppages"] == 2
    assert sorted(len(g["notification_ids"]) for g in witness["groups"]) == [1, 2]
    for group in witness["groups"]:
        assert group["time_interval"] == [100, 100]


def test_contrast_two_more_specific_rule_makes_one_b_stoppage_enough():
    # n3's payload now resolves to only {B} (a more specific rule).
    notifications = [
        notif("n1", "c1", 100, ["A", "B"]),
        notif("n2", "c2", 100, ["B", "C"]),
        notif("n3", "c3", 100, ["B"]),
    ]
    result = solve(notifications, THREE_HEADERS, ["A", "B", "C"])
    assert result["feasible"] and result["min_stoppages"] == 1
    group = result["witness"]["groups"][0]
    assert group["reason"] == "B"
    assert sorted(group["notification_ids"]) == ["n1", "n2", "n3"]
    counts = result["reason_counts"]
    assert counts["A"] == {"min": 0, "max": 0, "possible": False}
    assert counts["B"] == {"min": 1, "max": 1, "possible": True}
    assert counts["C"] == {"min": 0, "max": 0, "possible": False}


def test_contrast_three_same_header_for_first_two_needs_two_again():
    # The fixed new product (n3 only {B}); c1 and c2 share header h1.
    notifications = [
        notif("n1", "c1", 100, ["A", "B"]),
        notif("n2", "c2", 100, ["B", "C"]),
        notif("n3", "c3", 100, ["B"]),
    ]
    registrations = [reg("c1", "h1", 0, 0), reg("c2", "h1", 0, 0), reg("c3", "h2", 0, 0)]
    result = solve(notifications, registrations, ["A", "B", "C"])
    assert result["feasible"] and result["min_stoppages"] == 2
    witness = result["witness"]
    assert witness["min_stoppages"] == 2
    # n1 and n2 can never share a stoppage: same header.
    for group in witness["groups"]:
        ids = set(group["notification_ids"])
        assert not {"n1", "n2"} <= ids


def test_new_catalog_via_notification_service_keeps_old_product_readable():
    store = Store(":memory:")
    notify = NotificationService(store)
    notify.publish_catalog(
        "old",
        [
            rule("prefix", 0x0000FF, 0x000001, ["M"]),
        ],
    )
    # Old payload 0x000001 matches only the prefix rule -> {M}.
    old = notify.publish_notifications(
        "old-product",
        "old",
        [
            {
                "notification_id": "old-n1", "sample_id": "s1", "revision": 1,
                "channel": "c1", "reading": 100, "bits": "7fc00001",
            }
        ],
    )
    # A newer version adds a more specific rule that would resolve differently.
    notify.publish_catalog(
        "new",
        [
            rule("prefix", 0x0000FF, 0x000001, ["M"]),
            rule("exact", 0x7FFFFF, 0x400001, ["N"]),
        ],
    )
    reread = notify.get_notifications("old-product")
    assert reread["content_digest"] == old["content_digest"]
    assert reread["notifications"][0]["reasons"] == ["M"]
    # The new catalog does resolve the same bits more specifically.
    fresh = notify.resolve_code("new", "7fc00001")
    assert fresh["reasons"] == ["N"]
    store.close()


# ---------------------------------------------------------------------------
# time-window and delay semantics
# ---------------------------------------------------------------------------

def test_windows_touching_at_a_single_endpoint_merge():
    # n1 window [98,100], n2 window [100,102] -> share exactly 100.
    registrations = [reg("c1", "h1", 0, 2), reg("c2", "h2", 0, 2)]
    notifications = [notif("n1", "c1", 100, ["X"]), notif("n2", "c2", 102, ["X"])]
    result = solve(notifications, registrations, ["X"])
    assert result["min_stoppages"] == 1
    assert result["witness"]["groups"][0]["time_interval"] == [100, 100]
    assert result["witness"]["groups"][0]["time_witness"] == 100


def test_windows_just_missing_each_other_do_not_merge():
    # n1 window [98,100], n2 window [101,103] -> disjoint.
    registrations = [reg("c1", "h1", 0, 2), reg("c2", "h2", 0, 2)]
    notifications = [notif("n1", "c1", 100, ["X"]), notif("n2", "c2", 103, ["X"])]
    result = solve(notifications, registrations, ["X"])
    assert result["min_stoppages"] == 2


def test_same_channel_may_use_different_actual_delays():
    # Two notifications on one channel, different delays, same time 100.
    # They share a header so they must be separate stoppages, but each can
    # realize time 100 with its own delay in [0, 5].
    registrations = [reg("c1", "h1", 0, 5)]
    notifications = [notif("n1", "c1", 100, ["X"]), notif("n2", "c1", 103, ["X"])]
    assert solve(notifications, registrations, ["X"])["min_stoppages"] == 2
    # n2's window is [98,103]; pairing with n1 is forbidden by the header rule.
    assert solve(notifications, registrations, ["X"])["witness"]["groups"][0]["reason"] == "X"


def test_same_header_cannot_explain_one_stoppage_twice():
    registrations = [reg("c1", "h1", 0, 0), reg("c2", "h1", 0, 0), reg("c3", "h2", 0, 0)]
    notifications = [
        notif("n1", "c1", 100, ["X"]),
        notif("n2", "c2", 100, ["X"]),
        notif("n3", "c3", 100, ["X"]),
    ]
    result = solve(notifications, registrations, ["X"])
    assert result["min_stoppages"] == 2
    for group in result["witness"]["groups"]:
        assert len(group["notification_ids"]) <= 2


# ---------------------------------------------------------------------------
# reason-count bounds
# ---------------------------------------------------------------------------

def test_reason_count_bounds_over_minimum_explanations():
    # Three notifications all with {A,B}; h1 owns c1 and c3, so those two can
    # never share a stoppage.  Minimum is 2, and each of the two groups can be
    # attributed to A or to B independently.
    registrations = [reg("c1", "h1", 0, 0), reg("c2", "h2", 0, 0), reg("c3", "h1", 0, 0)]
    notifications = [
        notif("n1", "c1", 100, ["A", "B"]),
        notif("n2", "c2", 100, ["A", "B"]),
        notif("n3", "c3", 100, ["A", "B"]),
    ]
    result = solve(notifications, registrations, ["A", "B"])
    assert result["min_stoppages"] == 2
    assert result["reason_counts"]["A"] == {"min": 0, "max": 2, "possible": True}
    assert result["reason_counts"]["B"] == {"min": 0, "max": 2, "possible": True}


def test_reason_bounds_report_forced_minimum_above_zero():
    # n3 forces A and n4 forces B, so any minimum cover uses each at least once.
    registrations = [reg("c1", "h1", 0, 0), reg("c2", "h2", 0, 0),
                     reg("c3", "h3", 0, 0), reg("c4", "h4", 0, 0)]
    notifications = [
        notif("n1", "c1", 100, ["A", "B"]),
        notif("n2", "c2", 100, ["A", "B"]),
        notif("n3", "c3", 100, ["A"]),
        notif("n4", "c4", 100, ["B"]),
    ]
    result = solve(notifications, registrations, ["A", "B"])
    assert result["min_stoppages"] == 2
    assert result["reason_counts"]["A"]["min"] == 1
    assert result["reason_counts"]["B"]["min"] == 1


def test_reason_can_be_avoided_entirely():
    # A is always only an alternative for the merged pair; use B instead -> A 0.
    registrations = [reg("c1", "h1", 0, 0), reg("c2", "h2", 0, 0), reg("c3", "h3", 0, 0)]
    notifications = [
        notif("n1", "c1", 100, ["A", "B"]),
        notif("n2", "c2", 100, ["A", "B"]),
        notif("n3", "c3", 100, ["B", "C"]),
    ]
    result = solve(notifications, registrations, ["A", "B", "C"])
    assert result["min_stoppages"] == 1  # all three share B, all headers distinct
    assert result["reason_counts"]["A"]["max"] == 0
    assert result["reason_counts"]["B"] == {"min": 1, "max": 1, "possible": True}


# ---------------------------------------------------------------------------
# evidence gaps: unknown codes and unregistered channels
# ---------------------------------------------------------------------------

def test_unknown_code_marks_batch_infeasible():
    registrations = [reg("c1", "h1", 0, 0)]
    notifications = [notif("n1", "c1", 100, [], status=UNKNOWN, payload=999)]
    result = solve(notifications, registrations, [])
    assert not result["feasible"] and result["code"] == "INFEASIBLE"
    assert result["min_stoppages"] is None
    gap_codes = {g["code"] for g in result["evidence_gaps"]}
    assert "HAS_UNKNOWN" in gap_codes
    assert [n["notification_id"] for n in result["unexplained_notifications"]] == ["n1"]


def test_unregistered_channel_marks_batch_infeasible():
    registrations = [reg("c1", "h1", 0, 0)]
    notifications = [notif("n1", "c2", 100, ["X"])]
    result = solve(notifications, registrations, ["X"])
    assert not result["feasible"]
    gap_codes = {g["code"] for g in result["evidence_gaps"]}
    assert "UNREGISTERED" in gap_codes
    assert [n["notification_id"] for n in result["unregistered_notifications"]] == ["n1"]


# ---------------------------------------------------------------------------
# public verification (no database)
# ---------------------------------------------------------------------------

def _build_scene(tmp_path):
    store = Store(str(tmp_path / "scene.sqlite"))
    notify = NotificationService(store)
    notify.publish_catalog(
        "v1",
        [
            rule("ab", 0x7FFFFF, 0x400001, ["A", "B"]),
            rule("bc", 0x7FFFFF, 0x400002, ["B", "C"]),
            rule("b", 0x7FFFFF, 0x400003, ["B"]),
            rule("unknown-range", 0x00FF00, 0x000100, ["Z"]),
        ],
    )
    notify.publish_notifications(
        "np",
        "v1",
        [
            {"notification_id": "n1", "sample_id": "s1", "revision": 1,
             "channel": "c1", "reading": 100, "bits": "7fc00001"},
            {"notification_id": "n2", "sample_id": "s2", "revision": 1,
             "channel": "c2", "reading": 100, "bits": "7fc00002"},
            {"notification_id": "n3", "sample_id": "s3", "revision": 1,
             "channel": "c3", "reading": 100, "bits": "7fc00003"},
        ],
    )
    return store, notify


def test_public_notifications_re_resolve_without_db(tmp_path):
    store, notify = _build_scene(tmp_path)
    document = public_notifications(notify, "np")
    assert verify_public_notifications(document) == []
    # Tamper the reasons of one line; verification catches it.
    bad = {
        **document,
        "notifications": [
            {**document["notifications"][0], "reasons": ["A"]},
            *document["notifications"][1:],
        ],
    }
    assert verify_public_notifications(bad)
    store.close()


def test_public_reconstruction_self_verifies(tmp_path):
    store, notify = _build_scene(tmp_path)
    registrations = [reg("c1", "h1", 0, 0), reg("c2", "h2", 0, 0), reg("c3", "h3", 0, 0)]
    result = notify.reconstruct("r1", "np", registrations, ["A", "B", "C"])
    assert result["min_stoppages"] == 1  # n3 is exactly {B}; all share B
    doc = public_reconstruction(notify, "r1")
    assert verify_public_reconstruction(doc) == []
    # Relaxing the witness reason to one not shared is caught independently.
    group = dict(doc["result"]["witness"]["groups"][0], reason="A")
    bad = {
        **doc,
        "result": {
            **doc["result"],
            "witness": {"min_stoppages": 1, "groups": [group]},
        },
    }
    assert verify_public_reconstruction(bad)
    store.close()


def test_late_correction_keeps_old_reconstruction_readable(tmp_path):
    store, notify = _build_scene(tmp_path)
    registrations = [reg("c1", "h1", 0, 0), reg("c2", "h2", 0, 0), reg("c3", "h3", 0, 0)]
    first = notify.reconstruct("rc", "np", registrations, ["A", "B", "C"])
    # A later catalog publishes a more specific rule for payload 3 -> only {B}.
    notify.publish_catalog(
        "v2",
        [
            rule("ab", 0x7FFFFF, 0x400001, ["A", "B"]),
            rule("bc", 0x7FFFFF, 0x400002, ["B", "C"]),
            rule("b", 0x7FFFFF, 0x400003, ["B"]),
            rule("exact3", 0x7FFFFF, 0x400003, ["C"]),
        ],
    )
    reread = notify.get_reconstruction("rc")
    assert reread["content_digest"] == first["content_digest"]
    assert reread["min_stoppages"] == first["min_stoppages"]
    store.close()


# ---------------------------------------------------------------------------
# original numeric / decimal entry points stay available
# ---------------------------------------------------------------------------

def test_original_decimal_delivery_still_works():
    from fractions import Fraction
    from obsdelivery.contracts import ReaderContract
    from obsdelivery.delivery import DeliveryService
    from obsdelivery.service import SemanticsService

    store = Store(":memory:")
    semantics = SemanticsService(store)
    delivery = DeliveryService(store, semantics)
    semantics.admit_observations([{"sample_id": "s1", "revision": 1, "bits": "3e000000"}])
    semantics.publish_reference(
        "r1", [{"sample_id": "s1", "expected": "1/8", "tolerance": "0"}], copy_from=None
    )
    semantics.publish_semantic_product("p", ["s1"], "r1")
    contract = ReaderContract.from_mapping(
        {"allowed_scales": [2, 3], "max_abs_coefficient": 99, "max_abs_error": "1/200"}
    )
    out = delivery.submit("d", "p", ["s1"], contract)
    line = out["lines"][0]
    assert line["coefficient"] == 12 and line["scale"] == 2
    assert Fraction(line["reader_value"]) == Fraction(3, 25)
    assert semantics.judge("s1", "r1")["status"] == "PASS"
    store.close()
