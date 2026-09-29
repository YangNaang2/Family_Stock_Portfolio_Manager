"""Exercise the actual QThread/executor without a GUI or network access."""

from collections import Counter
from datetime import datetime
import threading
import unittest
from unittest.mock import patch

from PyQt5.QtCore import Qt

from main import QuoteWorker


class WorkerTests(unittest.TestCase):
    def collect_results(self, worker):
        results = []
        # No GUI receiver/event loop is needed for this thread-safe list append.
        worker.completed.connect(lambda *args: results.append(args), Qt.DirectConnection)
        return results

    def test_deduplicates_codes_and_limits_concurrent_requests(self):
        codes = [f"{index:06d}" for index in range(12)]
        worker = QuoteWorker(codes + codes[:3])
        results = self.collect_results(worker)
        lock, release, first_wave = threading.Lock(), threading.Event(), threading.Event()
        calls, thread_ids = [], set()
        active = maximum = 0

        def provider(code=None):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls.append(code)
                thread_ids.add(threading.get_ident())
                if active == 4:
                    first_wave.set()
            release.wait(3)
            with lock:
                active -= 1
            return {"KOSPI": "2,500.00"} if code is None else {"price": 75000, "rate": "+1.23%"}

        with patch("main.get_current_price", side_effect=provider), patch("main.get_market_info", side_effect=provider):
            worker.start()
            try:
                self.assertTrue(first_wave.wait(3), "four requests should run concurrently")
            finally:
                release.set()
                finished = worker.wait(5000)
            self.assertTrue(finished)
        self.assertEqual(maximum, 4)
        self.assertNotIn(threading.get_ident(), thread_ids)
        self.assertEqual(Counter(calls), Counter(codes + [None]))
        self.assertEqual(len(results), 1)
        quotes, market = results[0]
        self.assertEqual(set(quotes), set(codes))
        self.assertEqual(market, {"KOSPI": "2,500.00"})
        for quote in quotes.values():
            self.assertEqual(quote["price"], 75000)
            self.assertFalse(quote["stale"])
            datetime.fromisoformat(quote["updated_at"])

    def test_one_provider_failure_does_not_discard_other_quotes(self):
        worker = QuoteWorker(["005930", "000660", "035420"])
        results = self.collect_results(worker)

        def provider(code):
            if code == "005930":
                raise RuntimeError("unexpected provider error")
            if code == "000660":
                return None
            return {"price": 198000, "rate": "-"}

        with patch("main.get_current_price", side_effect=provider), patch("main.get_market_info", side_effect=RuntimeError("market failed")):
            worker.start()
            self.assertTrue(worker.wait(5000))
        self.assertEqual(len(results), 1)
        quotes, market = results[0]
        self.assertIsNone(quotes["005930"])
        self.assertIsNone(quotes["000660"])
        self.assertEqual(quotes["035420"]["price"], 198000)
        self.assertEqual(market, {})

    def test_interruption_waits_for_active_requests_and_suppresses_delivery(self):
        worker = QuoteWorker([f"{index:06d}" for index in range(50)])
        results = self.collect_results(worker)
        started, release = threading.Event(), threading.Event()

        def provider(*args):
            started.set()
            release.wait(3)
            return {"price": 75000, "rate": "0.00%"}

        with patch("main.get_current_price", side_effect=provider), patch("main.get_market_info", return_value={}):
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                worker.requestInterruption()
                self.assertTrue(worker.isRunning())
            finally:
                release.set()
                finished = worker.wait(5000)
            self.assertTrue(finished)
        self.assertEqual(results, [])

    def test_empty_portfolio_still_gets_market_information(self):
        worker = QuoteWorker([])
        results = self.collect_results(worker)
        with patch("main.get_current_price") as stock, patch("main.get_market_info", return_value={"USD": "1,355.00"}) as market:
            worker.start()
            self.assertTrue(worker.wait(5000))
        stock.assert_not_called()
        market.assert_called_once_with()
        self.assertEqual(results, [({}, {"USD": "1,355.00"})])


if __name__ == "__main__":
    unittest.main()
