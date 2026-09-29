"""Portfolio planning, local price alerts, history and member administration.

All edits return independent PortfolioData copies. Targets describe the user's
own plan; this module never submits orders or sends external notifications.
"""

from copy import deepcopy
from datetime import date as Date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from data_manager import ensure_portfolio_data, validate_metadata
from portfolio import (ValidationError, _add, _code, _decimal_text, _divide,
                       _multiply, _positive_decimal, build_snapshot)


def _scope(data, member):
    if member is not None and member not in data:
        raise ValidationError("선택한 가족 구성원이 없습니다.")
    return "*" if member is None else member


def get_targets(data, member=None):
    """Return an independent code -> decimal-string target mapping."""
    scope = _scope(data, member)
    return deepcopy(getattr(data, "metadata", {}).get("targets", {}).get(scope, {}))


def set_targets(data, member, targets):
    """Save positive percentage targets totaling at most 100; {} clears them.

    Unheld six-digit codes are allowed. Any unassigned percentage remains
    unallocated; this app does not track cash or suggest trades automatically.
    """
    scope = _scope(data, member)
    if not isinstance(targets, dict):
        raise ValidationError("목표 비중은 종목코드와 비중으로 입력해 주세요.")
    normalized, total = {}, Decimal(0)
    for code, percentage in targets.items():
        normalized_code = _code(code)
        if normalized_code in normalized:
            raise ValidationError("목표 비중에 같은 종목이 중복되었습니다.")
        value = _positive_decimal(percentage, "목표 비중", maximum=Decimal(100))
        total = _add(total, value)
        normalized[normalized_code] = _decimal_text(value)
    if total > 100:
        raise ValidationError("목표 비중의 합계는 100% 이하여야 합니다.")
    updated = ensure_portfolio_data(data)
    if normalized:
        updated.metadata["targets"][scope] = normalized
    else:
        updated.metadata["targets"].pop(scope, None)
    validate_metadata(updated.metadata, updated)
    return updated


def allocation_rows(data, quotes, member=None):
    """Compare current market-value weights with explicit user targets.

    ``drift`` is current minus target, in percentage points. When any quote is
    absent or stale, every weight and drift is None so a partial valuation cannot
    masquerade as a complete allocation. ``current_value`` is the known value.
    """
    snapshot = build_snapshot(data, quotes, member)
    targets = get_targets(data, member)
    grouped = {}
    for holding in snapshot.holdings:
        row = grouped.setdefault(holding.code, {"code": holding.code, "name": holding.name,
                                               "current_value": Decimal(0)})
        if holding.market_value is not None:
            row["current_value"] = _add(row["current_value"], holding.market_value)
    for code in targets:
        grouped.setdefault(code, {"code": code, "name": code, "current_value": Decimal(0)})
    complete = not snapshot.missing_count and not snapshot.stale_count
    rows = []
    for code in sorted(grouped):
        row = grouped[code]
        percentage = None
        if complete:
            percentage = (_multiply(_divide(row["current_value"], snapshot.market_value), 100)
                          if snapshot.market_value else Decimal(0))
        target = Decimal(targets.get(code, "0"))
        row.update(current_percent=percentage, target_percent=target,
                   drift=None if percentage is None else _add(percentage, target.copy_negate()))
        rows.append(row)
    return rows


def _snapshot_date(value):
    if value is None:
        return Date.today().isoformat()
    try:
        if isinstance(value, Date) and not isinstance(value, datetime):
            result = value
        elif isinstance(value, str):
            result = Date.fromisoformat(value)
            if result.isoformat() != value:
                raise ValueError
        else:
            raise ValueError
        if result > Date.today():
            raise ValueError
    except ValueError as exc:
        raise ValidationError("평가 기록일은 오늘 이전의 YYYY-MM-DD 날짜로 입력해 주세요.") from exc
    return result.isoformat()


