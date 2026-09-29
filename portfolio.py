"""Portfolio calculations and edits, independent of Qt, storage and the network.

``cost_basis`` is the source of truth for invested money. An average price can
have recurring decimal digits, so multiplying a displayed average back by the
quantity would gradually lose money during repeated purchases.
"""

from copy import deepcopy
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re


MAX_PRICE = Decimal("1e12")
MAX_COST = Decimal("1e24")
MAX_QUANTITY = 2_000_000_000


class ValidationError(ValueError):
    """A portfolio operation contains an invalid value."""


def _positive_decimal(value, label, *, maximum=MAX_PRICE):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError(f"{label}은(는) 0보다 큰 숫자여야 합니다.")
    try:
        text = str(value).strip()
        if len(text) > 160:
            raise ValidationError(f"{label}의 숫자 길이가 너무 깁니다.")
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{label}은(는) 0보다 큰 숫자여야 합니다.") from None
    if not result.is_finite() or result <= 0:
        raise ValidationError(f"{label}은(는) 0보다 큰 유한한 숫자여야 합니다.")
    if result > maximum:
        raise ValidationError(f"{label}은(는) {maximum:,.0f} 이하로 입력해 주세요.")
    if len(result.as_tuple().digits) > 120 or result.as_tuple().exponent < -80:
        raise ValidationError(f"{label}의 소수 자릿수가 너무 많습니다.")
    return result


def _quantity(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError("수량은 1 이상의 정수여야 합니다.")
    if value > MAX_QUANTITY:
        raise ValidationError(f"수량은 {MAX_QUANTITY:,}주 이하로 입력해 주세요.")
    return value


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value.strip()):
        raise ValidationError("종목코드는 6자리 숫자로 입력해 주세요.")
    return value.strip()


def validate_stock_input(name, code, price, quantity):
    """Return normalized values or raise a user-readable ValidationError."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("종목명을 입력해 주세요.")
    return name.strip(), _code(code), _positive_decimal(price, "매수 단가"), _quantity(quantity)


def _add(left, right):
    """Add finite decimals without silently rounding the stored principal."""
    with localcontext() as context:
        context.prec = max(
            28,
            max(left.adjusted(), right.adjusted())
            - min(left.as_tuple().exponent, right.as_tuple().exponent) + 2,
        )
        return left + right


def _multiply(left, right):
    right = Decimal(right)
    with localcontext() as context:
        context.prec = max(28, len(left.as_tuple().digits) + len(right.as_tuple().digits))
        return left * right


def _divide(left, right):
    with localcontext() as context:
        context.prec = 40
        return left / Decimal(right)


def _decimal_text(value):
    return format(value, "f")


def _stock_values(stock):
    if not isinstance(stock, dict):
        raise ValidationError("종목 데이터 형식이 올바르지 않습니다.")
    try:
        name, code, price, quantity = validate_stock_input(
            stock["name"], stock["code"], stock["purchase_price"], stock["quantity"]
        )
    except KeyError as error:
        raise ValidationError(f"종목 데이터에 {error.args[0]} 항목이 없습니다.") from None
    cost = (
        _positive_decimal(stock["cost_basis"], "매수 원금", maximum=MAX_COST)
        if "cost_basis" in stock
        else _multiply(price, quantity)
    )
    # Legacy numeric averages still work. For upgraded records the exact
    # principal wins over a rounded or recurring average.
    return name, code, _divide(cost, quantity), quantity, cost


def _group_stock_values(stocks):
    """Present legacy duplicate rows as one complete member/code position."""
    grouped = {}
    for stock in stocks:
        name, code, _, quantity, cost = _stock_values(stock)
        if code in grouped:
            name, old_quantity, old_cost = grouped[code]
            quantity += old_quantity
            cost = _add(cost, old_cost)
        grouped[code] = name, quantity, cost
    for code, (name, quantity, cost) in grouped.items():
        yield name, code, quantity, cost


def _members(data, member=None):
    if not isinstance(data, dict):
        raise ValidationError("가족 데이터는 이름과 보유 종목 목록으로 구성해야 합니다.")
    if member is not None and member not in data:
        raise ValidationError("선택한 가족 구성원이 없습니다.")
    selected = data.items() if member is None else [(member, data[member])]
    for owner, stocks in selected:
        if not isinstance(owner, str) or not owner.strip() or not isinstance(stocks, list):
            raise ValidationError("가족 이름 또는 보유 종목 목록이 올바르지 않습니다.")
        yield owner, stocks


def buy_stock(data, member, name, code, price, quantity):
    """Return an edited deep copy, merging all same-code positions for a member."""
    name, code, price, quantity = validate_stock_input(name, code, price, quantity)
    _, stocks = next(_members(data, member))
    cost = _multiply(price, quantity)
    total_quantity = quantity
    matching = []
    for index, stock in enumerate(stocks):
        old_name, old_code, _, old_quantity, old_cost = _stock_values(stock)
        if old_code == code:
            matching.append(index)
            total_quantity += old_quantity
            cost = _add(cost, old_cost)
            if len(matching) == 1:
                name = old_name
    _quantity(total_quantity)
    _positive_decimal(cost, "매수 원금", maximum=MAX_COST)
    updated = deepcopy(data)
    position = deepcopy(stocks[matching[0]]) if matching else {}
    position.update(
        name=name, code=code, quantity=total_quantity,
        purchase_price=_decimal_text(_divide(cost, total_quantity)),
        cost_basis=_decimal_text(cost),
    )
    if matching:
        first = matching[0]
        updated[member] = [
            position if index == first else stock
            for index, stock in enumerate(updated[member])
            if index == first or index not in matching
        ]
    else:
        updated[member].append(position)
    return updated


def update_stock(data, member, code, price, quantity):
    """Correct a position's average and quantity, deliberately resetting its cost."""
    code = _code(code)
    price = _positive_decimal(price, "매수 단가")
    quantity = _quantity(quantity)
    _, stocks = next(_members(data, member))
    matches = [index for index, stock in enumerate(stocks) if _stock_values(stock)[1] == code]
    if not matches:
        raise ValidationError("수정할 종목이 없습니다.")
    updated = deepcopy(data)
    first = matches[0]
    updated[member][first].update(
        purchase_price=_decimal_text(price), quantity=quantity,
        cost_basis=_decimal_text(_multiply(price, quantity)),
    )
    updated[member] = [stock for index, stock in enumerate(updated[member]) if index == first or index not in matches]
    return updated


