from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
import io
import unittest
from unittest.mock import patch

from data_manager import DataError
import first


def data():
    return {"나": [{"name": "삼성전자", "code": "005930", "purchase_price": "0.3333333333333333333333", "quantity": 3, "cost_basis": "1"}]}


class CliTests(unittest.TestCase):
    def test_add_preserves_exact_cost_and_original_until_save(self):
        original = data()
        with patch("builtins.input", side_effect=["삼성전자", "005930", "0.01", "2"]), patch("first.save_data") as save, redirect_stdout(io.StringIO()):
            updated = first.add_stock("나", original)
        self.assertEqual(updated["나"][0]["cost_basis"], "1.02")
        self.assertEqual(updated["나"][0]["quantity"], 5)
        self.assertEqual(original, data())
        save.assert_called_once_with(updated)

    def test_failed_save_and_invalid_input_leave_original_in_memory(self):
        original = data()
        with patch("builtins.input", side_effect=["삼성전자", "005930", "0.01", "2"]), patch("first.save_data", side_effect=DataError("disk full")), redirect_stdout(io.StringIO()) as output:
            self.assertIs(first.add_stock("나", original), original)
        self.assertIn("변경 사항을 적용하지 않았습니다", output.getvalue())
        self.assertEqual(original, data())
        with patch("builtins.input", side_effect=["삼성전자", "005930", "-1", "2"]), patch("first.save_data") as save, redirect_stdout(io.StringIO()):
            self.assertIs(first.add_stock("나", original), original)
        save.assert_not_called()

    def test_delete_requires_confirmation_and_returns_saved_candidate(self):
        original = data()
        original["나"].append(deepcopy(original["나"][0]))
        before = deepcopy(original)
        with patch("builtins.input", side_effect=["1", "n"]), patch("first.save_data") as save, redirect_stdout(io.StringIO()):
            self.assertIs(first.delete_stock("나", original), original)
        save.assert_not_called()
        with patch("builtins.input", side_effect=["1", "y"]), patch("first.save_data") as save, redirect_stdout(io.StringIO()) as output:
            updated = first.delete_stock("나", original)
        self.assertEqual(updated, {"나": []})
        self.assertIn("6주", output.getvalue())
        self.assertNotIn("2.", output.getvalue())
        self.assertEqual(original, before)
        save.assert_called_once_with(updated)

    def test_missing_quotes_remain_unknown_and_duplicates_are_fetched_once(self):
        portfolio = data()
        portfolio["나"].append(deepcopy(portfolio["나"][0]))
        with patch("first.get_current_price", return_value=None) as fetch, redirect_stdout(io.StringIO()) as output:
            first.display_portfolio("나", portfolio)
        fetch.assert_called_once_with("005930")
        self.assertIn("총 매수금액: 2.00원", output.getvalue())
        self.assertIn("시세 미확인 1건 제외", output.getvalue())
        self.assertIn("평가금액: 미확인 | 평가손익: 미확인 | 수익률: 미확인", output.getvalue())

    def test_display_uses_exact_cost_basis_for_decimal_string_data(self):
        with patch("first.get_current_price", return_value={"price": 1, "rate": "+1%"}), redirect_stdout(io.StringIO()) as output:
            first.display_portfolio("나", data())
        self.assertIn("총 매수금액: 1.00원", output.getvalue())
        self.assertIn("평가금액: 3.00원 | 평가손익: 2.00원 | 수익률: 200.00%", output.getvalue())

    def test_main_and_member_menu_keep_updated_data_between_visits(self):
        original = data()
        # Add on the first visit, then view on the second visit.
        inputs = ["1", "2", "삼성전자", "005930", "0.01", "2", "0", "1", "1", "0", "0"]
        with patch("builtins.input", side_effect=inputs), patch("first.load_data", return_value=original), patch("first.save_data"), patch("first.display_portfolio") as display, redirect_stdout(io.StringIO()):
            self.assertEqual(first.main(), 0)
        shown = display.call_args.args[1]
        self.assertEqual(shown["나"][0]["quantity"], 5)
        self.assertEqual(Decimal(shown["나"][0]["cost_basis"]), Decimal("1.02"))
        self.assertEqual(original, data())

    def test_corrupt_data_does_not_open_menu_or_save(self):
        with patch("first.load_data", side_effect=DataError("bad JSON")), patch("first.save_data") as save, patch("builtins.input") as read, redirect_stdout(io.StringIO()):
            self.assertEqual(first.main(), 1)
        read.assert_not_called()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
