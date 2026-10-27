"""Tests for solution/01.py (standard library only)."""
import importlib.util
import json
import os
import sys
from datetime import datetime
from decimal import Decimal

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sales01", os.path.join(HERE, "..", "01.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["sales01"] = m  # dataclasses needs the module registered
spec.loader.exec_module(m)


@pytest.fixture()
def store():
    return m.load_sample_data()


def test_transactions_for_user(store):
    txns = store.transactions_for_user(193216)
    assert [(t.product_id, t.cost) for t in txns] == [(1, Decimal("5.66"))]


def test_lookup_returns_copy_not_internal_list(store):
    store.transactions_for_user(193216).clear()
    assert len(store.transactions_for_user(193216)) == 1


def test_lifetime_value_values(store):
    assert store.lifetime_value(193216) == Decimal("5.66")
    assert store.lifetime_value(230671) == Decimal("10.75")
    assert store.lifetime_value(123456) == Decimal("100.00")  # "$100.0"


def test_user_without_purchases_and_unknown_user_are_zero(store):
    assert store.lifetime_value(654282) == Decimal("0.00") and store.transactions_for_user(654282) == []
    assert store.lifetime_value(424242) == Decimal("0.00")
    assert store.has_user(654282) and not store.has_user(424242)


def test_users_who_purchased_product(store):
    assert store.user_ids_who_purchased(2) == [151537, 862027]
    assert store.users_who_purchased(2) == [(151537, "abby"), (862027, "gabe")]
    assert store.user_ids_who_purchased(404) == []


def test_orphan_purchase_is_kept_and_flagged(store):
    assert store.orphan_user_ids() == [123456]
    assert store.users_who_purchased(5) == [(123456, None)]


def test_duplicate_usernames_do_not_collide(store):
    assert store.has_user(654282) and store.has_user(908762)


def test_same_user_buying_same_product_twice_counts_once_in_buyers(store):
    store.add_purchase(151537, 2, "$6.77")
    assert store.user_ids_who_purchased(2) == [151537, 862027]
    assert store.lifetime_value(151537) == Decimal("13.54")
    assert store.product_revenue_cents(2) == 677 * 3


def test_money_is_exact_no_float_drift():
    s = m.SalesStore()
    for _ in range(10):
        s.add_purchase(1, 1, "$0.10")
    assert s.lifetime_value_cents(1) == 100 and s.lifetime_value(1) == Decimal("1.00")


@pytest.mark.parametrize("raw,cents", [("$5.66", 566), ("$100.0", 10000), (" $1,234.50 ", 123450),
                                       ("7", 700), ("($2.00)", -200), (Decimal("0.01"), 1)])
def test_parse_money(raw, cents):
    assert m.parse_money_to_cents(raw) == cents


@pytest.mark.parametrize("raw", ["abc", "$1.005", "", "$", "NaN"])
def test_parse_money_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        m.parse_money_to_cents(raw)


def test_refund_is_a_negative_transaction(store):
    store.add_purchase(193216, 1, "-$5.66")
    assert store.lifetime_value(193216) == Decimal("0.00")
    assert len(store.transactions_for_user(193216)) == 2


def test_running_total_matches_recompute(store):
    for uid in (193216, 151537, 230671, 654282, 123456, 862027):
        assert store.lifetime_value_cents(uid) == store.recompute_lifetime_value_cents(uid)


def test_duplicate_ids_rejected(store):
    with pytest.raises(ValueError):
        store.add_user(193216, "dup")
    first = store.transactions_for_user(193216)[0].transaction_id
    with pytest.raises(ValueError):
        store.add_purchase(1, 1, "$1.00", transaction_id=first)


def test_json_round_trip_is_lossless_and_rebuilds_indexes():
    s = m.load_sample_data()
    s.add_purchase(193216, 7, "$3.25", purchased_at=datetime(2025, 5, 1, 12, 30))
    restored = m.SalesStore.from_json(s.to_json())
    assert restored.to_dict() == s.to_dict()
    assert restored.user_ids_who_purchased(7) == [193216]
    assert restored.lifetime_value(193216) == Decimal("8.91")
    assert restored.transactions_for_user(193216)[1].purchased_at == datetime(2025, 5, 1, 12, 30)


def test_serialized_form_has_no_indexes_and_stores_integer_cents(store):
    payload = json.loads(store.to_json())
    assert set(payload) == {"schema_version", "users", "transactions"}
    assert all(isinstance(t["cost_cents"], int) for t in payload["transactions"])


def test_unknown_schema_version_rejected():
    with pytest.raises(ValueError):
        m.SalesStore.from_dict({"schema_version": 999, "users": [], "transactions": []})


def test_demo_runs(capsys):
    m.main()
    out = capsys.readouterr().out
    assert "orphan user_ids" in out and "lossless: OK" in out