def remove_stock(data, member, code):
    """Return an edited deep copy with the member's position removed."""
    code = _code(code)
    _, stocks = next(_members(data, member))
    codes = [_stock_values(stock)[1] for stock in stocks]
    if code not in codes:
        raise ValidationError("삭제할 종목이 없습니다.")
    updated = deepcopy(data)
    updated[member] = [stock for stock, old_code in zip(updated[member], codes) if old_code != code]
    return updated


@dataclass(frozen=True)
class Holding:
    member: str
    name: str
    code: str
    quantity: int
    purchase_price: Decimal
    cost_basis: Decimal
    current_price: Decimal | None
    market_value: Decimal | None
    profit: Decimal | None
    roi: Decimal | None
    stale: bool
    updated_at: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    holdings: list[Holding]
    total_cost: Decimal
    known_cost: Decimal
    market_value: Decimal
    profit: Decimal
    roi: Decimal | None
    missing_count: int
    stale_count: int


def build_snapshot(data, quotes, member=None):
    """Calculate a member or family view; aggregate only successfully priced rows.

    ``total_cost`` includes every position, whereas ``known_cost``,
    ``market_value``, ``profit`` and ``roi`` cover the same priced subset.
    ``missing_count`` makes that partial coverage explicit to callers. Legacy
    duplicate codes are aggregated within each member without changing storage.
    """
    holdings = []
    total_cost = known_cost = market_value = Decimal("0")
    missing_count = stale_count = 0
    for owner, stocks in _members(data, member):
        for name, code, quantity, cost in _group_stock_values(stocks):
            average = _divide(cost, quantity)
            quote = quotes.get(code) or {}
            try:
                current_price = _positive_decimal(quote.get("price"), "현재가")
            except ValidationError:
                current_price = None
            stale = bool(quote.get("stale", False)) if current_price is not None else False
            updated_at = str(quote.get("updated_at") or "")
            value = profit = roi = None
            total_cost = _add(total_cost, cost)
            if current_price is None:
                missing_count += 1
            else:
                value = _multiply(current_price, quantity)
                profit = _add(value, cost.copy_negate())
                roi = _multiply(_divide(profit, cost), 100)
                known_cost = _add(known_cost, cost)
                market_value = _add(market_value, value)
                stale_count += int(stale)
            holdings.append(Holding(
                owner, name, code, quantity, average, cost, current_price,
                value, profit, roi, stale, updated_at,
            ))
    profit = _add(market_value, known_cost.copy_negate())
    roi = _multiply(_divide(profit, known_cost), 100) if known_cost else None
    return PortfolioSnapshot(
        holdings, total_cost, known_cost, market_value, profit, roi,
        missing_count, stale_count,
    )


def _csv_text(value):
    """Keep user-entered labels from executing as formulas in spreadsheet apps."""
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_csv(snapshot, path):
    """Export the exact visible snapshot; blanks preserve unavailable quotes."""
    def number(value):
        return "" if value is None else _decimal_text(value)

    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            "가족", "종목명", "종목코드", "수량", "매수단가(원)", "매수원금(원)",
            "현재가(원)", "평가금액(원)", "평가손익(원)", "수익률(%)", "시세상태", "시세확인시각",
        ])
        for holding in snapshot.holdings:
            status = "시세 없음" if holding.current_price is None else ("이전 시세" if holding.stale else "조회 성공")
            writer.writerow([
                _csv_text(holding.member), _csv_text(holding.name), holding.code,
                holding.quantity, number(holding.purchase_price), number(holding.cost_basis),
                number(holding.current_price), number(holding.market_value), number(holding.profit),
                number(holding.roi), status, _csv_text(holding.updated_at),
            ])
