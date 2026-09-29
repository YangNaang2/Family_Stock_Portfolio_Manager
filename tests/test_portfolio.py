import csv
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import tempfile
import unittest

from portfolio import (
    ValidationError, build_snapshot, buy_stock, export_csv, remove_stock,
    update_stock, validate_stock_input,
)


def stock(name="삼성전자", code="005930", price=100, quantity=1, **extra):
    return dict(name=name, code=code, purchase_price=price, quantity=quantity, **extra)


class PortfolioTests(unittest.TestCase):
    def test_repeated_buys_preserve_exact_principal_despite_recurring_average(self):
        data = {"아빠": [stock(price=100, quantity=2)], "엄마": []}
        original = deepcopy(data)
        data = buy_stock(data, "아빠", "삼성전자", "005930", "100.01", 1)
        self.assertEqual(data["아빠"][0]["cost_basis"], "300.01")
        expected = Decimal("300.01")
        for count in range(1, 31):
            amount = Decimal("0.07") * count
            data = buy_stock(data, "아빠", "삼성전자", "005930", "0.07", count)
            expected += amount
        position = data["아빠"][0]
        self.assertEqual(Decimal(position["cost_basis"]), expected)
        self.assertIsInstance(position["purchase_price"], str)
        self.assertEqual(original, {"아빠": [stock(price=100, quantity=2)], "엄마": []})
        snapshot = build_snapshot(data, {})
        self.assertEqual(snapshot.total_cost, expected)

    def test_new_entry_for_existing_code_merges_even_legacy_duplicates(self):
        data = {"아빠": [stock(price=100, quantity=2), stock(price=105, quantity=1)], "엄마": [stock()]}
        updated = buy_stock(data, "아빠", "다른 이름", "005930", 110, 2)
        self.assertEqual(len(updated["아빠"]), 1)
        self.assertEqual(updated["아빠"][0]["quantity"], 5)
        self.assertEqual(updated["아빠"][0]["name"], "삼성전자")
        self.assertEqual(Decimal(updated["아빠"][0]["cost_basis"]), Decimal("525"))
        self.assertEqual(Decimal(updated["아빠"][0]["purchase_price"]), Decimal("105"))
        updated["엄마"][0]["name"] = "변경"
        self.assertEqual(data["엄마"][0]["name"], "삼성전자")
        self.assertEqual(len(data["아빠"]), 2)

    def test_edit_deliberately_replaces_cost_and_delete_does_not_mutate_source(self):
        data = {"나": [stock(price="10.333", quantity=3, cost_basis="31")]}
        edited = update_stock(data, "나", "005930", "11.25", 4)
        self.assertEqual(edited["나"][0]["cost_basis"], "45.00")
        self.assertEqual(edited["나"][0]["purchase_price"], "11.25")
        self.assertEqual(data["나"][0]["cost_basis"], "31")
        removed = remove_stock(edited, "나", "005930")
        self.assertEqual(removed["나"], [])
        self.assertEqual(len(edited["나"]), 1)
        with self.assertRaises(ValidationError):
            remove_stock(removed, "나", "005930")

    def test_all_family_aggregation_and_member_filter(self):
        data = {"아빠": [stock(price=100, quantity=2)], "엄마": [stock(price=90, quantity=3)]}
        quotes = {"005930": {"price": 120, "rate": "+1.0%", "updated_at": "2026-09-29 10:00"}}
        snapshot = build_snapshot(data, quotes)
        self.assertEqual(len(snapshot.holdings), 2)
        self.assertEqual(snapshot.total_cost, Decimal("470"))
        self.assertEqual(snapshot.known_cost, Decimal("470"))
        self.assertEqual(snapshot.market_value, Decimal("600"))
        self.assertEqual(snapshot.profit, Decimal("130"))
        self.assertAlmostEqual(float(snapshot.roi), 130 / 470 * 100)
        self.assertEqual((snapshot.missing_count, snapshot.stale_count), (0, 0))
        self.assertEqual(build_snapshot(data, quotes, "엄마").market_value, Decimal("360"))

    def test_snapshot_groups_legacy_duplicates_without_changing_stored_rows(self):
        data = {"나": [stock(price=100, quantity=2), stock(name="다른 이름", price=130, quantity=1)],
                "엄마": [stock(price=90, quantity=1)]}
        original = deepcopy(data)
        snapshot = build_snapshot(data, {"005930": {"price": 120, "stale": True}})
        self.assertEqual(len(snapshot.holdings), 2)
        holding = snapshot.holdings[0]
        self.assertEqual((holding.member, holding.name, holding.quantity), ("나", "삼성전자", 3))
        self.assertEqual(holding.cost_basis, Decimal("330"))
        self.assertEqual(holding.purchase_price, Decimal("110"))
        self.assertEqual(holding.profit, Decimal("30"))
        self.assertEqual(snapshot.total_cost, Decimal("420"))
        self.assertEqual(snapshot.stale_count, 2)
        self.assertEqual(data, original)
        # Editing the aggregate changes only the intended position, preserving
        # all shares instead of using the first legacy row's quantity.
        edited = update_stock(data, holding.member, holding.code, "115", holding.quantity)
        self.assertEqual(edited["나"][0]["quantity"], 3)
        self.assertEqual(edited["나"][0]["cost_basis"], "345")

    def test_large_legacy_aggregate_is_readable_but_buy_cannot_overflow_input_limit(self):
        data = {"나": [stock(quantity=2_000_000_000), stock(quantity=1)]}
        snapshot = build_snapshot(data, {})
        self.assertEqual(snapshot.holdings[0].quantity, 2_000_000_001)
        self.assertEqual(snapshot.missing_count, 1)
        with self.assertRaises(ValidationError):
            buy_stock(data, "나", "삼성전자", "005930", 100, 1)
        self.assertEqual(len(data["나"]), 2)

    def test_missing_and_stale_quotes_have_explicit_partial_totals(self):
        data = {"나": [stock(price=100, quantity=2), stock("카카오", "035720", 50, 4)]}
        snapshot = build_snapshot(data, {"005930": {"price": "110", "stale": True, "updated_at": "어제"}})
        known, missing = snapshot.holdings
        self.assertEqual(snapshot.total_cost, Decimal("400"))
        self.assertEqual(snapshot.known_cost, Decimal("200"))
        self.assertEqual(snapshot.market_value, Decimal("220"))
        self.assertEqual(snapshot.profit, Decimal("20"))
        self.assertEqual(snapshot.roi, Decimal("10"))
        self.assertEqual((snapshot.missing_count, snapshot.stale_count), (1, 1))
        self.assertTrue(known.stale)
        self.assertEqual(known.updated_at, "어제")
        self.assertIsNone(missing.current_price)
        self.assertIsNone(missing.market_value)
        self.assertIsNone(missing.profit)
        self.assertIsNone(missing.roi)

    def test_invalid_and_absent_quotes_never_use_purchase_price(self):
        data = {"나": [stock()]}
        for price in (None, 0, -1, True, "NaN", "Infinity", "not a number"):
            with self.subTest(price=price):
                snapshot = build_snapshot(data, {"005930": {"price": price}})
                self.assertEqual(snapshot.missing_count, 1)
                self.assertEqual(snapshot.known_cost, Decimal("0"))
                self.assertEqual(snapshot.market_value, Decimal("0"))
                self.assertIsNone(snapshot.roi)
                self.assertIsNone(snapshot.holdings[0].current_price)

    def test_exact_cost_remains_authoritative_under_small_decimal_context(self):
        data = {"나": [stock(price="0.3333333333333333", quantity=3, cost_basis="1")]}
        with localcontext() as context:
            context.prec = 5
            updated = buy_stock(data, "나", "삼성전자", "005930", "1234567890.123456789", 9)
            snapshot = build_snapshot(updated, {})
        self.assertEqual(snapshot.total_cost, Decimal("11111111012.111111101"))

    def test_validation_rejects_bad_user_input_without_mutating_data(self):
        data = {"나": []}
        invalid = [
            ("", "005930", 1, 1), ("삼성", "5930", 1, 1),
            ("삼성", "００５９３０", 1, 1), ("삼성", "005930", 0, 1),
            ("삼성", "005930", "NaN", 1), ("삼성", "005930", 1, 1.5),
            ("삼성", "005930", 1, True), ("삼성", "005930", True, 1),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValidationError):
                buy_stock(data, "나", *args)
        self.assertEqual(data, {"나": []})
        with self.assertRaises(ValidationError):
            buy_stock(data, "없는 가족", "삼성", "005930", 1, 1)
        self.assertEqual(validate_stock_input(" 삼성 ", " 005930 ", "1.25", 2), ("삼성", "005930", Decimal("1.25"), 2))

    def test_numeric_bounds_reject_finite_extremes_before_arithmetic_or_formatting(self):
        for price in ("1e999999999", "1e-999999999", "1" * 161, "1e12" + "0", "0." + "0" * 80 + "1"):
            with self.subTest(price=price), self.assertRaises(ValidationError):
                buy_stock({"나": []}, "나", "삼성전자", "005930", price, 1)
        with self.assertRaises(ValidationError):
            validate_stock_input("삼성전자", "005930", 1, 2_000_000_001)
        with self.assertRaises(ValidationError):
            build_snapshot({"나": [stock(cost_basis="1e999999999")]}, {})
        self.assertEqual(validate_stock_input("삼성전자", "005930", "1e12", 2_000_000_000)[2], Decimal("1e12"))

    def test_empty_family_snapshot(self):
        snapshot = build_snapshot({"나": []}, {})
        self.assertEqual(snapshot.holdings, [])
        self.assertEqual(snapshot.total_cost, Decimal("0"))
        self.assertIsNone(snapshot.roi)

    def test_csv_has_korean_headers_bom_blanks_and_safe_text(self):
        data = {"=가족": [stock(name="+위험", price=100), stock("카카오", "035720", 50, 4)]}
        snapshot = build_snapshot(data, {"005930": {"price": 90, "stale": True, "updated_at": "@위험"}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.csv"
            export_csv(snapshot, path)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        self.assertEqual(rows[0]["가족"], "'=가족")
        self.assertEqual(rows[0]["종목명"], "'+위험")
        self.assertEqual(rows[0]["종목코드"], "005930")
        self.assertEqual(rows[0]["시세확인시각"], "'@위험")
        self.assertEqual(rows[0]["시세상태"], "이전 시세")
        self.assertEqual(rows[0]["평가손익(원)"], "-10")
        self.assertEqual(rows[1]["현재가(원)"], "")
        self.assertEqual(rows[1]["평가손익(원)"], "")
        self.assertEqual(rows[1]["수익률(%)"], "")
        self.assertEqual(rows[1]["시세상태"], "시세 없음")


if __name__ == "__main__":
    unittest.main()
