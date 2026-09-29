"""Qt integration tests using temporary portfolios and mocked quote providers."""

from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

# Make the ordinary unittest command usable on machines without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from data_manager import DataError, load_data, save_data
import main


def sample_data():
    return {
        "아빠": [
            {"name": "Alpha", "code": "000001", "purchase_price": 10, "quantity": 2},
            {"name": "Zeta", "code": "000002", "purchase_price": 100, "quantity": 10},
        ],
        "엄마": [
            {"name": "Alpha", "code": "000001", "purchase_price": 20, "quantity": 100},
            {"name": "Beta", "code": "000003", "purchase_price": 200, "quantity": 3},
        ],
    }


def sample_quotes():
    return {
        code: {"price": price, "rate": "+1.00%", "stale": False, "updated_at": "2026-01-01T10:00:00"}
        for code, price in (("000001", 12), ("000002", 110), ("000003", 220))
    }


class StockAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qtapp = QApplication.instance() or QApplication([])
        cls.qtapp.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test-portfolio.json"
        self.original = sample_data()
        save_data(self.original, self.path)
        self.windows = []
        self.addCleanup(self.close_windows)
        # A forgotten mock cannot send a real request or open a blocking dialog.
        self.price_mock = self.patch("main.get_current_price", side_effect=AssertionError("unmocked quote request"))
        self.market_mock = self.patch("main.get_market_info", return_value={})
        self.critical_mock = self.patch("main.QMessageBox.critical")
        self.patch("main.QMessageBox.warning")
        self.patch("main.QMessageBox.information")
        self.question_mock = self.patch("main.QMessageBox.question", return_value=QMessageBox.Yes)

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def wait_until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.qtapp.processEvents()
            QTest.qWait(10)
        self.qtapp.processEvents()
        self.assertTrue(condition(), "Qt operation did not finish before the test deadline")

    def window(self, *, demo=False, quotes=True):
        window = main.StockApp(self.path, demo=demo, auto_refresh=False)
        self.windows.append(window)
        if not demo and quotes:
            window.quotes = sample_quotes()
            window.render()
        return window

    def close_windows(self):
        for window in self.windows:
            window.clock.stop()
            window.close()
            self.wait_until(lambda: window.worker is None)
            window.deleteLater()
        self.qtapp.processEvents()

    def select(self, window, key):
        for row in range(window.table.rowCount()):
            if tuple(window.table.item(row, 0).data(Qt.UserRole)) == key:
                window.table.selectRow(row)
                self.assertEqual(window.selected_key(), key)
                return row
        self.fail(f"Holding {key} is missing from the table")

    def family(self, window, member):
        index = window.member_combo.findData(member)
        self.assertGreaterEqual(index, 0)
        window.member_combo.setCurrentIndex(index)

    def blocked_provider(self, responses):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def fetch(code):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test quote provider was not released")
            response = responses.get(code)
            if isinstance(response, Exception):
                raise response
            return response

        self.price_mock.side_effect = fetch
        return entered, release

    def test_all_family_search_and_member_filter_have_correct_summary_scope(self):
        window = self.window()
        self.assertIsNone(window.member_combo.currentData())
        self.assertEqual(window.table.rowCount(), 4)
        full_total = window.card_values[0].text()
        self.assertEqual(window.snapshot().total_cost, Decimal(3620))
        window.search.setText("alpha")
        self.assertEqual(window.table.rowCount(), 2)
        self.assertEqual(window.card_values[0].text(), full_total)
        self.family(window, "엄마")
        self.assertEqual(window.table.rowCount(), 1)
        self.assertEqual(window.table.item(0, 0).text(), "엄마")
        self.assertEqual(window.snapshot().total_cost, Decimal(2600))
        window.search.setText("000003")
        self.assertEqual(window.table.rowCount(), 1)
        self.assertIn("Beta", window.table.item(0, 1).text())
        window.search.setText("does not exist")
        self.assertEqual(window.table.rowCount(), 0)
        self.assertEqual(window.snapshot().total_cost, Decimal(2600))

    def test_numeric_sort_and_render_preserve_selected_holding_identity(self):
        window = self.window()
        window.table.sortItems(2, Qt.AscendingOrder)
        self.assertEqual([window.table.item(row, 2).text() for row in range(4)], ["2", "3", "10", "100"])
        key = ("엄마", "000001")
        self.select(window, key)
        window.table.sortItems(3, Qt.DescendingOrder)
        self.assertEqual(window.selected_key(), key)
        window.render()
        self.assertEqual(window.selected_key(), key)
        window.search.setText("Alpha")
        self.assertEqual(window.selected_key(), key)
        self.assertEqual(window.table.rowCount(), 2)
        window.search.setText("no match")
        self.assertIsNone(window.selected_key())

    def test_delete_after_search_and_sort_removes_only_selected_owner(self):
        window = self.window()
        window.search.setText("Alpha")
        window.table.sortItems(2, Qt.DescendingOrder)
        self.select(window, ("엄마", "000001"))
        window.delete_stock()
        self.question_mock.assert_called_once()
        self.assertEqual(window.data["아빠"], self.original["아빠"])
        self.assertEqual([stock["code"] for stock in window.data["엄마"]], ["000003"])
        self.assertEqual(load_data(self.path), window.data)
        self.assertEqual(load_data(str(self.path) + ".bak"), self.original)

    def test_edit_after_search_and_sort_changes_only_selected_owner(self):
        window = self.window()
        window.search.setText("Alpha")
        window.table.sortItems(2, Qt.DescendingOrder)
        self.select(window, ("엄마", "000001"))
        with mock.patch("main.StockDialog") as dialog_type:
            dialog_type.return_value.exec_.return_value = QDialog.Accepted
            dialog_type.return_value.get_data.return_value = {
                "name": "Alpha", "code": "000001", "price": "25.50", "quantity": 7,
            }
            window.edit_stock()
        dialog_stock = dialog_type.call_args.kwargs["stock"]
        self.assertEqual(dialog_stock["code"], "000001")
        self.assertEqual(dialog_stock["quantity"], 100)
        self.assertEqual(Decimal(str(dialog_stock["purchase_price"])), Decimal(20))
        self.assertEqual(window.data["아빠"], self.original["아빠"])
        edited = window.data["엄마"][0]
        self.assertEqual(edited["quantity"], 7)
        self.assertEqual(Decimal(edited["cost_basis"]), Decimal("178.50"))
        self.assertEqual(load_data(self.path), window.data)
        self.assertEqual(window.selected_key(), ("엄마", "000001"))

    def test_failed_save_keeps_memory_and_table_and_disk_unchanged(self):
        window = self.window()
        self.select(window, ("아빠", "000002"))
        original_data = window.data
        before = deepcopy(window.data)
        with mock.patch("main.save_data", side_effect=DataError("simulated storage failure")):
            window.delete_stock()
        self.assertIs(window.data, original_data)
        self.assertEqual(window.data, before)
        self.assertEqual(window.table.rowCount(), 4)
        self.assertEqual(window.selected_key(), ("아빠", "000002"))
        self.assertEqual(load_data(self.path), before)
        self.critical_mock.assert_called_once()

    def test_demo_changes_neither_read_nor_write_data_file_or_fetch_quotes(self):
        original_bytes = self.path.read_bytes()
        with mock.patch("main.load_data", side_effect=AssertionError("demo read the data file")):
            with mock.patch("main.save_data", side_effect=AssertionError("demo wrote the data file")):
                window = self.window(demo=True)
                self.assertFalse(window.refresh_button.isEnabled())
                self.select(window, ("엄마", "005930"))
                window.delete_stock()
                window.refresh_quotes()
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.price_mock.assert_not_called()
        self.market_mock.assert_not_called()

    def test_async_refresh_allows_family_switch_and_queries_duplicate_code_once(self):
        window = self.window(quotes=False)
        entered, release = self.blocked_provider(sample_quotes())
        self.market_mock.return_value = {"KOSPI": "2,500", "KOSPI_RATE": "+1.0%"}
        window.refresh_quotes()
        self.wait_until(entered.is_set)
        self.assertIsNotNone(window.worker)
        self.assertFalse(window.refresh_button.isEnabled())
        self.family(window, "엄마")
        self.assertEqual(window.table.rowCount(), 2)
        release.set()
        self.wait_until(lambda: window.worker is None)
        self.assertTrue(window.refresh_button.isEnabled())
        self.assertEqual(window.member_combo.currentData(), "엄마")
        self.assertEqual({holding.member for holding in window.snapshot().holdings}, {"엄마"})
        self.assertEqual(window.snapshot().market_value, Decimal(1860))
        self.assertIn("2,500", window.market_label.text())
        self.assertEqual(sorted(call.args[0] for call in self.price_mock.call_args_list), ["000001", "000002", "000003"])
        self.assertFalse(any(quote["stale"] for quote in window.quotes.values()))

    def test_refresh_failure_preserves_old_quote_as_stale_and_never_fabricates_prices(self):
        window = self.window(quotes=False)
        window.quotes = {"000001": sample_quotes()["000001"]}
        window.render()
        def offline(code):
            raise RuntimeError("offline")

        self.price_mock.side_effect = offline
        window.refresh_quotes()
        self.wait_until(lambda: window.worker is None)
        snapshot = window.snapshot()
        self.assertEqual(snapshot.stale_count, 2)
        self.assertEqual(snapshot.missing_count, 2)
        self.assertEqual(window.quotes["000001"]["price"], 12)
        self.assertEqual(window.quotes["000001"]["updated_at"], "2026-01-01T10:00:00")
        self.assertTrue(window.quotes["000001"]["stale"])
        self.assertNotIn("000002", window.quotes)
        self.assertNotIn("000003", window.quotes)
        for row in range(window.table.rowCount()):
            code = window.table.item(row, 0).data(Qt.UserRole)[1]
            if code != "000001":
                self.assertEqual(window.table.item(row, 4).text(), "—")
                self.assertEqual(window.table.item(row, 6).text(), "—")
                self.assertEqual(window.table.item(row, 8).text(), "미확인")
        self.assertEqual(snapshot.market_value, Decimal(1224))
        self.assertIn("이전 시세", window.notice_label.text())
        self.assertIn("미확인", window.notice_label.text())

    def test_no_quotes_displays_unknown_totals_instead_of_zero_return(self):
        window = self.window(quotes=False)
        self.assertEqual(window.card_values[0].text(), "3,620원")
        self.assertEqual([label.text() for label in window.card_values[1:]], ["—", "—", "—"])
        self.assertEqual(window.snapshot().missing_count, 4)

    def test_close_during_refresh_cancels_worker_before_window_destruction(self):
        window = self.window(quotes=False)
        entered, release = self.blocked_provider(sample_quotes())
        window.show()
        window.refresh_quotes()
        self.wait_until(entered.is_set)
        self.assertFalse(window.close())
        self.assertTrue(window._closing)
        self.assertTrue(window.worker.isInterruptionRequested())
        self.assertFalse(window.isEnabled())
        release.set()
        self.wait_until(lambda: window.worker is None)
        self.assertFalse(window.isVisible())
        self.assertEqual(window.quotes, {})


if __name__ == "__main__":
    unittest.main()
