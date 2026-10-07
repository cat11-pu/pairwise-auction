"""An English auction settlement kernel built on the standard library only.

The caller owns the clock: every bid and every state change carries the tick
it happened at, and nothing in this module touches a clock, a file or a
network.  Money is handled as integers.

A lot walks open -> settled once its closing tick is reached, or open ->
cancelled when the seller withdraws it.  Escrow posted by bidders stays with
the lot until it closes: the winning bidder's escrow is applied to what they
owe, everybody else gets theirs back in full.
"""

__all__ = ["AuctionError", "Bid", "Lot", "Settlement"]

#: States a lot can be in.
OPEN = "open"
CLOSED = "closed"
SETTLED = "settled"
CANCELLED = "cancelled"


class AuctionError(ValueError):
    """Raised when an operation breaks the rules of the sale room."""


class Bid:
    """One offer that a lot accepted."""

    __slots__ = ("bidder", "amount", "tick", "seq")

    def __init__(self, bidder, amount, tick, seq):
        self.bidder = bidder
        self.amount = amount
        self.tick = tick
        self.seq = seq

    def __repr__(self):
        return "Bid(%r, amount=%r, tick=%r, seq=%r)" % (
            self.bidder,
            self.amount,
            self.tick,
            self.seq,
        )


class Settlement:
    """The statement a closed or cancelled lot hands to the back office.

    ``lines`` carries one row per bidder: what is charged to that bidder and
    what is paid back to them.
    """

    def __init__(self, lot_id, status, winner, price, premium, refunds,
                 charges, reason=None):
        self.lot_id = lot_id
        self.status = status
        self.winner = winner
        self.price = price
        self.premium = premium
        self.reason = reason
        self._refunds = dict(refunds)
        self._charges = dict(charges)

    def refund_of(self, bidder):
        """Escrow paid back to bidder by this statement."""
        return self._refunds.get(bidder, 0)

    def charge_of(self, bidder):
        """Amount this statement still asks of bidder."""
        return self._charges.get(bidder, 0)

    def lines(self):
        """One (bidder, charge, refund) row per bidder, sorted by bidder."""
        bidders = sorted(set(self._refunds) | set(self._charges))
        return [(name, self._charges.get(name, 0), self._refunds.get(name, 0))
                for name in bidders]

    def total_charged(self):
        """Sum of every charge on the statement."""
        return sum(self._charges.values())

    def total_refunded(self):
        """Sum of every refund on the statement."""
        return sum(self._refunds.values())

    def __repr__(self):
        return "Settlement(%r, %r, winner=%r, price=%r)" % (
            self.lot_id,
            self.status,
            self.winner,
            self.price,
        )


