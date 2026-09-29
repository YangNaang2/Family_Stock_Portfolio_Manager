import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import data_manager
from data_manager import (DataError, PortfolioData, ensure_portfolio_data, load_data,
                          save_data, validate_data, validate_metadata)


def holding(**changes):
    stock = {"name": "삼성전자", "code": "005930", "purchase_price": 72000, "quantity": 3}
    stock.update(changes)
    return stock


class DataManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "holdings.json"
        self.backup = Path(str(self.path) + ".bak")

    def write_json(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_only_missing_file_gets_independent_defaults(self):
        first = load_data(self.path)
        first["아빠"].append(holding())
        self.assertEqual(load_data(self.path), {"아빠": [], "엄마": [], "나": []})
        self.assertFalse(self.path.exists())
        self.write_json({})
        self.assertEqual(load_data(self.path), {})

    def test_legacy_rows_load_without_migration(self):
        legacy = {"아빠": [], "동생": [holding(), holding(purchase_price=100.25)]}
        self.write_json(legacy)
        original = self.path.read_bytes()
        loaded = load_data(self.path)
        self.assertIsInstance(loaded, dict)
        self.assertEqual(loaded, legacy)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn("cost_basis", loaded["동생"][0])

    def test_roundtrip_preserves_decimal_and_additional_fields(self):
        data = {"나": [holding(purchase_price="100.33333333333333333333", quantity=3,
                              cost_basis="301.00000000000000000000",
                              notes={"tags": ["장기", None, True], "target": 125.5})]}
        save_data(data, self.path)
        self.assertEqual(load_data(self.path), data)
        self.assertFalse(self.backup.exists())

    def test_default_path_is_module_relative_and_can_be_overridden(self):
        default = Path(data_manager.DATA_FILE)
        self.assertTrue(default.is_absolute())
        self.assertEqual(default.parent, Path(data_manager.__file__).resolve().parent)
        with mock.patch.object(data_manager, "DATA_FILE", self.path):
            with mock.patch.dict(os.environ, {"FAMILY_STOCK_DATA_FILE": ""}):
                save_data({"나": []})
                self.assertEqual(load_data(), {"나": []})
            alternate = self.path.parent / "alternate.json"
            with mock.patch.dict(os.environ, {"FAMILY_STOCK_DATA_FILE": str(alternate)}):
                save_data({"엄마": []})
                self.assertEqual(load_data(), {"엄마": []})
                self.assertEqual(load_data(self.path), {"나": []})

    def test_rejects_invalid_fields_without_creating_file(self):
        invalid = [
            [], {"": []}, {"  ": []}, {"나": {}}, {"나": [None]},
            {"나": [holding(name=" ")]}, {"나": [holding(code="00593")]},
            {"나": [holding(code="００５９３０")]}, {"나": [holding(code=5930)]},
            {"나": [holding(code="005930\n")]},
        ]
        for price in (0, -1, True, None, float("nan"), float("inf"), "NaN", "Infinity", "1,000", ""):
            invalid.append({"나": [holding(purchase_price=price)]})
        for quantity in (0, -1, True, "3", 1.5):
            invalid.append({"나": [holding(quantity=quantity)]})
        for cost in (0, 3, True, "0", "-1", "NaN", "Infinity"):
            invalid.append({"나": [holding(cost_basis=cost)]})
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(DataError):
                save_data(data, self.path)
        self.assertFalse(self.path.exists())

    def test_additional_fields_must_be_safe_json(self):
        circular = []
        circular.append(circular)
        for extra in (float("nan"), {1: "not a string key"}, (1, 2), object(), circular):
            with self.subTest(extra=type(extra)), self.assertRaises(DataError):
                validate_data({"나": [holding(extra=extra)]})

    def test_numeric_limits_accept_boundaries_and_reject_resource_exhaustion(self):
        accepted = {"나": [holding(purchase_price="1000000000000", quantity=2_000_000_000,
                                  cost_basis="1000000000000000000000000"),
                           holding(purchase_price="1e-80")]}
        save_data(accepted, self.path)
        self.assertEqual(load_data(self.path), accepted)
        original_bytes = self.path.read_bytes()
        rejected = [
            holding(purchase_price="1000000000001"),
            holding(purchase_price="1e999999999"),
            holding(purchase_price="1e-999999999"),
            holding(purchase_price="1e-81"),
            holding(purchase_price="0." + "1" * 121),
            holding(purchase_price="0" * 160 + "1"),
            holding(purchase_price=10 ** 5000),
            holding(quantity=2_000_000_001),
            holding(cost_basis="1000000000000000000000001"),
            holding(cost_basis="1e-81"),
        ]
        for index, stock in enumerate(rejected):
            with self.subTest(case=index), self.assertRaises(DataError):
                save_data({"나": [stock]}, self.path)
        self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_corrupt_files_do_not_become_empty_defaults_or_get_overwritten(self):
        for raw in (b"", b"{broken", b"\xff", b'{"x": [], "x": []}', b'{"x": NaN}',
                    b'{"x": [{"name":"a","code":"005930","purchase_price":1,"quantity":0}]}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(DataError):
                    load_data(self.path)
                with self.assertRaises(DataError):
                    save_data({"나": []}, self.path)
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertFalse(self.backup.exists())

    def test_backup_is_previous_valid_file_including_original_formatting(self):
        self.write_json({"나": [holding()]})
        original = self.path.read_bytes()
        data = load_data(self.path)
        data["나"][0]["quantity"] = 4
        save_data(data, self.path)
        self.assertEqual(self.backup.read_bytes(), original)
        second = self.path.read_bytes()
        data["나"][0]["quantity"] = 5
        save_data(data, self.path)
        self.assertEqual(self.backup.read_bytes(), second)
        self.assertEqual(load_data(self.path)["나"][0]["quantity"], 5)
        self.assertEqual(set(json.loads(self.path.read_text(encoding="utf-8"))), {"나"})

    def test_failed_atomic_replace_keeps_original_and_removes_temporary_files(self):
        self.write_json({"나": [holding()]})
        original = self.path.read_bytes()
        data = load_data(self.path)
        data["나"][0]["quantity"] = 4
        real_replace = os.replace

        def fail_final_replace(source, destination):
            if Path(destination) == self.path:
                raise PermissionError("simulated failure")
            return real_replace(source, destination)

        with mock.patch.object(data_manager.os, "replace", side_effect=fail_final_replace):
            with self.assertRaises(DataError):
                save_data(data, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        save_data(data, self.path)  # The failed save must not advance its revision.
        self.assertEqual(load_data(self.path)["나"][0]["quantity"], 4)

    def test_backup_replace_failure_prevents_main_file_update(self):
        self.write_json({"나": [holding()]})
        original = self.path.read_bytes()
        with mock.patch.object(data_manager.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(DataError):
                save_data({"나": []}, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_deepcopy_detects_external_edits_and_deletions(self):
        self.write_json({"나": [holding()]})
        stale = copy.deepcopy(load_data(self.path))
        self.write_json({"나": [holding(quantity=10)]})
        external = self.path.read_bytes()
        with self.assertRaisesRegex(DataError, "다른 창"):
            save_data(stale, self.path)
        self.assertEqual(self.path.read_bytes(), external)
        self.assertFalse(self.backup.exists())
        latest = load_data(self.path)
        self.path.unlink()
        with self.assertRaisesRegex(DataError, "다른 창"):
            save_data(latest, self.path)
        self.assertFalse(self.path.exists())

    def test_separate_loaded_copies_cannot_overwrite_each_other(self):
        self.write_json({"나": [holding()]})
        first = load_data(self.path)
        second = load_data(self.path)
        first["나"][0]["quantity"] = 10
        save_data(first, self.path)
        second["나"][0]["quantity"] = 20
        with self.assertRaisesRegex(DataError, "다른 창"):
            save_data(second, self.path)
        self.assertEqual(load_data(self.path)["나"][0]["quantity"], 10)

    def test_data_created_since_first_load_is_not_overwritten(self):
        stale = load_data(self.path)
        self.write_json({"동생": []})
        with self.assertRaisesRegex(DataError, "다른 창"):
            save_data(stale, self.path)
        self.assertEqual(load_data(self.path), {"동생": []})

    def test_external_change_during_staging_is_detected(self):
        self.write_json({"나": [holding()]})
        data = load_data(self.path)
        real_stage = data_manager._stage_file

        def stage_and_change(target, content):
            result = real_stage(target, content)
            self.write_json({"나": [holding(quantity=20)]})
            return result

        with mock.patch.object(data_manager, "_stage_file", side_effect=stage_and_change):
            with self.assertRaisesRegex(DataError, "다른 창"):
                save_data(data, self.path)
        self.assertEqual(load_data(self.path)["나"][0]["quantity"], 20)
        self.assertFalse(self.backup.exists())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_metadata_defaults_are_independent_and_legacy_stays_legacy(self):
        first = PortfolioData({"나": []})
        second = PortfolioData({"나": []})
        first.metadata["targets"]["나"] = {"005930": "50"}
        self.assertEqual(second.metadata["targets"], {})
        save_data(second, self.path)
        self.assertEqual(json.loads(self.path.read_text()), {"나": []})
        loaded = load_data(self.path)
        self.assertIsInstance(loaded, PortfolioData)
        self.assertEqual(loaded.metadata, second.metadata)

    def test_versioned_metadata_roundtrip_backup_and_revision(self):
        self.write_json({"나": [holding()]})
        old_bytes = self.path.read_bytes()
        data = ensure_portfolio_data(load_data(self.path))
        data.metadata["targets"]["*"] = {"005930": "80.5", "000660": "19.5"}
        data.metadata["alerts"].append({"id": "alert-1", "code": "005930", "direction": "above", "price": "75000.1"})
        save_data(data, self.path)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored["schema_version"], 2)
        self.assertEqual(stored["members"], {"나": [holding()]})
        self.assertEqual(self.backup.read_bytes(), old_bytes)
        loaded = load_data(self.path)
        self.assertEqual(loaded.metadata, data.metadata)
        self.assertEqual(loaded, data)
        stale = ensure_portfolio_data(loaded)
        loaded.metadata["alerts"][0]["price"] = "76000"
        save_data(loaded, self.path)
        with self.assertRaisesRegex(DataError, "다른 창"):
            save_data(stale, self.path)

    def test_future_version_and_invalid_metadata_never_overwrite_file(self):
        for envelope in (
            {"schema_version": 99, "members": {}, "metadata": {}},
            {"schema_version": True, "members": {}, "metadata": {}},
            {"schema_version": 2, "members": {"나": []}, "metadata": {}},
            {"schema_version": 2, "members": [], "metadata": PortfolioData().metadata},
        ):
            with self.subTest(envelope=envelope):
                self.write_json(envelope)
                original = self.path.read_bytes()
                with self.assertRaises(DataError):
                    load_data(self.path)
                with self.assertRaises(DataError):
                    save_data({"나": []}, self.path)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertFalse(self.backup.exists())

    def test_legacy_member_named_schema_version_still_roundtrips(self):
        data = {"schema_version": [holding()], "members": [], "metadata": []}
        save_data(data, self.path)
        self.assertEqual(load_data(self.path), data)

    def test_metadata_shape_targets_and_alerts_are_strict(self):
        invalid_updates = [
            {"extra": 1}, {"ledger_initialized": 1}, {"transactions": {}}, {"snapshots": {}},
            {"targets": []}, {"targets": {"不存在": {"005930": "20"}}},
            {"targets": {"*": {"005930": 20}}}, {"targets": {"*": {"005930": "NaN"}}},
            {"targets": {"*": {"005930": "0"}}},
            {"targets": {"*": {"005930": "70", "000660": "30.000000000000000000000000001"}}},
            {"targets": {"*": {"005930": "1e999999999"}}},
            {"targets": {"*": {"005930": "1e-999999999"}}},
            {"alerts": [{"id": "a", "code": "005930", "direction": "up", "price": "100"}]},
            {"alerts": [{"id": "a", "code": "005930", "direction": "above", "price": "0"}]},
            {"alerts": [{"id": "a", "code": "005930", "direction": "above", "price": "100", "unknown": 1}]},
        ]
        for changes in invalid_updates:
            data = PortfolioData({"나": []})
            data.metadata.update(changes)
            with self.subTest(changes=changes), self.assertRaises(DataError):
                save_data(data, self.path)
        self.assertFalse(self.path.exists())

    def test_metadata_transactions_validate_dates_numbers_and_unique_ids(self):
        entry = {"id": "t-1", "type": "BUY", "date": "2024-02-29",
                 "recorded_at": "2024-02-29T12:00:00+00:00", "member": "나", "code": "005930",
                 "name": "삼성전자", "quantity": "3", "price": "100.50", "amount": "301.50",
                 "fee": "0", "tax": "0", "cost_basis": "301.50", "realized_profit": "0", "note": ""}
        data = PortfolioData({"나": [holding()]}, {"transactions": [entry], "ledger_initialized": True})
        save_data(data, self.path)
        self.assertEqual(load_data(self.path).metadata, data.metadata)
        for changes in ({"date": "2023-02-29"}, {"date": "20240229"},
                        {"recorded_at": "2024-02-29T12:00:00"}, {"recorded_at": "2024-02-29T12:00:00+09:00"},
                        {"quantity": "1.5"}, {"quantity": 3}, {"fee": "-1"}, {"fee": "0e99999999"}, {"price": "NaN"},
                        {"type": "SELL_ORDER"}, {"member": "삭제됨"}, {"type": "ADJUSTMENT"}):
            edited = copy.deepcopy(data)
            edited.metadata["transactions"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DataError):
                validate_metadata(edited.metadata, edited)
        duplicate = copy.deepcopy(data)
        duplicate.metadata["transactions"].append(copy.deepcopy(entry))
        with self.assertRaises(DataError):
            validate_metadata(duplicate.metadata, duplicate)

    def test_ensure_portfolio_data_copies_both_holdings_and_metadata(self):
        original = PortfolioData({"나": [holding()]}, {"targets": {"*": {"005930": "50"}}})
        edited = ensure_portfolio_data(original)
        edited["나"][0]["quantity"] = 10
        edited.metadata["targets"]["*"]["005930"] = "70"
        self.assertEqual(original["나"][0]["quantity"], 3)
        self.assertEqual(original.metadata["targets"]["*"]["005930"], "50")

    def test_backup_copy_and_import_keep_active_file_revision(self):
        original = PortfolioData({"나": [holding()]}, {"targets": {"나": {"005930": "80"}}})
        save_data(original, self.path)
        active = load_data(self.path)
        active_revision = active._revision
        export_path = self.path.parent / "export.json"
        save_data(ensure_portfolio_data(active), export_path)
        self.assertEqual(active._source_path, self.path)
        self.assertEqual(active._revision, active_revision)
        self.assertEqual(load_data(export_path).metadata, active.metadata)
        replacement = PortfolioData({"엄마": [holding(quantity=10)]}, {"targets": {"*": {"000660": "40"}}})
        candidate = ensure_portfolio_data(active)
        candidate.clear()
        candidate.update(copy.deepcopy(replacement))
        candidate.metadata = copy.deepcopy(replacement.metadata)
        self.assertEqual(candidate._revision, active_revision)
        save_data(candidate, self.path)
        self.assertEqual(load_data(self.path), replacement)
        self.assertEqual(load_data(self.path).metadata, replacement.metadata)
        self.assertEqual(load_data(self.backup).metadata, original.metadata)


if __name__ == "__main__":
    unittest.main()
