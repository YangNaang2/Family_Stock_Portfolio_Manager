"""Naver market quotes with explicit missing values and bounded HTTP waits.

The former finance.naver.com HTML pages now redirect to a JavaScript app.
Use Naver's public JSON responses rather than depending on those old selectors.
These endpoints are not a contracted API and can change; callers must handle
missing quotes. A timeout limits connection/read inactivity, not total elapsed
time (for example, DNS resolution is controlled by the operating system).
"""

from decimal import Decimal, InvalidOperation
import re

import requests


HTTP_TIMEOUT = (3, 5)
HEADERS = {"User-Agent": "Mozilla/5.0"}
STOCK_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{}"
INDEX_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ"
EXCHANGE_URL = "https://api.stock.naver.com/marketindex/exchange/FX_USDKRW"

_NUMBER = re.compile(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?")


def _number(value):
    """Read an ordinary finite decimal without accepting booleans or NaN."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _rate(item, direction_key="compareToPreviousPrice"):
    number = _number(item.get("fluctuationsRatio"))
    if number is None:
        return "-"
    direction = item.get(direction_key)
    if isinstance(direction, dict):
        if direction.get("code") in ("4", "5") or direction.get("name") in (
            "FALLING", "LOWER_LIMIT",
        ):
            number = -abs(number)
        elif direction.get("code") in ("1", "2") or direction.get("name") in (
            "RISING", "UPPER_LIMIT",
        ):
            number = abs(number)
    if not number:
        return "0.00%"
    return f"{number:+.2f}%"


def _items(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("datas"), list):
        return []
    return [item for item in payload["datas"] if isinstance(item, dict)]


def _get_json(url):
    # No shared mutable Session: separate workers can safely call this module.
    response = requests.get(
        url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=False,
    )
    response.raise_for_status()
    # A redirect can signal another provider migration; do not follow endlessly.
    if 300 <= response.status_code < 400:
        return None
    return response.json()


def get_current_price(code):
    """Return {price: positive int, rate: signed percent or '-'} or None.

    ``code`` must contain exactly six ASCII digits. Missing, invalid, or failed
    prices return None so the UI can retain its previous quote as stale data.
    """
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        return None
    try:
        payload = _get_json(STOCK_URL.format(code))
    except (requests.RequestException, ValueError):
        return None
    for item in _items(payload):
        if item.get("itemCode") != code:
            continue
        price = _number(item.get("closePrice"))
        if price is None or price <= 0 or price != price.to_integral_value():
            return None
        return {"price": int(price), "rate": _rate(item)}
    return None


def get_market_info():
    """Read each source independently; unavailable fields stay '-'."""
    result = {
        "KOSPI": "-", "KOSPI_RATE": "-",
        "KOSDAQ": "-", "KOSDAQ_RATE": "-",
        "USD": "-", "USD_RATE": "-",
    }
    try:
        index_payload = _get_json(INDEX_URL)
    except (requests.RequestException, ValueError):
        index_payload = None
    for item in _items(index_payload):
        code = item.get("itemCode")
        if code not in ("KOSPI", "KOSDAQ"):
            continue
        price = _number(item.get("closePrice"))
        if price is not None and price > 0:
            result[code] = f"{price:,.2f}"
            result[f"{code}_RATE"] = _rate(item)

    try:
        exchange_payload = _get_json(EXCHANGE_URL)
    except (requests.RequestException, ValueError):
        exchange_payload = None
    if isinstance(exchange_payload, dict):
        exchange = exchange_payload.get("exchangeInfo")
        if isinstance(exchange, dict) and exchange.get("reutersCode") == "FX_USDKRW":
            price = _number(exchange.get("closePrice"))
            if price is not None and price > 0:
                result["USD"] = f"{price:,.2f}"
                result["USD_RATE"] = _rate(exchange, "fluctuationsType")
    return result
