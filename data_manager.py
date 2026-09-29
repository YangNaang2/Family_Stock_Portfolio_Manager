"""Validate and safely persist legacy holdings and versioned portfolio records."""

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


DATA_FILE = str(Path(__file__).resolve().with_name("family_stocks.json"))
MAX_PRICE = Decimal("1e12")
MAX_COST = Decimal("1e24")
MAX_QUANTITY = 2_000_000_000
_DECIMAL_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


class DataError(ValueError):
    """A data or storage error that can be displayed directly to the user."""


def _empty_metadata():
    return {"transactions": [], "ledger_initialized": False, "targets": {},
            "snapshots": [], "alerts": []}


class PortfolioData(dict):
    """Member holdings with separate metadata and optimistic file revision."""

    def __init__(self, data=None, metadata=None, *, path=None, revision=None):
        super().__init__({} if data is None else data)
        self.metadata = _empty_metadata()
        if metadata is not None:
            self.metadata.update(deepcopy(metadata))
        self._source_path = path
        self._revision = revision


_LoadedData = PortfolioData  # Kept for callers of the previous persistence API.


def ensure_portfolio_data(data):
    """Return an independent editable copy, retaining loaded-file revisions."""
    validate_data(data)
    if isinstance(data, PortfolioData):
        validate_metadata(data.metadata, data)
        return deepcopy(data)
    return PortfolioData(deepcopy(data))


def _data_path(path):
    selected = path if path is not None else os.environ.get("FAMILY_STOCK_DATA_FILE") or DATA_FILE
    try:
        return Path(selected).expanduser().resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise DataError("데이터 파일 경로를 확인해주세요.") from exc


def _positive_decimal(value, label, *, string_only=False, maximum=MAX_PRICE):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DataError(f"{label}: 0보다 큰 숫자를 입력해주세요.")
    if string_only and not isinstance(value, str):
        raise DataError(f"{label}: 정확한 금액을 소수 문자열로 저장해주세요. 예: \"1000.50\"")
    try:
        text = str(value)
    except ValueError as exc:
        raise DataError(f"{label}: 숫자 길이가 너무 깁니다.") from exc
    if len(text) > 160:
        raise DataError(f"{label}: 숫자 길이가 너무 깁니다.")
    if isinstance(value, str) and not _DECIMAL_PATTERN.fullmatch(text):
        raise DataError(f"{label}: 쉼표 없이 올바른 숫자를 입력해주세요.")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise DataError(f"{label}: 올바른 숫자를 입력해주세요.") from exc
    if not number.is_finite() or number <= 0:
        raise DataError(f"{label}: 0보다 큰 유한한 숫자를 입력해주세요.")
    if number > maximum:
        raise DataError(f"{label}: {maximum:,.0f} 이하로 입력해주세요.")
    if len(number.as_tuple().digits) > 120 or number.as_tuple().exponent < -80:
        raise DataError(f"{label}: 소수 자릿수가 너무 많습니다.")


