"""
Problem 01: user sales data structure.

Run with `python 01.py` (standard library only, Python 3.10+).

Design
------
The main access path is "all purchases for a user_id", so transactions live in a dict
keyed by user_id (a list per user, in purchase order). Two extra indexes are kept up to date
on every write:

    _by_user     user_id    -> list of transactions     (primary)
    _ltv_cents   user_id    -> running total in cents
    _by_product  product_id -> list of transactions     (same objects, no copies)
    _buyers      product_id -> set of user_ids          (distinct buyers)

Lookups are O(1) plus the cost of copying the result. Adding a purchase is O(1).

Decisions
---------
- Money is stored as integer cents so totals are exact. Strings like "$5.66" are parsed on
  the way in, and values with more than 2 decimal places are rejected.
- Lifetime value is a running total. That works because the store is append-only; a refund
  would be a negative transaction rather than an edit.
- username isn't unique in the sample (two "andrew"s), so everything is keyed by user_id.
- The sample purchases have no id, so a transaction_id is generated. I also allow an
  optional purchased_at, since time-based questions are the obvious next ask.

Sample data issues
------------------
- User 123456 has a purchase but no user row. I keep the purchase and expose it through
  orphan_user_ids() instead of dropping money data.
- Users 654282 and 908762 have no purchases (empty list, lifetime value 0).

Serialization
-------------
Only users and transactions are written out (JSON, money as cents). The indexes are derived,
so they're rebuilt on load. That keeps them from ever disagreeing with the stored data and
means a new index needs no migration. At larger scale I'd write the same two tables as Parquet.

Limits
------
Not thread-safe, and everything is held in memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Money helpers
# --------------------------------------------------------------------------- #
def parse_money_to_cents(raw: str | int | Decimal) -> int:
    """Parse '$5.66', '$100.0', '1,234.50' (or a Decimal/int dollar amount) to cents.

    Raises ValueError for malformed input or more than 2 decimal places.
    """
    if isinstance(raw, str):
        cleaned = raw.strip().replace("$", "").replace(",", "").strip()
        negative = cleaned.startswith("(") and cleaned.endswith(")")  # accounting style
        cleaned = cleaned.strip("()")
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            raise ValueError(f"cannot parse money value: {raw!r}") from None
        if negative:
            value = -value
    else:
        value = Decimal(raw)

    if not value.is_finite():
        raise ValueError(f"money value must be finite: {raw!r}")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"money value has sub-cent precision: {raw!r}")
    return int(cents)


def cents_to_decimal(cents: int) -> Decimal:
    """Integer cents -> Decimal dollars, e.g. 566 -> Decimal('5.66')."""
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class User:
    user_id: int
    username: str  # NOT unique, never use as a key


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_id: int
    user_id: int
    product_id: int
    cost_cents: int  # negative = refund / reversal
    purchased_at: Optional[datetime] = None

    @property
    def cost(self) -> Decimal:
        return cents_to_decimal(self.cost_cents)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
class SalesStore:
    """Append-only in-memory sales store. See module docstring for the design."""

    def __init__(self) -> None:
        self._users: dict[int, User] = {}
        self._by_user: dict[int, list[Transaction]] = {}
        self._ltv_cents: dict[int, int] = {}
        self._by_product: dict[int, list[Transaction]] = {}
        self._buyers: dict[int, set[int]] = {}
        self._txn_ids: set[int] = set()
        self._next_txn_id = 1

    # ---- writes ----------------------------------------------------------- #
    def add_user(self, user_id: int, username: str) -> User:
        """Register a user. Re-adding an existing id is an error (no silent overwrite)."""
        if user_id in self._users:
            raise ValueError(f"user_id {user_id} already exists")
        user = User(user_id, username)
        self._users[user_id] = user
        return user

    def add_purchase(
        self,
        user_id: int,
        product_id: int,
        cost: str | int | Decimal,
        purchased_at: Optional[datetime] = None,
        transaction_id: Optional[int] = None,
    ) -> Transaction:
        """Record a purchase and update every index. `cost` may be '$5.66'."""
        cents = parse_money_to_cents(cost)
        if transaction_id is None:
            while self._next_txn_id in self._txn_ids:
                self._next_txn_id += 1
            transaction_id = self._next_txn_id
        elif transaction_id in self._txn_ids:
            raise ValueError(f"duplicate transaction_id {transaction_id}")

        txn = Transaction(transaction_id, user_id, product_id, cents, purchased_at)
        self._txn_ids.add(transaction_id)
        self._by_user.setdefault(user_id, []).append(txn)
        self._ltv_cents[user_id] = self._ltv_cents.get(user_id, 0) + cents
        self._by_product.setdefault(product_id, []).append(txn)
        self._buyers.setdefault(product_id, set()).add(user_id)
        return txn

    # ---- reads: the two required operations ------------------------------- #
    def transactions_for_user(self, user_id: int) -> list[Transaction]:
        """All transactions for a user in purchase order. O(1) lookup. Returns a copy."""
        return list(self._by_user.get(user_id, ()))

    def lifetime_value_cents(self, user_id: int) -> int:
        """Lifetime value so far, in cents. O(1) (maintained as a running total)."""
        return self._ltv_cents.get(user_id, 0)

    def lifetime_value(self, user_id: int) -> Decimal:
        """Lifetime value so far, in dollars."""
        return cents_to_decimal(self.lifetime_value_cents(user_id))

    def user_ids_who_purchased(self, product_id: int) -> list[int]:
        """Distinct user_ids who bought the product (sorted for stable output)."""
        return sorted(self._buyers.get(product_id, ()))

    # ---- reads: helpers and examples of future needs ----------------------- #
    def users_who_purchased(self, product_id: int) -> list[tuple[int, Optional[str]]]:
        """(user_id, username-or-None) pairs. None marks a user that has no user record."""
        return [
            (uid, self._users[uid].username if uid in self._users else None)
            for uid in self.user_ids_who_purchased(product_id)
        ]

    def product_revenue_cents(self, product_id: int) -> int:
        return sum(t.cost_cents for t in self._by_product.get(product_id, ()))

    def has_user(self, user_id: int) -> bool:
        return user_id in self._users

    def orphan_user_ids(self) -> list[int]:
        """user_ids that have transactions but no user record."""
        return sorted(set(self._by_user) - set(self._users))

    def recompute_lifetime_value_cents(self, user_id: int) -> int:
        """O(k) recomputation from the raw transactions. Used to verify the running total."""
        return sum(t.cost_cents for t in self._by_user.get(user_id, ()))

    # ---- serialization ------------------------------------------------------ #
    def to_dict(self) -> dict:
        """Serialize base records only. Indexes are derived and rebuilt on load."""
        return {
            "schema_version": SCHEMA_VERSION,
            "users": [{"user_id": u.user_id, "username": u.username} for u in self._users.values()],
            "transactions": [
                {
                    "transaction_id": t.transaction_id,
                    "user_id": t.user_id,
                    "product_id": t.product_id,
                    "cost_cents": t.cost_cents,
                    "purchased_at": t.purchased_at.isoformat() if t.purchased_at else None,
                }
                for txns in self._by_user.values()
                for t in txns
            ],
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_dict(cls, payload: dict) -> "SalesStore":
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")
        store = cls()
        for u in payload["users"]:
            store.add_user(u["user_id"], u["username"])
        # Replay in transaction_id order so per-user lists keep their original chronology.
        for t in sorted(payload["transactions"], key=lambda r: r["transaction_id"]):
            ts = datetime.fromisoformat(t["purchased_at"]) if t["purchased_at"] else None
            dollars = Decimal(t["cost_cents"]) / 100  # exact: cents are integers
            store.add_purchase(t["user_id"], t["product_id"], dollars, ts, t["transaction_id"])
        return store

    @classmethod
    def from_json(cls, text: str) -> "SalesStore":
        return cls.from_dict(json.loads(text))


# --------------------------------------------------------------------------- #
# Sample data and demo
# --------------------------------------------------------------------------- #
SAMPLE_USERS: list[tuple[int, str]] = [
    (193216, "shane"),
    (151537, "abby"),
    (230671, "hongkai"),
    (654282, "andrew"),
    (908762, "andrew"),  # same username, different person
    (862027, "gabe"),
]

SAMPLE_PURCHASES: list[tuple[int, int, str]] = [
    # (user_id, productid, cost)
    (193216, 1, "$5.66"),
    (151537, 2, "$6.77"),
    (230671, 3, "$10.75"),
    (123456, 5, "$100.0"),  # user 123456 is not in SAMPLE_USERS
    (862027, 2, "$6.77"),
]


def load_sample_data(
    users: Iterable[tuple[int, str]] = SAMPLE_USERS,
    purchases: Iterable[tuple[int, int, str]] = SAMPLE_PURCHASES,
) -> SalesStore:
    store = SalesStore()
    for user_id, username in users:
        store.add_user(user_id, username)
    for user_id, product_id, cost in purchases:
        store.add_purchase(user_id, product_id, cost)
    return store


def main() -> None:
    store = load_sample_data()

    print("1) Lookup: all transactions for user_id 193216")
    for t in store.transactions_for_user(193216):
        print(f"   txn {t.transaction_id}: product {t.product_id}, cost ${t.cost}")

    print("\n2) Lifetime value so far")
    for uid in (193216, 862027, 654282, 123456, 999999):
        note = "" if store.has_user(uid) else "  (no user record)"
        print(f"   user {uid}: ${store.lifetime_value(uid)}{note}")

    print("\n3) Users who purchased a product")
    for pid in (2, 5, 99):
        print(f"   product {pid}: {store.users_who_purchased(pid)}")

    print("\n4) Data-quality checks")
    print(f"   orphan user_ids with purchases: {store.orphan_user_ids()}")
    assert store.lifetime_value_cents(193216) == store.recompute_lifetime_value_cents(193216)

    print("\n5) Serialization round trip")
    text = store.to_json()
    restored = SalesStore.from_json(text)
    assert restored.to_dict() == store.to_dict()
    assert restored.user_ids_who_purchased(2) == store.user_ids_who_purchased(2)
    print(f"   {len(text)} bytes of JSON, lossless: OK")


if __name__ == "__main__":
    main()