def add_snapshot(data, quotes, member=None, date=None):
    """Record a complete, fresh valuation; replace the same scope's daily row.

    These are saved valuations, not a time-weighted investment return: deposits,
    withdrawals and position corrections can also move the chart's value.
    """
    scope = _scope(data, member)
    day = _snapshot_date(date)
    snapshot = build_snapshot(data, quotes, member)
    if snapshot.missing_count or snapshot.stale_count:
        raise ValidationError("모든 보유 종목의 최신 시세를 확인한 뒤 평가 기록을 저장해 주세요.")
    updated = ensure_portfolio_data(data)
    entries = updated.metadata["snapshots"]
    previous = next((entry for entry in entries if entry["member"] == scope and entry["date"] == day), None)
    record = {"id": previous["id"] if previous else str(uuid4()),
              "recorded_at": datetime.now(timezone.utc).isoformat(), "date": day, "member": scope,
              "total_cost": _decimal_text(snapshot.total_cost),
              "market_value": _decimal_text(snapshot.market_value), "profit": _decimal_text(snapshot.profit),
              "holding_count": len(snapshot.holdings), "priced_count": len(snapshot.holdings),
              "missing_count": 0, "stale_count": 0}
    updated.metadata["snapshots"] = [entry for entry in entries
                                     if not (entry["member"] == scope and entry["date"] == day)] + [record]
    updated.metadata["snapshots"].sort(key=lambda entry: (entry["date"], entry["member"]))
    validate_metadata(updated.metadata, updated)
    return updated


def set_alert(data, code, direction, price):
    """Add/update one inclusive above/below threshold for a code, retaining ID."""
    code = _code(code)
    if direction not in ("above", "below"):
        raise ValidationError("가격 알림 조건을 이상(above) 또는 이하(below)로 선택해 주세요.")
    price = _positive_decimal(price, "알림 가격")
    updated = ensure_portfolio_data(data)
    previous = next((entry for entry in updated.metadata["alerts"]
                     if entry["code"] == code and entry["direction"] == direction), None)
    if previous is not None:
        previous["price"] = _decimal_text(price)
    else:
        updated.metadata["alerts"].append({"id": str(uuid4()), "code": code, "direction": direction,
                                           "price": _decimal_text(price)})
    validate_metadata(updated.metadata, updated)
    return updated


def remove_alert(data, id):
    updated = ensure_portfolio_data(data)
    if not any(entry["id"] == id for entry in updated.metadata["alerts"]):
        raise ValidationError("삭제할 가격 알림이 없습니다.")
    updated.metadata["alerts"] = [entry for entry in updated.metadata["alerts"] if entry["id"] != id]
    return updated


def check_alerts(data, quotes):
    """Return matching local alerts; absent/invalid/stale prices never trigger."""
    matches = []
    for entry in getattr(data, "metadata", {}).get("alerts", []):
        quote = quotes.get(entry["code"]) or {}
        if quote.get("stale", False):
            continue
        try:
            price = _positive_decimal(quote.get("price"), "현재가")
        except ValidationError:
            continue
        threshold = Decimal(entry["price"])
        if ((entry["direction"] == "above" and price >= threshold) or
                (entry["direction"] == "below" and price <= threshold)):
            matches.append(dict(entry, current_price=price))
    return matches


def _member_name(value):
    if not isinstance(value, str) or not value.strip() or value.strip() == "*":
        raise ValidationError("가족 이름을 입력해 주세요. '*'는 전체 가족용으로 예약되어 있습니다.")
    return value.strip()


def rename_member(data, old, new):
    if old not in data:
        raise ValidationError("변경할 가족 구성원이 없습니다.")
    new = _member_name(new)
    if new != old and new in data:
        raise ValidationError("이미 같은 이름의 가족 구성원이 있습니다.")
    updated = ensure_portfolio_data(data)
    if new == old:
        return updated
    stocks = updated.pop(old)
    updated[new] = stocks
    targets = updated.metadata["targets"]
    if old in targets:
        targets[new] = targets.pop(old)
    for key in ("transactions", "snapshots"):
        for entry in updated.metadata[key]:
            if entry["member"] == old:
                entry["member"] = new
    validate_metadata(updated.metadata, updated)
    return updated


def remove_member(data, member):
    if member not in data:
        raise ValidationError("삭제할 가족 구성원이 없습니다.")
    if data[member]:
        raise ValidationError("보유 종목이 있는 가족은 삭제할 수 없습니다.")
    updated = ensure_portfolio_data(data)
    if any(entry["member"] == member for entry in updated.metadata["transactions"]):
        raise ValidationError("거래 기록이 있는 가족은 기록 보존을 위해 삭제할 수 없습니다.")
    del updated[member]
    updated.metadata["targets"].pop(member, None)
    updated.metadata["snapshots"] = [entry for entry in updated.metadata["snapshots"] if entry["member"] != member]
    validate_metadata(updated.metadata, updated)
    return updated