class Lot:
    """A single-lot English auction.

    reserve          price the seller will not go below.  It is applied when
                     the lot settles, so bids may stand below it and the lot
                     is then declared unsold.
    min_increment    smallest raise over the standing bid, and also the
                     smallest opening bid.
    deposit          escrow a bidder must hold before bidding; 0 turns the
                     check off.
    opens_at         first tick that takes bids.
    closes_at        tick the lot stops taking bids: bids land strictly
                     before it.  None keeps the lot open until close().
    snipe_window     a bid landing this close to the closing tick counts as
                     a late bid and moves the close.
    snipe_extension  how far a late bid moves the close, counted from the
                     late bid itself.
    premium_percent  buyer's premium charged on the hammer price.
    """

    def __init__(self, lot_id, reserve=0, min_increment=1, deposit=0,
                 opens_at=0, closes_at=None, snipe_window=0,
                 snipe_extension=0, premium_percent=0):
        if min_increment < 1:
            raise AuctionError("the minimum increment must be positive")
        if reserve < 0:
            raise AuctionError("the reserve cannot be negative")
        if deposit < 0:
            raise AuctionError("the required deposit cannot be negative")
        if premium_percent < 0:
            raise AuctionError("the buyer's premium cannot be negative")
        self._lot_id = lot_id
        self._reserve = reserve
        self._min_increment = min_increment
        self._deposit = deposit
        self._opens_at = opens_at
        self._closes_at = closes_at
        self._snipe_window = snipe_window
        self._snipe_extension = snipe_extension
        self._premium_percent = premium_percent
        self._state = OPEN
        self._bids = []
        self._seq = 0
        self._deposits = {}
        self._statement = None
        self._cancelled_at = None

    # -- read only views ------------------------------------------------
    def state(self):
        """One of open, closed, settled, cancelled."""
        return self._state

    def closes_at(self):
        """Tick the lot currently stops taking bids at."""
        return self._closes_at

    def bids(self):
        """Every accepted bid, in the order it arrived."""
        return list(self._bids)

    def bid_count(self):
        """How many bids the lot accepted."""
        return len(self._bids)

    def deposits(self):
        """Escrow held per bidder, as a fresh mapping."""
        return dict(self._deposits)

    def deposit_of(self, bidder):
        """Escrow currently held for bidder."""
        return self._deposits.get(bidder, 0)

    def statement(self):
        """Statement produced by settle() or cancel(), if any."""
        return self._statement

    def winner(self):
        """Bidder named by the statement, when there was a sale."""
        if self._statement is None:
            return None
        return self._statement.winner

    def cancelled_at(self):
        """Tick the lot was cancelled at, if it was."""
        return self._cancelled_at

    def opening_price(self):
        """Smallest amount the first bid may carry."""
        return self._min_increment

    def leading(self):
        """The standing best bid, or None while there is none."""
        if not self._bids:
            return None
        return max(self._bids, key=lambda bid: (bid.amount, -bid.seq))

    def current_price(self):
        """Amount a new bid is measured against."""
        leading = self.leading()
        if leading is None:
            return self.opening_price()
        return leading.amount

    def raise_floor(self):
        """Smallest amount a new bid may carry."""
        leading = self.leading()
        if leading is None:
            return self._min_increment
        return leading.amount + self._min_increment

    def reserve_met(self):
        """True when the standing bid clears the reserve."""
        if self._reserve <= 0:
            return True
        leading = self.leading()
        if leading is None:
            return False
        return leading.amount >= self._reserve

    # -- escrow ---------------------------------------------------------
    def post_deposit(self, bidder, amount):
        """Add amount to the escrow held for bidder, returning the balance."""
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise AuctionError("a deposit must be an integer amount")
        if amount <= 0:
            raise AuctionError("a deposit must be positive")
        if self._state in (SETTLED, CANCELLED):
            raise AuctionError(
                "lot %r takes no deposits in state %r" % (self._lot_id, self._state)
            )
        self._deposits[bidder] = self._deposits.get(bidder, 0) + amount
        return self._deposits[bidder]

    def _may_bid(self, bidder):
        """True when bidder holds enough escrow to bid."""
        if self._deposit <= 0:
            return True
        return self._deposits.get(bidder, 0) >= self._deposit

    # -- bidding --------------------------------------------------------
    def place_bid(self, bidder, amount, tick):
        """Record a bid and return it, or raise when it is not admissible."""
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise AuctionError("a bid must be an integer amount")
        if amount <= 0:
            raise AuctionError("a bid must be positive")
        if self._state != OPEN:
            raise AuctionError(
                "lot %r takes no bids in state %r" % (self._lot_id, self._state)
            )
        if tick < self._opens_at:
            raise AuctionError(
                "lot %r opens at tick %d" % (self._lot_id, self._opens_at)
            )
        if self._closes_at is not None and tick >= self._closes_at:
            raise AuctionError(
                "lot %r closed at tick %d" % (self._lot_id, self._closes_at)
            )
        if not self._may_bid(bidder):
            raise AuctionError(
                "bidder %r has not posted the required deposit" % (bidder,)
            )
        floor = self.raise_floor()
        if amount < floor:
            raise AuctionError(
                "bid %d does not clear the raise floor %d" % (amount, floor)
            )
        bid = Bid(bidder, amount, tick, self._seq)
        self._seq += 1
        self._bids.append(bid)
        self._apply_extension(tick)
        return bid

    def _apply_extension(self, tick):
        """Move the close out of the way of a late bid."""
        if self._closes_at is None:
            return
        if self._snipe_window <= 0 or self._snipe_extension <= 0:
            return
        if self._closes_at - tick > self._snipe_window:
            return
        self._closes_at = tick + self._snipe_extension

    # -- closing --------------------------------------------------------
    def close(self):
        """Stop taking bids without settling the lot."""
        if self._state != OPEN:
            raise AuctionError(
                "lot %r cannot be closed from state %r" % (self._lot_id, self._state)
            )
        self._state = CLOSED

    def cancel(self, tick, reason=None):
        """Withdraw the lot: no sale happens and escrow goes back untouched."""
        if self._state in (SETTLED, CANCELLED):
            raise AuctionError(
                "lot %r cannot be cancelled from state %r" % (self._lot_id, self._state)
            )
        refunds, charges = self._release(None, 0, 0)
        self._state = CANCELLED
        self._cancelled_at = tick
        self._statement = Settlement(
            self._lot_id, "cancelled", None, None, 0, refunds, charges, reason
        )
        return self._statement

    def _is_due(self, tick):
        """True when the lot may be settled at tick."""
        if self._state == CLOSED:
            return True
        if self._closes_at is None:
            return False
        return tick >= self._closes_at

    def settle(self, tick):
        """Settle the lot at tick and return the statement it produced."""
        if self._state == SETTLED:
            raise AuctionError("lot %r is already settled" % (self._lot_id,))
        if self._state == CANCELLED:
            raise AuctionError(
                "lot %r was cancelled and can never sell" % self._lot_id
            )
        if not self._is_due(tick):
            raise AuctionError(
                "lot %r is still taking bids at tick %d" % (self._lot_id, tick)
            )
        leading = self.leading()
        if leading is None or not self.reserve_met():
            refunds, charges = self._release(None, 0, 0)
            statement = Settlement(
                self._lot_id, "unsold", None, None, 0, refunds, charges
            )
        else:
            premium = leading.amount * self._premium_percent // 100
            refunds, charges = self._release(leading.bidder, leading.amount, premium)
            statement = Settlement(
                self._lot_id, "sold", leading.bidder, leading.amount, premium,
                refunds, charges,
            )
        self._state = SETTLED
        self._statement = statement
        return statement

    def _release(self, winner, price, premium):
        """Split the escrow into refunds and charges for a closing lot."""
        refunds = {}
        charges = {}
        bidders = set(self._deposits) | {bid.bidder for bid in self._bids}
        for bidder in bidders:
            held = self._deposits.get(bidder, 0)
            if bidder != winner:
                refunds[bidder] = held
                continue
            applied = min(held, price + premium)
            charges[bidder] = price + premium - applied
            refunds[bidder] = held - applied
        return refunds, charges