def _check_json_value(value, seen):
    """Keep additional fields, but reject values JSON would change or lose."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataError("추가 데이터에 NaN 또는 무한대가 포함되어 있습니다.")
        return
    if not isinstance(value, (dict, list)):
        raise DataError("추가 데이터는 JSON으로 저장 가능한 값이어야 합니다.")
    if id(value) in seen:
        raise DataError("데이터가 자기 자신을 참조하고 있어 저장할 수 없습니다.")
    seen.add(id(value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise DataError("데이터의 모든 항목 이름은 문자열이어야 합니다.")
        children = value.values()
    else:
        children = value
    for child in children:
        _check_json_value(child, seen)
    seen.remove(id(value))


def validate_data(data):
    """Validate without normalizing values or dropping optional fields.

    ``purchase_price`` accepts a positive JSON number or decimal string.
    ``cost_basis``, when present, is the authoritative total purchase cost and
    must be a positive decimal string. Legacy rows need no migration.
    """
    if not isinstance(data, dict):
        raise DataError("데이터는 가족 이름별 주식 목록 형식이어야 합니다.")
    for member, stocks in data.items():
        if not isinstance(member, str) or not member.strip():
            raise DataError("가족 이름을 비워둘 수 없습니다.")
        if not isinstance(stocks, list):
            raise DataError(f"[{member}] 주식 데이터는 목록이어야 합니다.")
        for index, stock in enumerate(stocks, 1):
            label = f"[{member}] {index}번째 종목"
            if not isinstance(stock, dict):
                raise DataError(f"{label}: 주식 정보 형식을 확인해주세요.")
            if not isinstance(stock.get("name"), str) or not stock["name"].strip():
                raise DataError(f"{label}: 종목명을 입력해주세요.")
            if not isinstance(stock.get("code"), str) or not re.fullmatch(r"[0-9]{6}", stock["code"]):
                raise DataError(f"{label}: 종목코드는 숫자 6자리여야 합니다. 예: 005930")
            _positive_decimal(stock.get("purchase_price"), f"{label} 매수단가")
            quantity = stock.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise DataError(f"{label}: 수량은 1 이상의 정수여야 합니다.")
            if quantity > MAX_QUANTITY:
                raise DataError(f"{label}: 수량은 {MAX_QUANTITY:,}주 이하로 입력해주세요.")
            if "cost_basis" in stock:
                _positive_decimal(stock["cost_basis"], f"{label} 총 매수금액", string_only=True,
                                  maximum=MAX_COST)
    try:
        _check_json_value(data, set())
    except RecursionError as exc:
        raise DataError("데이터의 중첩 단계가 너무 많습니다. 추가 항목을 확인해주세요.") from exc
    return data


def _metadata_text(value, label, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise DataError(f"{label}: 문자열 형식을 확인해주세요.")
    return value


def _metadata_decimal(value, label, *, signed=False, positive=False, maximum=Decimal("1e30")):
    if not isinstance(value, str) or len(value) > 160 or not _DECIMAL_PATTERN.fullmatch(value):
        raise DataError(f"{label}: 금액은 유효한 소수 문자열이어야 합니다.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise DataError(f"{label}: 숫자 형식을 확인해주세요.") from exc
    if (not number.is_finite() or number.copy_abs() > maximum or
            (not signed and number < 0) or (positive and number <= 0)):
        raise DataError(f"{label}: 숫자 범위를 확인해주세요.")
    if (len(number.as_tuple().digits) > 120 or number.as_tuple().exponent < -80 or
            number.as_tuple().exponent > 30):
        raise DataError(f"{label}: 소수 자릿수가 너무 많습니다.")
    return number


def _metadata_date(value, label):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise DataError(f"{label}: 날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _metadata_timestamp(value):
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError as exc:
        raise DataError("기록 시각은 시간대가 포함된 UTC ISO 형식이어야 합니다.") from exc


def _metadata_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise DataError("종목코드는 숫자 6자리여야 합니다.")


def _metadata_keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or set(value) - set(required) - set(optional):
        raise DataError("부가 데이터의 필수 항목 또는 지원하지 않는 항목을 확인해주세요.")


def validate_metadata(metadata, members=None):
    """Validate the version 2 metadata schema without silently dropping fields."""
    _metadata_keys(metadata, _empty_metadata())
    try:
        _check_json_value(metadata, set())
    except RecursionError as exc:
        raise DataError("부가 데이터의 중첩 단계가 너무 많습니다.") from exc
    if type(metadata["ledger_initialized"]) is not bool:
        raise DataError("거래 기록 초기화 여부는 참 또는 거짓이어야 합니다.")
    for key in ("transactions", "snapshots", "alerts"):
        if not isinstance(metadata[key], list):
            raise DataError(f"{key}: 목록 형식이어야 합니다.")
    if not isinstance(metadata["targets"], dict):
        raise DataError("목표 비중은 가족별 종목 비중 목록이어야 합니다.")

    def owner(value, *, aggregate=False):
        _metadata_text(value, "가족 이름")
        if members is not None and not (aggregate and value == "*") and value not in members:
            raise DataError(f"부가 데이터에 존재하지 않는 가족이 있습니다: {value}")

    def identity(entry, seen):
        identifier = _metadata_text(entry["id"], "기록 ID")
        if len(identifier) > 128 or identifier in seen:
            raise DataError("기록 ID가 너무 길거나 중복되었습니다.")
        seen.add(identifier)

    transaction_keys = {"id", "type", "date", "recorded_at", "member", "code", "name", "quantity",
                        "price", "amount", "fee", "tax", "cost_basis", "realized_profit", "note"}
    transaction_ids = set()
    for entry in metadata["transactions"]:
        _metadata_keys(entry, transaction_keys, {"previous_quantity", "previous_cost_basis"})
        identity(entry, transaction_ids)
        if entry["type"] not in ("OPENING", "BUY", "SELL", "DIVIDEND", "ADJUSTMENT", "REMOVE"):
            raise DataError("지원하지 않는 거래 유형입니다.")
        owner(entry["member"])
        _metadata_code(entry["code"])
        _metadata_text(entry["name"], "종목명")
        _metadata_text(entry["note"], "거래 메모", empty=True)
        _metadata_date(entry["date"], "거래일")
        _metadata_timestamp(entry["recorded_at"])
        numbers = {key: _metadata_decimal(entry[key], key, signed=key == "realized_profit")
                   for key in ("quantity", "price", "amount", "fee", "tax", "cost_basis", "realized_profit")}
        if numbers["quantity"] != numbers["quantity"].to_integral_value():
            raise DataError("거래 수량은 정수여야 합니다.")
        if entry["type"] == "DIVIDEND":
            if numbers["quantity"] != 0 or numbers["price"] != 0:
                raise DataError("배당 기록의 수량과 단가는 0이어야 합니다.")
        elif numbers["quantity"] <= 0 or numbers["price"] <= 0:
            raise DataError("거래 수량과 단가는 0보다 커야 합니다.")
        previous = {"previous_quantity", "previous_cost_basis"} & entry.keys()
        if entry["type"] in ("ADJUSTMENT", "REMOVE"):
            if previous != {"previous_quantity", "previous_cost_basis"}:
                raise DataError("수정/삭제 기록에는 이전 수량과 원금이 필요합니다.")
            quantity = _metadata_decimal(entry["previous_quantity"], "이전 수량", positive=True)
            _metadata_decimal(entry["previous_cost_basis"], "이전 원금", positive=True)
            if quantity != quantity.to_integral_value():
                raise DataError("이전 수량은 정수여야 합니다.")
        elif previous:
            raise DataError("이전 수량과 원금은 수정/삭제 기록에만 사용할 수 있습니다.")
    if metadata["transactions"] and not metadata["ledger_initialized"]:
        raise DataError("거래 기록이 있는 포트폴리오는 거래 기록 초기화 상태여야 합니다.")

    for scope, targets in metadata["targets"].items():
        owner(scope, aggregate=True)
        if not isinstance(targets, dict):
            raise DataError("목표 비중은 종목코드와 비중으로 구성해야 합니다.")
        total = Decimal(0)
        for code, value in targets.items():
            _metadata_code(code)
            percentage = _metadata_decimal(value, "목표 비중", positive=True, maximum=Decimal(100))
            with localcontext() as context:
                context.prec = 200
                total += percentage
        if total > 100:
            raise DataError("목표 비중 합계는 100%를 초과할 수 없습니다.")

    snapshot_keys = {"id", "recorded_at", "date", "member", "total_cost", "market_value", "profit",
                     "holding_count", "priced_count", "missing_count", "stale_count"}
    snapshot_ids, snapshot_dates = set(), set()
    for entry in metadata["snapshots"]:
        _metadata_keys(entry, snapshot_keys)
        identity(entry, snapshot_ids)
        owner(entry["member"], aggregate=True)
        _metadata_date(entry["date"], "평가 기록일")
        _metadata_timestamp(entry["recorded_at"])
        day = entry["member"], entry["date"]
        if day in snapshot_dates:
            raise DataError("같은 가족의 같은 날짜에 평가 기록이 중복되었습니다.")
        snapshot_dates.add(day)
        for key in ("total_cost", "market_value", "profit"):
            _metadata_decimal(entry[key], key, signed=key == "profit")
        for key in ("holding_count", "priced_count", "missing_count", "stale_count"):
            if type(entry[key]) is not int or entry[key] < 0:
                raise DataError("평가 기록의 종목 수는 0 이상의 정수여야 합니다.")
        if (entry["missing_count"] or entry["stale_count"] or
                entry["priced_count"] != entry["holding_count"]):
            raise DataError("평가 기록에는 모든 종목의 최신 시세가 필요합니다.")

    alert_ids, alert_conditions = set(), set()
    for entry in metadata["alerts"]:
        _metadata_keys(entry, {"id", "code", "direction", "price"})
        identity(entry, alert_ids)
        _metadata_code(entry["code"])
        if entry["direction"] not in ("above", "below"):
            raise DataError("가격 알림 조건은 above 또는 below여야 합니다.")
        _metadata_decimal(entry["price"], "알림 가격", positive=True, maximum=MAX_PRICE)
        condition = entry["code"], entry["direction"]
        if condition in alert_conditions:
            raise DataError("같은 종목의 같은 방향 가격 알림이 중복되었습니다.")
        alert_conditions.add(condition)
    return metadata


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError(f"데이터에 같은 항목 이름이 반복됩니다: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise DataError(f"데이터에 유효하지 않은 숫자가 있습니다: {value}")


def _decode(raw, path):
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
        if isinstance(data, dict) and "schema_version" in data and not isinstance(data["schema_version"], list):
            if type(data["schema_version"]) is not int or data["schema_version"] != 2:
                raise DataError("지원하지 않는 데이터 버전입니다. 더 최신 버전의 앱으로 열어주세요.")
            _metadata_keys(data, {"schema_version", "members", "metadata"})
            validate_data(data["members"])
            validate_metadata(data["metadata"], data["members"])
            return PortfolioData(data["members"], data["metadata"])
        return PortfolioData(validate_data(data))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DataError(
            f"데이터 파일을 읽을 수 없습니다: {path}\n{exc}\n"
            f"원본 파일을 보존했습니다. 파일을 수정하거나 백업({path}.bak)을 확인해주세요."
        ) from exc


def _read_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DataError(f"데이터 파일에 접근할 수 없습니다: {path}\n파일 경로와 읽기 권한을 확인해주세요.") from exc


def _revision(raw):
    return None if raw is None else hashlib.sha256(raw).digest()


def load_data(path=None):
    """Read validated data; only a missing file receives empty defaults."""
    target = _data_path(path)
    raw = _read_bytes(target)
    data = PortfolioData({"아빠": [], "엄마": [], "나": []}) if raw is None else _decode(raw, target)
    data._source_path = target
    data._revision = _revision(raw)
    return data


def _stage_file(target, content):
    """Write and flush a temporary file on the same filesystem as the target."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{target.name}.",
                                         suffix=".tmp", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _check_revision(target, expected):
    if _revision(_read_bytes(target)) != expected:
        raise DataError(
            f"데이터 파일이 다른 창이나 프로그램에서 변경되었습니다: {target}\n"
            "덮어쓰기를 중단했습니다. 앱을 다시 열어 최신 데이터를 확인한 뒤 변경해주세요."
        )


