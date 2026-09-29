import csv
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from data_manager import PortfolioData, load_data, save_data, validate_metadata
from portfolio import ValidationError, build_snapshot
from trading import (
    bootstrap_ledger, correct_holding, export_transactions_csv, record_buy,
    record_dividend, record_sell, remove_holding, summarize_activity,
)


def stock(name="삼성전자", code="005930", price="100", quantity=1, **extra):
    return dict(name=name, code=code, purchase_price=price, quantity=quantity, **extra)


class TradingTests(unittest.TestCase):
    def test_opening_groups_duplicates_and_initializes_all_members_only_once(self):
        original = {"아빠": [stock(quantity=2), stock(price="120")],
                    "엄마": [stock(price="90")], "나": []}
        data = bootstrap_ledger(original)
        self.assertIsInstance(data, PortfolioData)
        self.assertEqual(data, original)
        entries = data.metadata["transactions"]
        self.assertEqual(len(entries), 2)
        self.assertEqual((entries[0]["quantity"], entries[0]["cost_basis"]), ("3", "320"))
        self.assertEqual(entries[0]["date"], date.today().isoformat())
        self.assertIn("이전 매수일이 아닌", entries[0]["note"])
        self.assertTrue(data.metadata["ledger_initialized"])
        self.assertEqual(bootstrap_ledger(data).metadata, data.metadata)
        self.assertFalse(hasattr(original, "metadata"))
        self.assertEqual([entry["type"] for entry in entries], ["OPENING", "OPENING"])
        self.assertEqual(len({UUID(entry["id"]) for entry in entries}), 2)

    def test_buy_capitalizes_fee_and_preserves_original_and_existing_entries(self):
        data = bootstrap_ledger({"나": [stock(quantity=2)], "엄마": []})
        original = deepcopy(data)
        updated = record_buy(data, "나", "새이름", "005930", "105.25", 2, fee="1.50", note="추가 매수")
        position = updated["나"][0]
        self.assertEqual(position["name"], "삼성전자")
        self.assertEqual(position["quantity"], 4)
        self.assertEqual(Decimal(position["cost_basis"]), Decimal("412"))
        self.assertEqual(Decimal(position["purchase_price"]), Decimal("103"))
        entry = updated.metadata["transactions"][-1]
        self.assertEqual(entry["type"], "BUY")
        self.assertEqual(Decimal(entry["amount"]), Decimal("210.50"))
        self.assertEqual(Decimal(entry["cost_basis"]), Decimal("212.00"))
        self.assertEqual(entry["note"], "추가 매수")
        self.assertEqual(data, original)
        self.assertEqual(data.metadata, original.metadata)
        self.assertEqual(updated.metadata["transactions"][:-1], original.metadata["transactions"])
        updated.metadata["transactions"][0]["note"] = "변경"
        self.assertEqual(data.metadata, original.metadata)

    def test_partial_sale_and_full_sale_account_for_fees_tax_and_buy_commission(self):
        data = record_buy({"나": []}, "나", "삼성전자", "005930", "100", 10, fee="10")
        data = record_sell(data, "나", "005930", "120", 4, fee="2", tax="3")
        position = data["나"][0]
        self.assertEqual((position["quantity"], Decimal(position["cost_basis"])), (6, Decimal("606")))
        partial = data.metadata["transactions"][-1]
        self.assertEqual(Decimal(partial["cost_basis"]), Decimal("404"))
        self.assertEqual(Decimal(partial["realized_profit"]), Decimal("71"))
        data = record_sell(data, "나", "005930", "90", 6, fee="1", tax="2")
        self.assertEqual(data["나"], [])
        self.assertEqual(Decimal(data.metadata["transactions"][-1]["cost_basis"]), Decimal("606"))
        self.assertEqual(summarize_activity(data), {
            "realized_profit": Decimal("2"), "dividends": Decimal("0"),
            "fees": Decimal("13"), "taxes": Decimal("5"),
        })

    def test_repeating_average_sells_retain_exact_residual_principal(self):
        data = {"나": [stock(price="0.3333333333333333", quantity=3, cost_basis="1")]}
        with localcontext() as context:
            context.prec = 5
            data = record_sell(data, "나", "005930", "1.05", 1)
            first_cost = Decimal(data.metadata["transactions"][-1]["cost_basis"])
            remainder = Decimal(data["나"][0]["cost_basis"])
            data = record_sell(data, "나", "005930", "1.05", 1)
            data = record_sell(data, "나", "005930", "1.05", 1)
            self.assertEqual(summarize_activity(data)["realized_profit"], Decimal("2.15"))
        with localcontext() as context:
            context.prec = 160
            self.assertEqual(first_cost + remainder, Decimal("1"))
            allocated = sum(Decimal(entry["cost_basis"]) for entry in data.metadata["transactions"] if entry["type"] == "SELL")
            self.assertEqual(allocated, Decimal("1"))
        self.assertEqual(data["나"], [])

    def test_oversell_rolls_back_holdings_and_bootstrap(self):
        original = {"나": [stock(quantity=3)]}
        with self.assertRaisesRegex(ValidationError, "초과"):
            record_sell(original, "나", "005930", "120", 4)
        self.assertFalse(hasattr(original, "metadata"))
        self.assertEqual(original["나"][0]["quantity"], 3)
        data = bootstrap_ledger(original)
        before = deepcopy(data)
        with self.assertRaises(ValidationError):
            record_sell(data, "나", "005930", "120", 4)
        self.assertEqual(data.metadata, before.metadata)
        self.assertEqual(data, before)

    def test_sell_merges_duplicate_legacy_rows_preserving_optional_stock_fields(self):
        data = {"나": [stock(quantity=2, market="KOSPI"), stock(price="130")], "엄마": [stock()]}
        updated = record_sell(data, "나", "005930", "150", 1)
        self.assertEqual(len(updated["나"]), 1)
        self.assertEqual(updated["나"][0]["market"], "KOSPI")
        self.assertEqual(updated["나"][0]["quantity"], 2)
        self.assertEqual(Decimal(updated["나"][0]["cost_basis"]), Decimal("220"))
        self.assertEqual(summarize_activity(updated, "나")["realized_profit"], Decimal("40"))
        self.assertEqual(summarize_activity(updated, "엄마")["realized_profit"], Decimal("0"))
        self.assertEqual(len(data["나"]), 2)

    def test_dividend_tracks_net_amount_without_changing_holdings(self):
        data = {"나": [stock(quantity=2)]}
        updated = record_dividend(data, "나", "005930", "100.50", tax="15.40", note="분기 배당")
        self.assertEqual(updated, data)
        entry = updated.metadata["transactions"][-1]
        self.assertEqual(entry["type"], "DIVIDEND")
        self.assertEqual(entry["amount"], "100.50")
        self.assertEqual(entry["realized_profit"], "85.10")
        self.assertEqual(entry["quantity"], "0")
        self.assertEqual(summarize_activity(updated)["realized_profit"], Decimal("0"))
        self.assertEqual(summarize_activity(updated)["dividends"], Decimal("85.10"))
        self.assertEqual(summarize_activity(updated)["taxes"], Decimal("15.40"))

    def test_dividend_can_follow_full_sale_but_requires_owned_or_historical_stock(self):
        data = record_sell({"나": [stock()]}, "나", "005930", "120", 1)
        updated = record_dividend(data, "나", "005930", "20", tax="3")
        self.assertEqual(updated["나"], [])
        self.assertEqual(updated.metadata["transactions"][-1]["name"], "삼성전자")
        self.assertEqual(summarize_activity(updated)["dividends"], Decimal("17"))
        with self.assertRaises(ValidationError):
            record_dividend(data, "나", "000001", "20")
        for amount, tax in [(0, 0), (1, 2), (1, -1), (True, 0)]:
            with self.subTest(amount=amount, tax=tax), self.assertRaises(ValidationError):
                record_dividend(data, "나", "005930", amount, tax=tax)

    def test_adjustment_and_removal_preserve_audit_values_without_fake_profits(self):
        data = {"나": [stock(quantity=2), stock(price="130")]}
        updated = correct_holding(data, "나", "005930", "110.25", 4, reason="이전 입력 오류")
        entry = updated.metadata["transactions"][-1]
        self.assertEqual(entry["type"], "ADJUSTMENT")
        self.assertEqual(entry["previous_quantity"], "3")
        self.assertEqual(entry["previous_cost_basis"], "330")
        self.assertEqual(Decimal(entry["cost_basis"]), Decimal("441"))
        self.assertEqual(entry["note"], "이전 입력 오류")
        removed = remove_holding(updated, "나", "005930")
        self.assertEqual(removed["나"], [])
        entry = removed.metadata["transactions"][-1]
        self.assertEqual(entry["type"], "REMOVE")
        self.assertEqual(entry["previous_quantity"], "4")
        self.assertEqual(Decimal(entry["previous_cost_basis"]), Decimal("441"))
        self.assertEqual(summarize_activity(removed), dict.fromkeys(("realized_profit", "dividends", "fees", "taxes"), Decimal("0")))

    def test_trade_dates_allow_first_historical_entry_then_enforce_position_chronology(self):
        early = (date.today() - timedelta(days=30)).isoformat()
        late = (date.today() - timedelta(days=20)).isoformat()
        data = record_buy({"나": [stock()], "엄마": [stock()]}, "나", "삼성전자", "005930", "100", 1, date=late)
        self.assertEqual(data.metadata["transactions"][0]["date"], date.today().isoformat())
        self.assertEqual(data.metadata["transactions"][-1]["date"], late)
        with self.assertRaisesRegex(ValidationError, "마지막 거래일"):
            record_sell(data, "나", "005930", "110", 1, date=early)
        # Another member's position and another code have independent histories.
        data = record_sell(data, "엄마", "005930", "110", 1, date=early)
        data = record_buy(data, "나", "카카오", "035720", "50", 1, date=early)
        data = record_sell(data, "나", "005930", "110", 1, date=late)
        self.assertEqual(data.metadata["transactions"][-1]["date"], late)

    def test_closed_position_reentry_still_obeys_ledger_chronology(self):
        late = date.today().isoformat()
        early = (date.today() - timedelta(days=1)).isoformat()
        data = remove_holding({"나": [stock()]}, "나", "005930", date=late)
        with self.assertRaises(ValidationError):
            record_buy(data, "나", "삼성전자", "005930", "100", 1, date=early)
        self.assertEqual(data["나"], [])

    def test_dates_and_numeric_inputs_reject_invalid_values(self):
        data = {"나": [stock(quantity=2)]}
        for bad_date in ["2026-02-30", "2026-2-01", "20260901", "", True,
                         (date.today() + timedelta(days=1)).isoformat()]:
            with self.subTest(date=bad_date), self.assertRaises(ValidationError):
                record_sell(data, "나", "005930", "100", 1, date=bad_date)
        for fee in ["NaN", "Infinity", "-1", "1e99999", "1e-100", True, {}, "1" * 161]:
            with self.subTest(fee=fee), self.assertRaises(ValidationError):
                record_sell(data, "나", "005930", "100", 1, fee=fee)
        for quantity in [0, -1, 1.5, True]:
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                record_sell(data, "나", "005930", "100", quantity)
        with self.assertRaises(ValidationError):
            record_buy(data, "나", "삼성전자", "005930", "1e12", 1, fee="1e24")

    def test_zero_fees_with_extreme_exponent_do_not_expand_into_huge_strings(self):
        data = record_buy({"나": []}, "나", "삼성전자", "005930", "100", 1, fee="0e99999999")
        self.assertEqual(data.metadata["transactions"][-1]["fee"], "0")

    def test_sell_fees_can_exceed_proceeds_with_explicit_negative_profit(self):
        data = record_sell({"나": [stock(price="1")]}, "나", "005930", "0.1", 1, fee="1")
        self.assertEqual(summarize_activity(data)["realized_profit"], Decimal("-1.9"))

    def test_unknown_member_and_position_rejected(self):
        data = {"나": [stock()]}
        with self.assertRaises(ValidationError):
            record_sell(data, "없음", "005930", "100", 1)
        with self.assertRaises(ValidationError):
            correct_holding(data, "나", "000001", "100", 1)
        with self.assertRaises(ValidationError):
            remove_holding(data, "나", "000001")
        with self.assertRaises(ValidationError):
            summarize_activity(data, "없음")

    def test_summary_of_legacy_data_is_zero_without_mutating_or_bootstrapping(self):
        data = {"나": [stock()]}
        self.assertEqual(summarize_activity(data), {
            "realized_profit": Decimal("0"), "dividends": Decimal("0"),
            "fees": Decimal("0"), "taxes": Decimal("0"),
        })
        self.assertFalse(hasattr(data, "metadata"))

    def test_transactions_round_trip_with_revision_and_exact_remaining_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            save_data({"나": [stock(price="0.333333333333333", quantity=3, cost_basis="1")]}, path)
            data = load_data(path)
            sold = record_sell(data, "나", "005930", "1", 1)
            self.assertEqual(sold._revision, data._revision)
            save_data(sold, path)
            reloaded = load_data(path)
            self.assertEqual(reloaded, sold)
            self.assertEqual(reloaded.metadata, sold.metadata)
            reloaded = record_sell(reloaded, "나", "005930", "1", 2)
            save_data(reloaded, path)
            self.assertEqual(summarize_activity(load_data(path))["realized_profit"], Decimal("2"))
            self.assertTrue(Path(str(path) + ".bak").exists())

    def test_new_entries_preserve_other_metadata(self):
        data = PortfolioData({"나": []})
        data.metadata["targets"] = {"나": {"005930": "80"}}
        updated = record_buy(data, "나", "삼성전자", "005930", "100", 1)
        self.assertEqual(updated.metadata["targets"], data.metadata["targets"])
        validate_metadata(updated.metadata, updated)

    def test_csv_exports_filter_bom_safe_text_and_real_negative_numbers(self):
        data = {"=가족": [stock(name="+위험")], "엄마": [stock()]}
        data = record_sell(data, "=가족", "005930", "90", 1, note="\t=HYPERLINK(1)")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transactions.csv"
            export_transactions_csv(data, path, "=가족")
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["가족"], "'=가족")
        self.assertEqual(rows[-1]["종목명"], "'+위험")
        self.assertEqual(rows[-1]["종목코드"], "005930")
        self.assertEqual(rows[-1]["실현손익·순배당(원)"], "-10")
        self.assertEqual(rows[-1]["메모"], "'=HYPERLINK(1)")
        self.assertEqual(rows[-1]["정정전수량"], "")

    def test_several_sales_preserve_total_economic_profit(self):
        data = record_buy({"나": []}, "나", "삼성전자", "005930", "100.01", 13, fee="0.13")
        original_cost = build_snapshot(data, {}).total_cost
        for quantity in [1, 2, 3, 7]:
            data = record_sell(data, "나", "005930", "110.07", quantity, fee="0.01", tax="0.02")
        expected = Decimal("110.07") * 13 - original_cost - Decimal("0.12")
        self.assertEqual(summarize_activity(data)["realized_profit"], expected)
        self.assertEqual(data["나"], [])


if __name__ == "__main__":
    unittest.main()
