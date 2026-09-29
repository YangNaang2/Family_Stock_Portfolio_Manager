from datetime import date, timedelta
from decimal import Decimal
import tempfile
from pathlib import Path
import unittest

from analytics import (add_snapshot, allocation_rows, check_alerts, get_targets,
                       remove_alert, remove_member, rename_member, set_alert, set_targets)
from data_manager import DataError, PortfolioData, load_data, save_data, validate_metadata
from portfolio import ValidationError


def holding(code="005930", price="100", quantity=2):
    return {"name": f"종목 {code}", "code": code, "purchase_price": price, "quantity": quantity}


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.data = {"나": [holding()], "엄마": [holding(quantity=1), holding("000660", "300", 1)]}
        self.quotes = {"005930": {"price": "100"}, "000660": {"price": "300"}}

    def test_targets_scope_unheld_codes_and_independent_copy(self):
        updated = set_targets(self.data, None, {"005930": "50", "035420": "25.50"})
        member_targets = set_targets(updated, "나", {"005930": "100"})
        self.assertEqual(get_targets(updated), {"005930": "50", "035420": "25.50"})
        self.assertEqual(get_targets(member_targets, "나"), {"005930": "100"})
        self.assertEqual(get_targets(updated, "나"), {})
        copied = get_targets(updated)
        copied["005930"] = "20"
        self.assertEqual(get_targets(updated)["005930"], "50")
        self.assertEqual(get_targets(set_targets(updated, None, {})), {})
        self.assertEqual(len(self.data["나"]), 1)

    def test_targets_reject_invalid_and_exact_overflow(self):
        for targets in ({"005930": "101"}, {"005930": "0"}, {"005930": "-1"},
                        {"005930": "NaN"}, {"005930": True}, {"5930": "20"},
                        {"005930": "80", "000660": "20.0000000000000000000000000001"}):
            with self.subTest(targets=targets), self.assertRaises(ValidationError):
                set_targets(self.data, None, targets)
        with self.assertRaises(ValidationError):
            set_targets(self.data, "없음", {})

    def test_allocation_aggregates_family_and_shows_unheld_target(self):
        data = set_targets(self.data, None, {"005930": "40", "035420": "20"})
        rows = {row["code"]: row for row in allocation_rows(data, self.quotes)}
        self.assertEqual(rows["005930"]["current_value"], Decimal(300))
        self.assertEqual(rows["005930"]["current_percent"], Decimal(50))
        self.assertEqual(rows["005930"]["drift"], Decimal(10))
        self.assertEqual(rows["035420"]["current_percent"], Decimal(0))
        self.assertEqual(rows["035420"]["drift"], Decimal(-20))
        member = allocation_rows(data, self.quotes, "나")
        self.assertEqual(member[0]["current_percent"], Decimal(100))

    def test_partial_or_stale_quotes_do_not_produce_misleading_weights(self):
        for quotes in ({"005930": {"price": "100"}},
                       {"005930": {"price": "100", "stale": True}, "000660": {"price": "300"}}):
            rows = allocation_rows(self.data, quotes)
            self.assertTrue(all(row["current_percent"] is None and row["drift"] is None for row in rows))

    def test_snapshot_complete_daily_replace_and_member_scope(self):
        first = add_snapshot(self.data, self.quotes, date="2024-01-02")
        original = first.metadata["snapshots"][0]
        self.assertEqual(original["market_value"], "600")
        self.assertEqual(original["total_cost"], "600")
        self.assertEqual(original["profit"], "0")
        self.assertEqual(original["holding_count"], 3)
        self.assertEqual(original["priced_count"], 3)
        second = add_snapshot(first, {"005930": {"price": "120"}, "000660": {"price": "300"}}, date="2024-01-02")
        self.assertEqual(len(second.metadata["snapshots"]), 1)
        self.assertEqual(second.metadata["snapshots"][0]["id"], original["id"])
        self.assertEqual(second.metadata["snapshots"][0]["profit"], "60")
        self.assertEqual(original["profit"], "0")
        third = add_snapshot(second, self.quotes, "나", "2024-01-02")
        fourth = add_snapshot(third, self.quotes, date="2024-01-03")
        self.assertEqual(len(fourth.metadata["snapshots"]), 3)
        validate_metadata(fourth.metadata, fourth)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "family.json"
            save_data(fourth, path)
            self.assertEqual(load_data(path).metadata, fourth.metadata)

    def test_snapshot_rejects_incomplete_stale_and_invalid_date(self):
        for quotes in ({}, {"005930": {"price": "100"}},
                       {"005930": {"price": "100", "stale": True}, "000660": {"price": "300"}}):
            with self.subTest(quotes=quotes), self.assertRaises(ValidationError):
                add_snapshot(self.data, quotes)
        for day in ("20240101", "2024-02-30", (date.today() + timedelta(days=1)).isoformat(), 123):
            with self.subTest(day=day), self.assertRaises(ValidationError):
                add_snapshot(self.data, self.quotes, date=day)

    def test_history_schema_rejects_corrupt_coverage_duplicate_and_unknown_fields(self):
        for changes in ({"missing_count": 1}, {"stale_count": 1}, {"priced_count": 0},
                        {"holding_count": True}, {"market_value": "NaN"}, {"profit": 12}, {"extra": "bad"}):
            data = add_snapshot(self.data, self.quotes)
            data.metadata["snapshots"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DataError):
                validate_metadata(data.metadata, data)
        data = add_snapshot(self.data, self.quotes)
        data.metadata["snapshots"].append(dict(data.metadata["snapshots"][0], id="other"))
        with self.assertRaises(DataError):
            validate_metadata(data.metadata, data)

    def test_alerts_match_inclusive_threshold_and_preserve_id_on_edit(self):
        data = set_alert(self.data, "005930", "above", "100")
        first_id = data.metadata["alerts"][0]["id"]
        self.assertEqual(check_alerts(data, self.quotes)[0]["id"], first_id)
        updated = set_alert(data, "005930", "above", "110")
        self.assertEqual(updated.metadata["alerts"][0]["id"], first_id)
        self.assertEqual(len(updated.metadata["alerts"]), 1)
        self.assertEqual(check_alerts(updated, self.quotes), [])
        both = set_alert(updated, "005930", "below", "100")
        self.assertEqual(check_alerts(both, self.quotes)[0]["direction"], "below")
        self.assertEqual(check_alerts(data, {"005930": {"price": "200", "stale": True}}), [])
        self.assertEqual(check_alerts(data, {"005930": {"price": "NaN"}}), [])
        self.assertEqual(check_alerts(data, {}), [])
        self.assertEqual(remove_alert(data, first_id).metadata["alerts"], [])
        self.assertEqual(len(data.metadata["alerts"]), 1)

    def test_alerts_validate_conditions_prices_and_deletions(self):
        for code, direction, price in (("5930", "above", "10"), ("005930", "up", "10"),
                                       ("005930", "below", 0), ("005930", "below", True),
                                       ("005930", "below", "1e999999999")):
            with self.subTest(code=code, direction=direction, price=price), self.assertRaises(ValidationError):
                set_alert(self.data, code, direction, price)
        with self.assertRaises(ValidationError):
            remove_alert(self.data, "missing")

    def test_rename_preserves_holdings_target_history_and_transaction_identity(self):
        data = set_targets(self.data, "나", {"005930": "50"})
        data = add_snapshot(data, self.quotes, "나")
        data.metadata["ledger_initialized"] = True
        data.metadata["transactions"] = [{
            "id": "trade-1", "type": "OPENING", "date": "2024-01-01", "recorded_at": "2024-01-01T00:00:00Z",
            "member": "나", "code": "005930", "name": "삼성전자", "quantity": "2", "price": "100",
            "amount": "200", "fee": "0", "tax": "0", "cost_basis": "200", "realized_profit": "0", "note": ""}]
        renamed = rename_member(data, "나", "딸")
        self.assertNotIn("나", renamed)
        self.assertEqual(renamed["딸"], data["나"])
        self.assertEqual(get_targets(renamed, "딸"), {"005930": "50"})
        self.assertEqual(renamed.metadata["snapshots"][0]["member"], "딸")
        self.assertEqual(renamed.metadata["transactions"][0]["member"], "딸")
        self.assertEqual(renamed.metadata["transactions"][0]["id"], "trade-1")
        self.assertEqual(data.metadata["transactions"][0]["member"], "나")
        for new in ("엄마", "*", " "):
            with self.subTest(new=new), self.assertRaises(ValidationError):
                rename_member(data, "나", new)
        with self.assertRaises(ValidationError):
            remove_member(renamed, "딸")
        renamed["딸"] = []
        with self.assertRaisesRegex(ValidationError, "거래 기록"):
            remove_member(renamed, "딸")

    def test_remove_empty_member_clears_its_empty_history_and_targets(self):
        data = PortfolioData({"아빠": [], "엄마": []})
        data = set_targets(data, "아빠", {"005930": "100"})
        data = add_snapshot(data, {}, "아빠")
        removed = remove_member(data, "아빠")
        self.assertEqual(removed, {"엄마": []})
        self.assertEqual(removed.metadata["targets"], {})
        self.assertEqual(removed.metadata["snapshots"], [])
        self.assertIn("아빠", data)


if __name__ == "__main__":
    unittest.main()
