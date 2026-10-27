import importlib.util
import os
import sys
from decimal import Decimal

import pytest

path = os.path.join(os.path.dirname(__file__), "..", "01.py")
spec = importlib.util.spec_from_file_location("sales", path)
sales = importlib.util.module_from_spec(spec)
sys.modules["sales"] = sales
spec.loader.exec_module(sales)


@pytest.fixture
def store():
    return sales.load_sample()


def test_purchases_for_user(store):
    purchases = store.purchases_for(193216)
    assert [(p.product_id, p.cost_cents) for p in purchases] == [(1, 566)]


def test_user_without_purchases(store):
    assert store.purchases_for(654282) == []
    assert store.lifetime_value(654282) == Decimal("0.00")


def test_lifetime_value(store):
    assert store.lifetime_value(230671) == Decimal("10.75")
    assert store.lifetime_value(123456) == Decimal("100.00")


def test_lifetime_value_adds_up_exactly():
    s = sales.SalesStore()
    for _ in range(10):
        s.add_purchase(1, 1, "$0.10")
    assert s.lifetime_value(1) == Decimal("1.00")


def test_users_who_bought(store):
    assert store.users_who_bought(2) == [151537, 862027]
    assert store.users_who_bought(99) == []


def test_same_user_twice_counts_once(store):
    store.add_purchase(151537, 2, "$6.77")
    assert store.users_who_bought(2) == [151537, 862027]
    assert store.lifetime_value(151537) == Decimal("13.54")


def test_purchase_without_user_is_kept(store):
    assert store.orphan_user_ids() == [123456]


def test_bad_cost_rejected():
    with pytest.raises(ValueError):
        sales.to_cents("$1.005")


def test_json_round_trip(store):
    restored = sales.SalesStore.from_json(store.to_json())
    assert restored.users == store.users
    assert restored.lifetime_value(862027) == store.lifetime_value(862027)
    assert restored.users_who_bought(2) == store.users_who_bought(2)