def save_data(data, path=None):
    """Atomically save and retain the previous valid version at ``<path>.bak``.

    Loaded dictionaries (including deep copies) reject an external change since
    loading. Revision checks reduce lost writes; they are not a cross-process
    transaction lock. Use one app instance per data file.
    """
    validate_data(data)
    stored = data
    if isinstance(data, PortfolioData):
        validate_metadata(data.metadata, data)
        if data.metadata != _empty_metadata():
            stored = {"schema_version": 2, "members": data, "metadata": data.metadata}
    target = _data_path(path)
    try:
        serialized = (json.dumps(stored, ensure_ascii=False, indent=4, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("저장할 데이터 형식을 확인해주세요. 기존 파일은 보존했습니다.") from exc

    old_raw = _read_bytes(target)
    previous_revision = _revision(old_raw)
    if isinstance(data, _LoadedData) and data._source_path == target:
        if data._revision != previous_revision:
            raise DataError(
                f"데이터 파일이 다른 창이나 프로그램에서 변경되었습니다: {target}\n"
                "덮어쓰기를 중단했습니다. 앱을 다시 열어 최신 데이터를 확인한 뒤 변경해주세요."
            )
    if old_raw is not None:
        _decode(old_raw, target)  # Never replace an unreadable existing file.

    staged = backup_staged = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = _stage_file(target, serialized)
        if old_raw is not None:
            backup = Path(str(target) + ".bak")
            backup_staged = _stage_file(backup, old_raw)
            _check_revision(target, previous_revision)
            os.replace(backup_staged, backup)
            backup_staged = None
        _check_revision(target, previous_revision)
        os.replace(staged, target)
        staged = None
    except OSError as exc:
        raise DataError(
            f"데이터를 저장하지 못했습니다: {target}\n"
            "기존 데이터는 보존했습니다. 저장 공간과 폴더 쓰기 권한을 확인해주세요."
        ) from exc
    finally:
        for temporary in (staged, backup_staged):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    if isinstance(data, _LoadedData):
        data._source_path = target
        data._revision = _revision(serialized)
