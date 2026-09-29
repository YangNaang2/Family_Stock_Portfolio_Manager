"""Real Qt forms exercise domain-compatible inputs without a running network."""

from copy import deepcopy
import csv
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QDate, Qt
from PyQt5.QtWidgets import QApplication, QAbstractItemView, QDialog, QDialogButtonBox, QLabel

from analytics import add_snapshot, set_targets
from data_manager import PortfolioData
from feature_ui import ActivityDialog, AlertDialog, HistoryDialog, TargetsDialog, TradeDialog
from portfolio import build_snapshot
from trading import bootstrap_ledger, record_dividend, record_sell


def sample_data():
    return PortfolioData({
        "아빠": [{"name": "삼성전자", "code": "005930", "purchase_price": "333.3366666666666666666666666666666666667",
                  "quantity": 3, "cost_basis": "1000.01"}],
        "엄마": [{"name": "카카오", "code": "035720", "purchase_price": 50, "quantity": 2}],
    })


def quotes():
    return {"005930": {"price": 400, "stale": False}, "035720": {"price": 60, "stale": False}}


class FeatureDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qtapp = QApplication.instance() or QApplication([])
        cls.qtapp.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.dialogs = []
        self.addCleanup(self.close_dialogs)
        self.warning = self.patch("feature_ui.QMessageBox.warning")
        self.critical = self.patch("feature_ui.QMessageBox.critical")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def dialog(self, cls, *args, **kwargs):
        dialog = cls(*args, **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def close_dialogs(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()

    def holding(self):
        return build_snapshot(sample_data(), quotes(), "아빠").holdings[0]

    def test_partial_sell_estimate_uses_exact_principal_and_accepts_decimal_costs(self):
        holding = self.holding()
        original = deepcopy(holding)
        dialog = self.dialog(TradeDialog, holding)
        self.assertEqual(dialog.qty_input.text(), "1")
        dialog.qty_input.setText("1")
        dialog.price_input.setText("400")
        dialog.fee_input.setText("1")
        dialog.tax_input.setText("2")
        dialog.note_input.setText("  분할 매도  ")
        values = dialog.get_data()
        self.assertEqual(values, dict(price="400", quantity=1, fee="1", tax="2", note="분할 매도", date=QDate.currentDate().toString("yyyy-MM-dd")))
        self.assertIn("+63.66원", dialog.estimate_label.text())
        self.assertIn("333.34원", dialog.estimate_label.text())
        self.assertEqual(holding, original)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_sell_rejects_oversells_nonintegers_and_nonfinite_numbers(self):
        dialog = self.dialog(TradeDialog, self.holding())
        for quantity in ("0", "4", "1.5", "-1", "NaN"):
            with self.subTest(quantity=quantity):
                dialog.qty_input.setText(quantity)
                with self.assertRaises(ValueError):
                    dialog.get_data()
        dialog.qty_input.setText("1")
        for price in ("NaN", "Infinity", "0", "1e999999999", "1e-999999999"):
            with self.subTest(price=price):
                dialog.price_input.setText(price)
                with self.assertRaises(ValueError):
                    dialog.get_data()
        dialog.accept()
        self.warning.assert_called_once()
        self.assertEqual(dialog.result(), QDialog.Rejected)

    def test_sell_allows_minimum_fee_larger_than_proceeds_like_ledger(self):
        dialog = self.dialog(TradeDialog, self.holding())
        dialog.qty_input.setText("1")
        dialog.price_input.setText("1")
        dialog.fee_input.setText("2")
        self.assertEqual(dialog.get_data()["fee"], "2")
        self.assertIn("세후 매도대금 -1.00원", dialog.estimate_label.text())

    def test_dividend_validates_tax_and_keeps_holdings_unchanged(self):
        holding = self.holding()
        dialog = self.dialog(TradeDialog, holding, kind="DIVIDEND")
        self.assertFalse(hasattr(dialog, "qty_input"))
        dialog.amount_input.setText("100.50")
        dialog.tax_input.setText("15.25")
        self.assertEqual(dialog.get_data()["amount"], "100.50")
        self.assertIn("85.25원", dialog.estimate_label.text())
        dialog.tax_input.setText("101")
        with self.assertRaises(ValueError):
            dialog.get_data()
        self.assertEqual(holding.quantity, 3)
        self.assertEqual(holding.cost_basis, Decimal("1000.01"))

    def test_dates_cannot_select_future_and_labels_treat_user_text_as_plain(self):
        data = sample_data()
        data["아빠"][0]["name"] = "<b>이름</b>"
        dialog = self.dialog(TradeDialog, build_snapshot(data, quotes(), "아빠").holdings[0])
        dialog.date_input.setDate(QDate.currentDate().addDays(10))
        self.assertEqual(dialog.date_input.date(), QDate.currentDate())
        user_label = next(label for label in dialog.findChildren(QLabel) if "<b>이름</b>" in label.text())
        self.assertEqual(user_label.textFormat(), Qt.PlainText)
        self.assertEqual(dialog.estimate_label.textFormat(), Qt.PlainText)

    def test_activity_filters_read_only_rows_without_mutating_data(self):
        data = record_sell(sample_data(), "아빠", "005930", 400, 1, fee="1", tax="2", note="부분매도")
        data = record_dividend(data, "아빠", "005930", 100, tax="15", note="분기배당")
        original = deepcopy(data)
        dialog = self.dialog(ActivityDialog, data)
        self.assertEqual(dialog.table.rowCount(), 4)
        self.assertEqual(dialog.table.editTriggers(), QAbstractItemView.NoEditTriggers)
        self.assertGreaterEqual(dialog.table.rowHeight(0), 46)
        self.assertGreaterEqual(dialog.table.columnWidth(3), 140)
        dialog.type_combo.setCurrentIndex(dialog.type_combo.findData("SELL"))
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 1).text(), "매도")
        dialog.search.setText("없는 검색")
        self.assertEqual(dialog.table.rowCount(), 0)
        dialog.type_combo.setCurrentIndex(0)
        dialog.search.setText("분기배당")
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 1).text(), "배당")
        self.assertIn("85.00원", dialog.summary_label.text())
        self.assertEqual(data, original)
        self.assertEqual(data.metadata, original.metadata)

    def test_activity_member_filter_and_csv_export_use_all_member_records(self):
        data = record_dividend(sample_data(), "아빠", "005930", 100, tax=15)
        dialog = self.dialog(ActivityDialog, data, member="아빠")
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog.search.setText("표에 없는 검색")
        path = Path(self.directory.name) / "transactions"
        self.patch("feature_ui.QFileDialog.getSaveFileName", return_value=(str(path), ""))
        dialog.export_data()
        exported = path.with_suffix(".csv")
        self.assertTrue(exported.read_bytes().startswith(b"\xef\xbb\xbf"))
        with exported.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["가족"] == "아빠" for row in rows))
        self.critical.assert_not_called()

    def test_target_weights_include_unheld_existing_targets_and_leave_data_unchanged(self):
        data = set_targets(sample_data(), None, {"000001": "10", "005930": "60"})
        original = deepcopy(data)
        dialog = self.dialog(TargetsDialog, data, quotes())
        self.assertEqual(set(dialog.target_inputs), {"000001", "005930", "035720"})
        self.assertGreaterEqual(dialog.table.rowHeight(0), 46)
        dialog.target_inputs["005930"].setText("55")
        dialog.target_inputs["035720"].setText("25")
        self.assertEqual(dialog.get_targets(), {"000001": "10", "005930": "55", "035720": "25"})
        self.assertIn("미배정 10.00%", dialog.total_label.text())
        self.assertEqual(dialog.table.editTriggers(), QAbstractItemView.NoEditTriggers)
        self.assertEqual(data.metadata, original.metadata)
        self.assertEqual(data, original)

    def test_target_weights_reject_excess_or_invalid_totals_and_allow_clear(self):
        dialog = self.dialog(TargetsDialog, sample_data(), quotes())
        dialog.target_inputs["005930"].setText("75")
        dialog.target_inputs["035720"].setText("30")
        with self.assertRaises(ValueError):
            dialog.get_targets()
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Save).isEnabled())
        dialog.accept()
        self.warning.assert_called_once()
        for invalid in ("NaN", "-1", "101"):
            dialog.target_inputs["005930"].setText(invalid)
            with self.assertRaises(ValueError):
                dialog.get_targets()
        for widget in dialog.target_inputs.values():
            widget.clear()
        self.assertEqual(dialog.get_targets(), {})
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.Save).isEnabled())

    def test_target_current_weights_hidden_for_missing_or_stale_quotes(self):
        for quote_data in ({}, {"005930": {"price": 400, "stale": True}, "035720": {"price": 60}}):
            with self.subTest(quotes=quote_data):
                dialog = self.dialog(TargetsDialog, sample_data(), quote_data)
                for row in range(dialog.table.rowCount()):
                    self.assertEqual(dialog.table.item(row, 1).text(), "미확인")
                    self.assertEqual(dialog.table.item(row, 3).text(), "미확인")

    def test_alert_deduplicates_codes_and_validates_thresholds(self):
        data = sample_data()
        data["엄마"].append(deepcopy(data["아빠"][0]))
        original = deepcopy(data)
        dialog = self.dialog(AlertDialog, data)
        self.assertEqual(dialog.code_combo.count(), 2)
        dialog.code_combo.setCurrentIndex(dialog.code_combo.findData("005930"))
        dialog.direction_combo.setCurrentIndex(dialog.direction_combo.findData("below"))
        dialog.price_input.setText("399.25")
        self.assertEqual(dialog.get_data(), dict(code="005930", direction="below", price="399.25"))
        dialog.price_input.setText("0")
        with self.assertRaises(ValueError):
            dialog.get_data()
        self.assertEqual(data, original)
        self.assertEqual(data.metadata, original.metadata)

    def test_empty_holdings_cannot_create_alert(self):
        dialog = self.dialog(AlertDialog, PortfolioData({"나": []}))
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Save).isEnabled())
        with self.assertRaises(ValueError):
            dialog.get_data()

    def test_history_scopes_and_chart_use_saved_valuations_without_mutation(self):
        data = add_snapshot(sample_data(), quotes(), date="2024-01-01")
        data = add_snapshot(data, {"005930": {"price": 410}, "035720": {"price": 55}}, date="2024-01-02")
        data = add_snapshot(data, quotes(), member="아빠", date="2024-01-01")
        original = deepcopy(data)
        dialog = self.dialog(HistoryDialog, data)
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertEqual(len(dialog.figure.axes[0].lines), 2)
        self.assertEqual(list(dialog.figure.axes[0].lines[0].get_ydata()), [1320.0, 1340.0])
        self.assertEqual(dialog.figure.axes[0].yaxis.get_major_formatter()(1320, 0), "1,320")
        scoped = self.dialog(HistoryDialog, data, member="아빠")
        self.assertEqual(scoped.table.rowCount(), 1)
        self.assertTrue(any("수익률을 뜻하지 않습니다" in label.text() for label in dialog.findChildren(QLabel)))
        self.assertEqual(data.metadata, original.metadata)
        self.assertEqual(data, original)

    def test_history_csv_export_and_empty_history(self):
        data = add_snapshot(sample_data(), quotes(), member="아빠", date="2024-01-01")
        dialog = self.dialog(HistoryDialog, data, member="아빠")
        path = Path(self.directory.name) / "history.csv"
        self.patch("feature_ui.QFileDialog.getSaveFileName", return_value=(str(path), ""))
        dialog.export_data()
        with path.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["가족"], "아빠")
        self.assertEqual(rows[0]["평가금액(원)"], "1200")
        empty = self.dialog(HistoryDialog, PortfolioData({"나": []}))
        self.assertEqual(empty.table.rowCount(), 0)
        self.critical.assert_not_called()


if __name__ == "__main__":
    unittest.main()
