"""
Problem 01: User Sales Data Structure
=====================================

Run it:  python 01.py        (standard library only, Python 3.10+)

WHAT I BUILT
------------
`SalesStore`, an append-only in-memory store with ONE source of truth (the
transactions) and a few indexes that are maintained on every write:

    _users        : dict[user_id -> User]                  user dimension
    _by_user      : dict[user_id -> list[Transaction]]     PRIMARY ACCESS PATH
    _ltv_cents    : dict[user_id -> int]                   running lifetime value
    _by_product   : dict[product_id -> list[Transaction]]  secondary index
    _buyers       : dict[product_id -> set[user_id]]       distinct buyers

    Operation                               Cost
    ------------------------------------    ---------------------------------
    all transactions for a user_id          O(1) + O(k) to copy k rows
    lifetime value of a user                O(1)  (running total)
    all users who bought a product          O(1) + O(b) to copy b buyers
    revenue / purchase count per product    O(p) over that product's rows
    add one purchase                        O(1) amortised

WHY THESE CHOICES
-----------------
* The workload is "all of a user's purchases, keyed on user_id", so a hash map
  from user_id to that user's transaction list is the primary structure.
  The list is chronological (append order), so "latest purchase", "first
  purchase", "last N" are cheap slices.
* "Who bought product X" is the inverse access pattern. A scan would be
  O(all transactions), so I keep a product -> buyers index. It is a set of
  user_ids because one user can buy the same product twice and the question
  is about distinct users. The per-product transaction list shares the same
  Transaction objects (references, not copies), so it costs only pointers
  and answers revenue-per-product, units-per-product, top buyers, etc.
* Money is stored as integer cents, never float. 0.1 + 0.2 != 0.3 is not an
  acceptable property for a ledger. `Decimal` is used only at the edges
  (parsing "$5.66" and presenting totals).
* Lifetime value is a running total updated on write. That is safe because the
  store is append-only. Refunds/chargebacks are modelled as NEGATIVE-cost
  transactions rather than edits, so the total stays correct and the history
  stays auditable. `recompute_lifetime_value_cents` exists as a cross-check.
* Transactions are frozen dataclasses with __slots__: immutable (indexes can
  safely share them) and compact in memory.
* The user_id is the join key everywhere. `username` is NOT unique in the
  sample data ("andrew" is both 654282 and 908762), so results that identify
  people return user_ids, with usernames attached only as a display helper.

FIELDS I ADDED
--------------
`transaction_id` (the sample purchases have no key, so one is generated; a
caller-supplied id is rejected if it is a duplicate, which makes replays of a
load idempotent) and an optional `purchased_at` timestamp. Real purchase data
will need time, and "LTV in the last 90 days" is an obvious next question.
Quantity/currency/order_id would be added the same way (new optional field on
Transaction, nothing else changes).

DATA ISSUES FOUND IN THE SAMPLE (and what the code does)
--------------------------------------------------------
1. Purchase for user_id 123456 has no matching user. Dropping money records is
   worse than keeping them, so the transaction is stored and flagged:
   `orphan_user_ids()` lists such users, and `has_user()` returns False.
2. Users 654282 and 908762 have no purchases. Lookup returns an empty list and
   lifetime value is 0.00. (An unknown user_id behaves the same way; use
   `has_user()` to tell "no purchases" from "no such user".)
3. Costs arrive as strings: "$5.66" and "$100.0". `parse_money_to_cents`
   handles "$", commas and whitespace, and REJECTS sub-cent values like
   "$1.005" instead of silently rounding money.
4. Duplicate usernames, see above.
5. Product 2 appears for two different users, which the buyers index handles.

SERIALIZATION
-------------
Only the base records are serialized (users + transactions). Indexes are
derived data and are rebuilt on load. That means:
  * no way for a stored index to disagree with the stored transactions,
  * smaller payloads, and
  * adding a new index later needs no data migration.
Format is JSON with a `schema_version`, money as integer cents, timestamps as
ISO-8601 strings. JSON is fine at this scale and is human-inspectable. For
millions of rows I would write the same two flat tables as Parquet (columnar,
dictionary-encoded user_id/product_id) and rebuild indexes at load, or keep
them in a real store. `to_json` / `from_json` below do the round trip and the
demo asserts that it is lossless.

KNOWN LIMITS
------------
Not thread-safe (wrap writes in a lock or use a single writer). Everything is in
RAM, so memory grows with transactions. Indexes cost roughly one extra pointer
per transaction per index.
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
