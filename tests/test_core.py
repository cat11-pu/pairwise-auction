"""Behaviour tests for the auction settlement kernel.

Run them from the project root:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auction.core import AuctionError, Lot


def capture(call):
    """Run call, returning its result or the AuctionError it raised."""
    try:
        return call()
    except AuctionError as error:
        return error


class BiddingTest(unittest.TestCase):
    def test_the_standing_bid_is_the_highest_one_and_matches_are_refused(self):
        lot = Lot("vase", min_increment=10, closes_at=200)
        lot.place_bid("alice", 100, 5)
        match = capture(lambda: lot.place_bid("bob", 100, 6))
        short = capture(lambda: lot.place_bid("carol", 105, 7))
        self.assertIsInstance(match, AuctionError)
        self.assertIsInstance(short, AuctionError)
        lot.place_bid("dave", 110, 8)
        self.assertEqual(
            [(bid.bidder, bid.amount) for bid in lot.bids()],
            [("alice", 100), ("dave", 110)],
        )
        self.assertEqual(lot.leading().bidder, "dave")
        self.assertEqual(lot.current_price(), 110)

    def test_bids_inside_one_tick_are_checked_in_arrival_order(self):
        lot = Lot("vase", min_increment=10, closes_at=200)
        lot.place_bid("alice", 200, 20)
        low = capture(lambda: lot.place_bid("bob", 205, 20))
        lot.place_bid("bob", 210, 20)
        again = capture(lambda: lot.place_bid("carol", 210, 20))
        self.assertIsInstance(low, AuctionError)
        self.assertIsInstance(again, AuctionError)
        self.assertEqual([bid.amount for bid in lot.bids()], [200, 210])
        self.assertEqual(lot.leading().bidder, "bob")

    def test_the_lot_stops_taking_bids_at_its_closing_tick(self):
        lot = Lot("vase", min_increment=10, closes_at=100)
        lot.place_bid("alice", 100, 99)
        at_close = capture(lambda: lot.place_bid("bob", 200, 100))
        after = capture(lambda: lot.place_bid("carol", 300, 101))
        self.assertIsInstance(at_close, AuctionError)
        self.assertIsInstance(after, AuctionError)
        self.assertEqual(lot.bid_count(), 1)

    def test_a_late_bid_moves_the_closing_tick_off_the_late_bid(self):
        lot = Lot(
            "vase", min_increment=10, closes_at=100,
            snipe_window=10, snipe_extension=30,
        )
        lot.place_bid("alice", 100, 95)
        self.assertEqual(lot.closes_at(), 125)
        lot.place_bid("bob", 200, 120)
        self.assertEqual(lot.closes_at(), 150)
        lot.place_bid("carol", 300, 140)
        self.assertEqual(lot.closes_at(), 170)


class EscrowTest(unittest.TestCase):
    def test_deposits_accumulate_and_the_exact_amount_unlocks_bidding(self):
        lot = Lot("vase", min_increment=10, deposit=120, closes_at=200)
        lot.post_deposit("alice", 60)
        lot.post_deposit("alice", 60)
        self.assertEqual(lot.deposit_of("alice"), 120)
        self.assertEqual(lot.deposits(), {"alice": 120})
        allowed = capture(lambda: lot.place_bid("alice", 10, 5))
        self.assertNotIsInstance(allowed, AuctionError)
        refused = capture(lambda: lot.place_bid("dave", 20, 6))
        self.assertIsInstance(refused, AuctionError)
        self.assertEqual(lot.bid_count(), 1)


class SettlementTest(unittest.TestCase):
    def test_the_statement_charges_the_winner_and_refunds_everybody_else(self):
        lot = Lot("crown", reserve=300, min_increment=10, closes_at=200,
                  premium_percent=10)
        lot.post_deposit("alice", 50)
        lot.post_deposit("bob", 50)
        lot.post_deposit("carol", 50)
        lot.place_bid("bob", 250, 5)
        lot.place_bid("alice", 300, 10)
        statement = lot.settle(200)
        self.assertEqual(statement.status, "sold")
        self.assertEqual(statement.winner, "alice")
        self.assertEqual(statement.price, 300)
        self.assertEqual(statement.premium, 30)
        self.assertEqual(statement.charge_of("alice"), 280)
        self.assertEqual(statement.refund_of("alice"), 0)
        self.assertEqual(statement.refund_of("bob"), 50)
        self.assertEqual(statement.refund_of("carol"), 50)
        self.assertEqual(statement.total_refunded(), 100)
        self.assertEqual(
            statement.lines(),
            [("alice", 280, 0), ("bob", 0, 50), ("carol", 0, 50)],
        )

    def test_a_bid_below_the_reserve_leaves_the_lot_unsold(self):
        lot = Lot("crown", reserve=500, min_increment=10, closes_at=100)
        lot.place_bid("alice", 100, 10)
        lot.place_bid("bob", 200, 20)
        self.assertFalse(lot.reserve_met())
        statement = lot.settle(100)
        self.assertEqual(statement.status, "unsold")
        self.assertIsNone(statement.winner)
        self.assertIsNone(statement.price)
        self.assertEqual(statement.premium, 0)
        self.assertEqual(statement.total_charged(), 0)

    def test_a_cancelled_lot_can_never_produce_a_sale(self):
        lot = Lot("urn", min_increment=10, closes_at=100)
        lot.place_bid("alice", 50, 10)
        lot.place_bid("bob", 70, 20)
        withdrawn = lot.cancel(30, "seller withdrew the lot")
        self.assertEqual(withdrawn.status, "cancelled")
        self.assertIsNone(withdrawn.winner)
        self.assertIsNone(withdrawn.price)
        self.assertEqual(lot.state(), "cancelled")
        self.assertIsInstance(capture(lambda: lot.settle(100)), AuctionError)
        self.assertIsInstance(capture(lambda: lot.place_bid("carol", 90, 40)), AuctionError)
        self.assertIsNone(lot.winner())

    def test_a_settled_lot_is_frozen(self):
        lot = Lot("relic", min_increment=10, closes_at=100)
        lot.place_bid("alice", 100, 10)
        early = capture(lambda: lot.settle(99))
        self.assertIsInstance(early, AuctionError)
        statement = lot.settle(100)
        self.assertEqual(statement.status, "sold")
        self.assertEqual(statement.price, 100)
        self.assertEqual(lot.state(), "settled")
        self.assertIsInstance(capture(lambda: lot.settle(200)), AuctionError)
        self.assertIsInstance(capture(lambda: lot.place_bid("bob", 200, 150)), AuctionError)
        self.assertIsInstance(capture(lambda: lot.cancel(150, "too late")), AuctionError)
        self.assertIsInstance(capture(lambda: lot.post_deposit("bob", 10)), AuctionError)


if __name__ == "__main__":
    unittest.main()
