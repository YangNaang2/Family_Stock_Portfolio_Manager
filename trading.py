"""Append-only trade bookkeeping with decimal costs and an average-cost ledger.

Every edit returns a deep copy. Saving remains the caller's responsibility, so a
failed disk write can leave the displayed portfolio and its ledger unchanged.
Buy commissions are capitalized; sell commissions/taxes reduce realized profit.
Dividend amounts are gross, with their net cash recorded separately in summaries.

Existing holdings receive one OPENING entry per member/code on first use. That
entry records today's migration date, not an invented historical purchase date.
It is excluded from the chronology cutoff. Later entries must be appended in
date order for each member/code; same-day entries retain their insertion order.
"""

import csv
from datetime import date as calendar_date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import re
from uuid import uuid4

from data_manager import DataError, ensure_portfolio_data, validate_data, validate_metadata
from portfolio import (
    MAX_COST, ValidationError, _add, _code, _csv_text, _decimal_text,
    _divide, _group_stock_values, _members, _multiply, _positive_decimal,
    _quantity, _stock_values, buy_stock, remove_stock, update_stock,
    validate_stock_input,
)


_ZERO = Decimal("0")
_MONEY_QUANTUM = Decimal("1e-80")
_OPENING_NOTE = "기존 보유정보의 최초 장부 등록입니다. 날짜는 이전 매수일이 아닌 등록일입니다."


