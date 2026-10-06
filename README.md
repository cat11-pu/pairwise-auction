# auction

A single-lot English auction kernel for the sale room back office, standard
library only.

## What it does

- One `Lot` carries a reserve price, a minimum increment, a required deposit
  and the ticks it opens and closes at.
- `place_bid(bidder, amount, tick)` records an offer; the caller owns the
  clock, so nothing here reads a real one.
- A late bid inside the soft-close window moves the closing tick out of the
  way (`snipe_window`, `snipe_extension`).
- Escrow is posted with `post_deposit`; `settle(tick)` and `cancel(tick,
  reason)` return a `Settlement` statement saying what is charged to and
  refunded to every bidder.
- All money is handled as integers.

## API

```python
from auction.core import AuctionError, Lot

lot = Lot("crown", reserve=300, min_increment=10, deposit=50,
          closes_at=200, premium_percent=10)
lot.post_deposit("alice", 50)
lot.place_bid("alice", 300, 10)

statement = lot.settle(200)
statement.status            # "sold"
statement.price             # 300
statement.refund_of("bob")  # escrow back to a losing bidder
```

## Requirements

Python 3 and the standard library only. No third-party packages.

## Running the tests

From the project root:

```
python3 -m unittest discover -s tests -v
```
