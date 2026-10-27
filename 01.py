"""
Problem 01: user sales data structure.

Run with `python 01.py` (standard library only).

Design
------
The main query is "all purchases for a user_id", so purchases are stored in a dict keyed by
user_id, one list per user. Two more dicts are updated on every write:

    purchases_by_user     user_id    -> list of Purchase
    lifetime_cents        user_id    -> running total, in cents
    buyers_by_product     product_id -> set of user_ids

Looking up a user's purchases, their lifetime value, or the buyers of a product is a single
dict lookup. Adding a purchase is O(1). A product's buyers are a set because the same user can
buy the same product more than once and we want distinct users.

Money is kept as integer cents so totals are exact, and costs like "$5.66" are parsed on the
way in. The running total is safe because purchases are only ever appended (a refund would be
a negative purchase).

Things I noticed in the sample data
-----------------------------------
- user 123456 has a purchase but isn't in the users table. I keep the purchase rather than
  drop money data, and orphan_user_ids() reports it.
- "andrew" is two different users (654282, 908762), so username can't be a key. Everything
  is keyed on user_id.
- users 654282 and 908762 have no purchases, so lookups return [] and lifetime value is 0.

Serialization
-------------
to_json() writes just the users and purchases. The two lookup dicts are derived from those, so
from_json() rebuilds them instead of storing them. That way they can't get out of sync with the
data, and adding another index later doesn't change the file format. For a lot more data I'd
write the same two tables as Parquet instead of JSON.

Not thread-safe, and everything lives in memory.
"""

import json
from dataclasses import dataclass
from decimal import Decimal


def to_cents(cost):
    """'$5.66' or '$100.0' -> integer cents. Rejects values with fractions of a cent."""
    value = Decimal(str(cost).replace("$", "").replace(",", "").strip())
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"cost has fractions of a cent: {cost!r}")
    return int(cents)


def dollars(cents):
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Purchase:
    user_id: int
    product_id: int
    cost_cents: int


class SalesStore:
    def __init__(self):
        self.users = {}                # user_id -> username
        self.purchases_by_user = {}    # user_id -> [Purchase, ...]
        self.lifetime_cents = {}       # user_id -> total cents
        self.buyers_by_product = {}    # product_id -> {user_id, ...}

    def add_user(self, user_id, username):
        self.users[user_id] = username

    def add_purchase(self, user_id, product_id, cost):
        purchase = Purchase(user_id, product_id, to_cents(cost))
        self.purchases_by_user.setdefault(user_id, []).append(purchase)
        self.lifetime_cents[user_id] = self.lifetime_cents.get(user_id, 0) + purchase.cost_cents
        self.buyers_by_product.setdefault(product_id, set()).add(user_id)

    def purchases_for(self, user_id):
        """All purchases for a user, oldest first. Empty list if there are none."""
        return list(self.purchases_by_user.get(user_id, []))

    def lifetime_value(self, user_id):
        """What the user has spent so far, in dollars."""
        return dollars(self.lifetime_cents.get(user_id, 0))

    def users_who_bought(self, product_id):
        """Sorted user_ids of everyone who bought the product."""
        return sorted(self.buyers_by_product.get(product_id, set()))

    def orphan_user_ids(self):
        """user_ids that have purchases but no row in the users table."""
        return sorted(set(self.purchases_by_user) - set(self.users))

    def to_json(self):
        return json.dumps({
            "users": [{"user_id": u, "username": n} for u, n in self.users.items()],
            "purchases": [
                {"user_id": p.user_id, "product_id": p.product_id, "cost_cents": p.cost_cents}
                for ps in self.purchases_by_user.values() for p in ps
            ],
        })

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        store = cls()
        for u in data["users"]:
            store.add_user(u["user_id"], u["username"])
        for p in data["purchases"]:
            store.add_purchase(p["user_id"], p["product_id"], Decimal(p["cost_cents"]) / 100)
        return store


USERS = [
    (193216, "shane"),
    (151537, "abby"),
    (230671, "hongkai"),
    (654282, "andrew"),
    (908762, "andrew"),
    (862027, "gabe"),
]

PURCHASES = [
    (193216, 1, "$5.66"),
    (151537, 2, "$6.77"),
    (230671, 3, "$10.75"),
    (123456, 5, "$100.0"),
    (862027, 2, "$6.77"),
]


def load_sample():
    store = SalesStore()
    for user_id, username in USERS:
        store.add_user(user_id, username)
    for user_id, product_id, cost in PURCHASES:
        store.add_purchase(user_id, product_id, cost)
    return store


if __name__ == "__main__":
    store = load_sample()

    print("purchases for 193216:", store.purchases_for(193216))

    for user_id in (193216, 862027, 654282, 123456):
        print(f"lifetime value of {user_id}: ${store.lifetime_value(user_id)}")

    for product_id in (2, 5):
        print(f"users who bought product {product_id}:", store.users_who_bought(product_id))

    print("purchases with no user record:", store.orphan_user_ids())

    restored = SalesStore.from_json(store.to_json())
    assert restored.lifetime_value(123456) == store.lifetime_value(123456)
    assert restored.users_who_bought(2) == store.users_who_bought(2)
    print("json round trip ok")