def _nonnegative_decimal(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError(f"{label}은(는) 0 이상의 숫자여야 합니다.")
    try:
        text = str(value).strip()
    except ValueError:
        raise ValidationError(f"{label}의 숫자 길이가 너무 깁니다.") from None
    if len(text) > 160:
        raise ValidationError(f"{label}의 숫자 길이가 너무 깁니다.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValidationError(f"{label}은(는) 0 이상의 숫자여야 합니다.") from None
    if not number.is_finite() or number < 0 or number > MAX_COST:
        raise ValidationError(f"{label}은(는) 0 이상 {MAX_COST:,.0f} 이하로 입력해 주세요.")
    if len(number.as_tuple().digits) > 120 or number.as_tuple().exponent < -80:
        raise ValidationError(f"{label}의 소수 자릿수가 너무 많습니다.")
    return _ZERO if number == 0 else number


def _text(value, label="메모"):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValidationError(f"{label}는 2,000자 이하의 문자열이어야 합니다.")
    return value.strip()


def _date(value):
    if value is None:
        result = calendar_date.today()
    elif isinstance(value, calendar_date) and not isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            result = calendar_date.fromisoformat(value)
        except ValueError:
            raise ValidationError("거래일은 올바른 YYYY-MM-DD 날짜여야 합니다.") from None
    else:
        raise ValidationError("거래일은 YYYY-MM-DD 형식으로 입력해 주세요.")
    if result > calendar_date.today():
        raise ValidationError("미래 날짜의 거래는 등록할 수 없습니다.")
    return result.isoformat()


def _entry(kind, member, code, name, date, *, quantity=0, price=_ZERO,
           amount=_ZERO, fee=_ZERO, tax=_ZERO, cost_basis=_ZERO,
           realized_profit=_ZERO, note="", **extra):
    entry = {
        "id": str(uuid4()), "type": kind, "date": date,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "member": member, "code": code, "name": name,
        "quantity": str(quantity), "price": _decimal_text(price),
        "amount": _decimal_text(amount), "fee": _decimal_text(fee),
        "tax": _decimal_text(tax), "cost_basis": _decimal_text(cost_basis),
        "realized_profit": _decimal_text(realized_profit), "note": note,
    }
    entry.update(extra)
    return entry


def bootstrap_ledger(data):
    """Copy data and register existing positions once, without changing holdings."""
    try:
        updated = ensure_portfolio_data(data)
        validate_data(updated)
    except DataError as exc:
        raise ValidationError(str(exc)) from exc
    metadata = updated.metadata
    if metadata.get("ledger_initialized", False):
        return updated
    if metadata["transactions"]:
        raise ValidationError("기존 거래 원장의 초기화 상태가 올바르지 않습니다.")
    today = _date(None)
    for member, stocks in _members(updated):
        for name, code, quantity, cost in _group_stock_values(stocks):
            metadata["transactions"].append(_entry(
                "OPENING", member, code, name, today, quantity=quantity,
                price=_divide(cost, quantity), amount=cost, cost_basis=cost,
                note=_OPENING_NOTE,
            ))
    metadata["ledger_initialized"] = True
    try:
        validate_metadata(metadata, updated)
    except DataError as exc:
        raise ValidationError(str(exc)) from exc
    return updated


def _check_chronology(data, member, code, date):
    dates = [
        entry["date"] for entry in data.metadata["transactions"]
        if entry["member"] == member and entry["code"] == code
        and entry["type"] != "OPENING"
    ]
    if dates and date < max(dates):
        raise ValidationError(
            f"이 종목의 마지막 거래일({max(dates)})보다 이전 날짜는 등록할 수 없습니다. "
            "평균 매수원가를 보호하기 위해 날짜순으로 입력해 주세요."
        )


def _prepare(data, member, code, date):
    code = _code(code)
    date = _date(date)
    next(_members(data, member))
    updated = bootstrap_ledger(data)
    _check_chronology(updated, member, code, date)
    return updated, code, date


def _position(data, member, code):
    _, stocks = next(_members(data, member))
    for name, candidate, quantity, cost in _group_stock_values(stocks):
        if candidate == code:
            return name, quantity, cost
    raise ValidationError("선택한 가족의 보유 종목이 없습니다.")


def _finish(data, entry):
    data.metadata["transactions"].append(entry)
    try:
        validate_data(data)
        validate_metadata(data.metadata, data)
    except DataError as exc:
        raise ValidationError(str(exc)) from exc
    return data


def record_buy(data, member, name, code, price, quantity, date=None, fee="0", note=""):
    """Buy shares; add the commission to exact principal and append a BUY entry."""
    name, code, price, quantity = validate_stock_input(name, code, price, quantity)
    fee = _nonnegative_decimal(fee, "매수 수수료")
    note = _text(note)
    updated, code, date = _prepare(data, member, code, date)
    updated = buy_stock(updated, member, name, code, price, quantity)
    position = next(stock for stock in updated[member] if stock["code"] == code)
    gross = _multiply(price, quantity)
    purchase_cost = _add(gross, fee)
    total_cost = _add(Decimal(position["cost_basis"]), fee)
    _positive_decimal(total_cost, "수수료 포함 매수 원금", maximum=MAX_COST)
    position["cost_basis"] = _decimal_text(total_cost)
    position["purchase_price"] = _decimal_text(_divide(total_cost, position["quantity"]))
    return _finish(updated, _entry(
        "BUY", member, code, position["name"], date, quantity=quantity,
        price=price, amount=gross, fee=fee, cost_basis=purchase_cost, note=note,
    ))


def _allocated_cost(cost, sold, held):
    if sold == held:
        return cost
    # A recurring average cannot be represented exactly. Allocate at 80 decimal
    # places, preserving the exact remainder as principal for the next sale.
    # The last sale consumes that remainder, so principal is never lost.
    with localcontext() as context:
        context.prec = max(160, len(cost.as_tuple().digits) + len(str(sold)) + len(str(held)) + 4)
        allocated = (cost * sold / held).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    # Remove insignificant zeros without Decimal.normalize(), which would use
    # the caller's current arithmetic precision and could round principal.
    allocated = Decimal(format(allocated, "f").rstrip("0").rstrip("."))
    remaining = _add(cost, allocated.copy_negate())
    if allocated <= 0 or remaining <= 0:
        raise ValidationError("매도 후 원금이 허용 소수 자릿수보다 작습니다. 수량을 조정해 주세요.")
    return allocated


def record_sell(data, member, code, price, quantity, date=None, fee="0", tax="0", note=""):
    """Sell using average cost; reject oversells and keep exact residual cost."""
    price = _positive_decimal(price, "매도 단가")
    quantity = _quantity(quantity)
    fee = _nonnegative_decimal(fee, "매도 수수료")
    tax = _nonnegative_decimal(tax, "매도 세금")
    note = _text(note)
    updated, code, date = _prepare(data, member, code, date)
    name, held, cost = _position(updated, member, code)
    if quantity > held:
        raise ValidationError(f"보유 수량({held:,}주)을 초과하여 매도할 수 없습니다.")
    allocated = _allocated_cost(cost, quantity, held)
    gross = _multiply(price, quantity)
    realized = _add(_add(_add(gross, fee.copy_negate()), tax.copy_negate()), allocated.copy_negate())
    if quantity == held:
        updated = remove_stock(updated, member, code)
    else:
        remaining_quantity = held - quantity
        _quantity(remaining_quantity)
        remaining_cost = _add(cost, allocated.copy_negate())
        matches = [index for index, stock in enumerate(updated[member]) if _stock_values(stock)[1] == code]
        first = matches[0]
        updated[member][first].update(
            quantity=remaining_quantity, cost_basis=_decimal_text(remaining_cost),
            purchase_price=_decimal_text(_divide(remaining_cost, remaining_quantity)),
        )
        updated[member] = [
            stock for index, stock in enumerate(updated[member])
            if index == first or index not in matches
        ]
    return _finish(updated, _entry(
        "SELL", member, code, name, date, quantity=quantity, price=price,
        amount=gross, fee=fee, tax=tax, cost_basis=allocated,
        realized_profit=realized, note=note,
    ))


def record_dividend(data, member, code, amount, date=None, tax="0", note=""):
    """Record gross dividend and tax, including dividends for a closed position."""
    amount = _positive_decimal(amount, "배당금", maximum=MAX_COST)
    tax = _nonnegative_decimal(tax, "배당 세금")
    if tax > amount:
        raise ValidationError("배당 세금은 배당금보다 클 수 없습니다.")
    note = _text(note)
    updated, code, date = _prepare(data, member, code, date)
    try:
        name, _, _ = _position(updated, member, code)
    except ValidationError:
        history = [entry for entry in updated.metadata["transactions"]
                   if entry["member"] == member and entry["code"] == code]
        if not history:
            raise ValidationError("보유 또는 거래 이력이 있는 종목만 배당을 기록할 수 있습니다.") from None
        name = history[-1]["name"]
    return _finish(updated, _entry(
        "DIVIDEND", member, code, name, date, amount=amount, tax=tax,
        realized_profit=_add(amount, tax.copy_negate()), note=note,
    ))


def correct_holding(data, member, code, price, quantity, reason="", date=None):
    """Correct holdings while retaining the old quantity and principal in history."""
    price = _positive_decimal(price, "정정 매수 단가")
    quantity = _quantity(quantity)
    reason = _text(reason, "정정 사유")
    updated, code, date = _prepare(data, member, code, date)
    name, previous_quantity, previous_cost = _position(updated, member, code)
    updated = update_stock(updated, member, code, price, quantity)
    cost = _multiply(price, quantity)
    return _finish(updated, _entry(
        "ADJUSTMENT", member, code, name, date, quantity=quantity, price=price,
        amount=cost, cost_basis=cost, note=reason or "보유정보 정정",
        previous_quantity=str(previous_quantity), previous_cost_basis=_decimal_text(previous_cost),
    ))


def remove_holding(data, member, code, date=None):
    """Remove a mistaken position as a correction, without inventing a sale."""
    updated, code, date = _prepare(data, member, code, date)
    name, quantity, cost = _position(updated, member, code)
    updated = remove_stock(updated, member, code)
    return _finish(updated, _entry(
        "REMOVE", member, code, name, date, quantity=quantity,
        price=_divide(cost, quantity), amount=cost, cost_basis=cost,
        note="보유정보 삭제(매도 거래 아님)",
        previous_quantity=str(quantity), previous_cost_basis=_decimal_text(cost),
    ))


def _transactions(data, member=None):
    # Validate the selected family even when no ledger has been initialized yet.
    list(_members(data, member))
    metadata = getattr(data, "metadata", {})
    return [entry for entry in metadata.get("transactions", [])
            if member is None or entry["member"] == member]


def summarize_activity(data, member=None):
    """Return exact capital gains, net dividends, commissions and taxes."""
    result = dict.fromkeys(("realized_profit", "dividends", "fees", "taxes"), _ZERO)
    for entry in _transactions(data, member):
        if entry["type"] == "SELL":
            result["realized_profit"] = _add(result["realized_profit"], Decimal(entry["realized_profit"]))
        elif entry["type"] == "DIVIDEND":
            result["dividends"] = _add(result["dividends"], Decimal(entry["realized_profit"]))
        result["fees"] = _add(result["fees"], Decimal(entry["fee"]))
        result["taxes"] = _add(result["taxes"], Decimal(entry["tax"]))
    return result


def export_transactions_csv(data, path, member=None):
    """Export the ledger in insertion order with BOM and formula-safe labels."""
    entries = _transactions(data, member)
    fields = [
        ("id", "거래ID"), ("type", "유형"), ("date", "거래일"),
        ("recorded_at", "기록시각"), ("member", "가족"), ("name", "종목명"),
        ("code", "종목코드"), ("quantity", "수량"), ("price", "단가(원)"),
        ("amount", "총금액(원)"), ("fee", "수수료(원)"), ("tax", "세금(원)"),
        ("cost_basis", "매수원가(원)"), ("realized_profit", "실현손익·순배당(원)"),
        ("previous_quantity", "정정전수량"), ("previous_cost_basis", "정정전매수원금(원)"),
        ("note", "메모"),
    ]
    numeric = {"quantity", "price", "amount", "fee", "tax", "cost_basis",
               "realized_profit", "previous_quantity", "previous_cost_basis"}
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([label for _, label in fields])
        for entry in entries:
            writer.writerow([entry.get(key, "") if key in numeric else _csv_text(entry.get(key, ""))
                             for key, _ in fields])
