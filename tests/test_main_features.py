"""Exercise GUI actions through temporary files, real domain logic and fake dialogs."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from analytics import add_snapshot, set_alert, set_targets
from data_manager import load_data, save_data
import main
from trading import record_buy, summarize_activity


def sample_data():
    return {"아빠": [
        {"name": "Alpha", "code": "000001", "purchase_price": "10", "quantity": 2},
        {"name": "Zeta", "code": "000002", "purchase_price": "100", "quantity": 10},
    ], "엄마": [
        {"name": "Alpha", "code": "000001", "purchase_price": "20", "quantity": 100},
    ]}


def sample_quotes():
    return {code: {"price": price, "stale": False, "rate": "+1%", "updated_at": "테스트 시세"}
            for code, price in (("000001", 12), ("000002", 110))}


class MainFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qtapp = QApplication.instance() or QApplication([])
        cls.qtapp.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "portfolio.json"
        save_data(sample_data(), self.path)
        self.windows = []
        self.addCleanup(self.close_windows)
        self.prices = self.patch("main.get_current_price", side_effect=AssertionError("unexpected network request"))
        self.market = self.patch("main.get_market_info", return_value={})
        self.warning = self.patch("main.QMessageBox.warning")
        self.critical = self.patch("main.QMessageBox.critical")
        self.patch("main.QMessageBox.information")
        self.patch("main.QMessageBox.question", return_value=QMessageBox.Yes)

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
        self.assertTrue(condition(), "Qt worker did not finish within the test deadline")

    def close_windows(self):
        for window in self.windows:
            window.clock.stop()
            window.auto_timer.stop()
            window.close()
            self.wait_until(lambda: window.worker is None)
            window.deleteLater()
        self.qtapp.processEvents()

    def window(self):
        window = main.StockApp(self.path, auto_refresh=False)
        self.windows.append(window)
        window.quotes = sample_quotes()
        window.render()
        return window

    def family(self, window, member):
        index = window.member_combo.findData(member)
        self.assertGreaterEqual(index, 0)
        window.member_combo.setCurrentIndex(index)

    def select(self, window, member="아빠", code="000001"):
        for row in range(window.table.rowCount()):
            if tuple(window.table.item(row, 0).data(Qt.UserRole)) == (member, code):
                window.table.selectRow(row)
                self.assertEqual(window.selected_key(), (member, code))
                return row
        self.fail(f"missing holding {member}/{code}")

    def dialog(self, name, values, method="get_data"):
        dialog_type = self.patch(f"main.{name}")
        dialog_type.return_value.exec_.return_value = QDialog.Accepted
        getattr(dialog_type.return_value, method).return_value = values
        return dialog_type

    def assert_persisted(self, window):
        stored = load_data(self.path)
        self.assertEqual(stored, window.data)
        self.assertEqual(stored.metadata, window.data.metadata)
        return stored

    def test_buy_dialog_passes_fee_date_and_note_to_persisted_ledger(self):
        window = self.window()
        self.family(window, "아빠")
        day = (date.today() - timedelta(days=2)).isoformat()
        self.dialog("StockDialog", dict(name="Alpha", code="000001", price="11.25", quantity=2,
                                        fee="0.50", date=day, note="추가 매수"))
        window.add_stock()
        self.assertEqual(window.data["아빠"][0]["quantity"], 4)
        self.assertEqual(Decimal(window.data["아빠"][0]["cost_basis"]), Decimal("43"))
        entry = self.assert_persisted(window).metadata["transactions"][-1]
        self.assertEqual((entry["type"], entry["date"], entry["fee"], entry["note"]),
                         ("BUY", day, "0.50", "추가 매수"))
        self.assertEqual(window.member_combo.currentData(), "아빠")
        self.prices.assert_not_called()

    def test_sell_and_dividend_actions_update_holdings_summary_and_file(self):
        window = self.window()
        self.family(window, "아빠")
        self.select(window)
        trade = self.dialog("TradeDialog", dict(price="15", quantity=1, fee="0.25", tax="0.75",
                                              date=date.today().isoformat(), note="일부 매도"))
        window.sell_stock()
        self.assertEqual(trade.call_args.kwargs["kind"], "SELL")
        self.assertEqual(window.data["아빠"][0]["quantity"], 1)
        self.assertEqual(Decimal(window.data["아빠"][0]["cost_basis"]), Decimal("10"))
        self.assertIn("+4원", window.activity_label.text())
        self.select(window)
        trade.return_value.get_data.return_value = dict(amount="100", tax="15", date=date.today().isoformat(), note="배당")
        window.add_dividend()
        self.assertEqual(trade.call_args.kwargs["kind"], "DIVIDEND")
        self.assertIn("85원", window.activity_label.text())
        stored = self.assert_persisted(window)
        self.assertEqual(summarize_activity(stored, "아빠")["realized_profit"], Decimal("4"))
        self.assertEqual(summarize_activity(stored, "아빠")["dividends"], Decimal("85"))
        self.assertEqual(stored["엄마"], sample_data()["엄마"])

    def test_dividend_for_fully_sold_position_uses_history_without_selected_row(self):
        save_data({"나": [sample_data()["아빠"][0]]}, self.path)
        window = self.window()
        self.family(window, "나")
        self.select(window, "나", "000001")
        trade = self.dialog("TradeDialog", dict(price="15", quantity=2, fee="0", tax="0",
                                              date=date.today().isoformat(), note="전량 매도"))
        window.sell_stock()
        self.assertEqual(window.data["나"], [])
        self.assertEqual(window.table.rowCount(), 0)
        self.assertIsNone(window.selected_key())
        picker = self.patch("main.QInputDialog.getItem", return_value=("나 · Alpha (000001)", True))
        trade.return_value.get_data.return_value = dict(amount="100", tax="15", date=date.today().isoformat(), note="매도 후 입금된 배당")
        window.add_dividend()
        self.assertEqual(picker.call_args.args[3], ["나 · Alpha (000001)"])
        self.assertEqual(trade.call_args.kwargs["kind"], "DIVIDEND")
        self.assertEqual(trade.call_args.args[0].quantity, 0)
        stored = self.assert_persisted(window)
        self.assertEqual(stored["나"], [])
        self.assertEqual(stored.metadata["transactions"][-1]["type"], "DIVIDEND")
        self.assertEqual(summarize_activity(stored, "나")["realized_profit"], Decimal("10"))
        self.assertEqual(summarize_activity(stored, "나")["dividends"], Decimal("85"))
        self.assertIn("+10원", window.activity_label.text())
        self.assertIn("85원", window.activity_label.text())

    def test_edit_and_delete_append_adjustments_without_creating_realized_profit(self):
        window = self.window()
        self.select(window)
        self.dialog("StockDialog", dict(name="Alpha", code="000001", price="13.50", quantity=3, reason="입력 정정"))
        window.edit_stock()
        adjustment = window.data.metadata["transactions"][-1]
        self.assertEqual((adjustment["type"], adjustment["previous_quantity"], adjustment["note"]),
                         ("ADJUSTMENT", "2", "입력 정정"))
        self.select(window)
        window.delete_stock()
        self.assertEqual(window.data.metadata["transactions"][-1]["type"], "REMOVE")
        self.assertEqual([row["code"] for row in window.data["아빠"]], ["000002"])
        self.assertEqual(summarize_activity(window.data)["realized_profit"], Decimal("0"))
        self.assert_persisted(window)

    def test_unchanged_edit_does_not_round_principal_or_append_a_correction(self):
        window = self.window()
        data = record_buy(window.data, "아빠", "Alpha", "000001", "10.01", 1)
        self.assertTrue(window.persist(data))
        self.select(window)
        holding = window.selected_holding()
        original = deepcopy(window.data)
        raw = self.path.read_bytes()
        self.dialog("StockDialog", dict(name=holding.name, code=holding.code,
                                        price=str(holding.purchase_price), quantity=holding.quantity,
                                        reason=""))
        window.edit_stock()
        self.assertEqual(window.data, original)
        self.assertEqual(window.data.metadata, original.metadata)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_fresh_snapshot_saves_selected_family_and_replaces_same_day(self):
        window = self.window()
        self.family(window, "아빠")
        window.capture_snapshot()
        snapshot = window.data.metadata["snapshots"][0]
        self.assertEqual(snapshot["member"], "아빠")
        self.assertEqual(Decimal(snapshot["market_value"]), window.snapshot().market_value)
        previous_id = snapshot["id"]
        window.quotes["000002"]["price"] = 120
        window.capture_snapshot()
        records = self.assert_persisted(window).metadata["snapshots"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], previous_id)
        self.assertEqual(Decimal(records[0]["market_value"]), Decimal("1224"))

    def test_snapshot_missing_or_stale_quotes_preserves_previous_file(self):
        window = self.window()
        original = self.path.read_bytes()
        for quotes in [{"000001": sample_quotes()["000001"]},
                       {code: dict(quote, stale=True) for code, quote in sample_quotes().items()}]:
            with self.subTest(quotes=quotes):
                window.quotes = quotes
                window.capture_snapshot()
                self.assertEqual(window.data.metadata["snapshots"], [])
                self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.warning.call_count, 2)

    def test_target_dialog_saves_scope_and_rejects_invalid_percentages(self):
        window = self.window()
        self.family(window, "아빠")
        dialog = self.dialog("TargetsDialog", {"000001": "40", "000002": "60"}, "get_targets")
        window.edit_targets()
        self.assertEqual(dialog.call_args.kwargs["member"], "아빠")
        self.assertEqual(self.assert_persisted(window).metadata["targets"],
                         {"아빠": {"000001": "40", "000002": "60"}})
        original = self.path.read_bytes()
        dialog.return_value.get_targets.return_value = {"000001": "101"}
        window.edit_targets()
        self.warning.assert_called_once()
        self.assertEqual(self.path.read_bytes(), original)

    def test_add_member_has_one_dropdown_entry_and_keeps_it_selected(self):
        window = self.window()
        self.patch("main.QInputDialog.getText", return_value=("  동생  ", True))
        window.add_member()
        self.assertEqual(window.data["동생"], [])
        items = [window.member_combo.itemData(index) for index in range(window.member_combo.count())]
        self.assertEqual(items.count("동생"), 1)
        self.assertEqual(window.member_combo.count(), len(window.data) + 1)
        self.assertEqual(window.member_combo.currentData(), "동생")
        self.assert_persisted(window)
        window.add_member()
        self.warning.assert_called_once()
        self.assertEqual(window.member_combo.count(), len(window.data) + 1)

    def test_rename_updates_ledger_targets_snapshot_and_selected_dropdown(self):
        window = self.window()
        data = record_buy(window.data, "아빠", "Alpha", "000001", "11", 1)
        data = set_targets(data, "아빠", {"000001": "50"})
        data = add_snapshot(data, sample_quotes(), "아빠")
        self.assertTrue(window.persist(data))
        self.family(window, "아빠")
        self.patch("main.QInputDialog.getText", return_value=("아버지", True))
        window.rename_selected_member()
        self.assertNotIn("아빠", window.data)
        self.assertIn("아버지", window.data)
        self.assertEqual(window.member_combo.currentData(), "아버지")
        self.assertEqual(window.member_combo.findData("아빠"), -1)
        metadata = self.assert_persisted(window).metadata
        self.assertIn("아버지", metadata["targets"])
        self.assertNotIn("아빠", metadata["targets"])
        self.assertTrue(all(entry["member"] != "아빠" for entry in metadata["transactions"] + metadata["snapshots"]))
        self.assertEqual(metadata["snapshots"][0]["member"], "아버지")

    def test_timer_skips_busy_worker_and_manual_refreshes_coalesce(self):
        window = self.window()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(code):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test provider was not released")
            return sample_quotes()[code]

        self.prices.side_effect = blocked
        window.interval_combo.setCurrentIndex(window.interval_combo.findData(60))
        self.assertTrue(window.auto_timer.isActive())
        self.assertEqual(window.auto_timer.interval(), 60_000)
        window.auto_refresh_quotes()
        self.wait_until(entered.is_set)
        original_worker = window.worker
        for _ in range(3):
            window.auto_refresh_quotes()
        self.assertIs(window.worker, original_worker)
        self.assertFalse(window._pending_refresh)
        for _ in range(3):
            window.refresh_quotes()
        self.assertTrue(window._pending_refresh)
        release.set()
        self.wait_until(lambda: window.worker is None and not window._pending_refresh)
        self.assertEqual(self.prices.call_count, 4)  # Two unique codes, two coalesced batches.
        window.interval_combo.setCurrentIndex(0)
        self.assertFalse(window.auto_timer.isActive())

    def test_backup_and_import_keep_metadata_and_active_file_revision(self):
        window = self.window()
        self.assertTrue(window.persist(record_buy(window.data, "아빠", "Alpha", "000001", "11", 1)))
        before = deepcopy(window.data)
        active_revision = window.data._revision
        backup = Path(self.directory.name) / "manual-backup.json"
        self.patch("main.QFileDialog.getSaveFileName", return_value=(str(backup), "JSON (*.json)"))
        window.backup_data()
        self.assertEqual(load_data(backup).metadata, before.metadata)
        self.assertEqual(window.data._revision, active_revision)
        self.assertEqual(window.data._source_path, self.path.resolve())

        import_path = Path(self.directory.name) / "incoming.json"
        imported = record_buy({"동생": []}, "동생", "Beta", "000003", "30", 2, fee="1")
        save_data(imported, import_path)
        self.patch("main.QFileDialog.getOpenFileName", return_value=(str(import_path), "JSON (*.json)"))
        with mock.patch.object(window, "refresh_quotes") as refresh:
            window.import_data()
        refresh.assert_called_once()
        self.assertEqual(window.data, imported)
        self.assertEqual(window.data.metadata, imported.metadata)
        self.assertEqual(window.data._source_path, self.path.resolve())
        self.assertEqual(window.data._revision, load_data(self.path)._revision)
        self.assertEqual(load_data(str(self.path) + ".bak").metadata, before.metadata)
        self.assertEqual(window.quotes, {})
        self.assertIsNone(window.member_combo.currentData())
        # Imported content must still detect changes to the active file, rather
        # than accidentally retaining the incoming file's revision metadata.
        save_data({"외부 변경": []}, self.path)
        stale_candidate = record_buy(window.data, "동생", "Beta", "000003", "31", 1)
        self.assertFalse(window.persist(stale_candidate))
        self.critical.assert_called_once()
        self.assertEqual(load_data(self.path), {"외부 변경": []})

    def test_invalid_sell_does_not_change_ledger_memory_or_disk(self):
        window = self.window()
        self.select(window)
        original = deepcopy(window.data)
        raw = self.path.read_bytes()
        self.dialog("TradeDialog", dict(price="10", quantity=3, fee="0", tax="0", date=date.today().isoformat(), note=""))
        window.sell_stock()
        self.warning.assert_called_once()
        self.assertEqual(window.data, original)
        self.assertEqual(window.data.metadata, original.metadata)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_privacy_masks_amounts_without_changing_sorting_or_underlying_values(self):
        window = self.window()
        original = deepcopy(window.data)
        snapshot = window.snapshot()
        window.privacy.setChecked(True)
        self.assertTrue(all(label.text() == "••••••" for label in window.card_values))
        self.assertIn("금액 숨김", window.activity_label.text())
        for row in range(window.table.rowCount()):
            for column in range(2, 8):
                self.assertEqual(window.table.item(row, column).text(), "••••")
                self.assertEqual(window.table.item(row, column).toolTip(), "")
        window.table.sortItems(2, Qt.AscendingOrder)
        self.assertEqual([window.table.item(row, 2).sort_value for row in range(3)], [2, 10, 100])
        self.assertEqual(window.snapshot(), snapshot)
        self.assertEqual(window.data, original)
        self.assertEqual(window.data.metadata, original.metadata)
        window.privacy.setChecked(False)
        self.assertEqual([window.table.item(row, 2).text() for row in range(3)], ["2", "10", "100"])

    def test_alert_beeps_once_per_confirmed_crossing_and_ignores_unknown_quotes(self):
        window = self.window()
        self.assertTrue(window.persist(set_alert(window.data, "000001", "above", "15")))
        beep = self.patch("main.QApplication.beep")
        window.quotes["000001"] = dict(price=16, stale=True)
        window.update_alerts(notify=True)
        beep.assert_not_called()
        window.quotes["000001"] = dict(price=15, stale=False)
        window.update_alerts(notify=True)
        self.assertEqual(beep.call_count, 1)
        window.update_alerts(notify=True)
        self.assertEqual(beep.call_count, 1)
        for unknown in [None, dict(price=16, stale=True), dict(price=None, stale=False)]:
            with self.subTest(unknown=unknown):
                if unknown is None:
                    window.quotes.pop("000001", None)
                else:
                    window.quotes["000001"] = unknown
                window.update_alerts(notify=True)
                self.assertTrue(window.alert_label.isHidden())
                window.quotes["000001"] = dict(price=16, stale=False)
                window.update_alerts(notify=True)
                self.assertEqual(beep.call_count, 1)
        window.quotes["000001"] = dict(price=14, stale=False)
        window.update_alerts(notify=True)
        window.quotes["000001"] = dict(price=16, stale=False)
        window.update_alerts(notify=True)
        self.assertEqual(beep.call_count, 2)
        window.privacy.setChecked(True)
        self.assertIn("금액 숨김", window.alert_label.text())
        self.assertNotIn("16원", window.alert_label.text())


if __name__ == "__main__":
    unittest.main()
