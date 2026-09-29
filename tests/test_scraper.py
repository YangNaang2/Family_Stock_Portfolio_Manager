"""Provider response fixtures are embedded; these tests never use the network."""

import unittest
from unittest.mock import Mock, patch

import requests

import scraper


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def stock_item(code="005930", price="75,000", rate="1.23", direction="2"):
    return {
        "itemCode": code,
        "closePrice": price,
        "fluctuationsRatio": rate,
        "compareToPreviousPrice": {"code": direction},
    }


def exchange_payload(price="1,355.00", rate="-0.37"):
    return {"exchangeInfo": {
        "reutersCode": "FX_USDKRW",
        "closePrice": price,
        "fluctuationsRatio": rate,
        "fluctuationsType": {"code": "5"},
    }}


class QuoteTests(unittest.TestCase):
    @patch("scraper.requests.get")
    def test_actual_price_and_daily_rate_with_finite_waits(self, get):
        result = response({"datas": [stock_item()]})
        get.return_value = result
        self.assertEqual(scraper.get_current_price("005930"), {"price": 75000, "rate": "+1.23%"})
        get.assert_called_once_with(
            scraper.STOCK_URL.format("005930"), headers=scraper.HEADERS,
            timeout=(3, 5), allow_redirects=False,
        )
        result.raise_for_status.assert_called_once()

    @patch("scraper.requests.get")
    def test_rate_direction_and_unchanged_are_real_data(self, get):
        for rate, direction, expected in [("1.23", "5", "-1.23%"), ("-1.23", "5", "-1.23%"), ("0", "3", "0.00%")]:
            with self.subTest(rate=rate, direction=direction):
                get.return_value = response({"datas": [stock_item(rate=rate, direction=direction)]})
                self.assertEqual(scraper.get_current_price("005930")["rate"], expected)

    @patch("scraper.requests.get")
    def test_missing_rate_is_unknown_instead_of_flat(self, get):
        get.return_value = response({"datas": [stock_item(rate=None)]})
        self.assertEqual(scraper.get_current_price("005930"), {"price": 75000, "rate": "-"})

    @patch("scraper.requests.get")
    def test_invalid_stock_codes_never_reach_network(self, get):
        for code in [None, 5930, "5930", "0059300", " 005930", "００５９３０", "005930&x=1"]:
            with self.subTest(code=code):
                self.assertIsNone(scraper.get_current_price(code))
        get.assert_not_called()

    @patch("scraper.requests.get")
    def test_missing_malformed_and_wrong_stock_data_fail_explicitly(self, get):
        payloads = [None, [], {}, {"datas": {}}, {"datas": [None]}, {"datas": [stock_item(code="000660")]}]
        payloads += [{"datas": [stock_item(price=price)]} for price in [None, "", "-", "NaN", "Infinity", 0, -1, True, "1,2", "75000.5"]]
        for payload in payloads:
            with self.subTest(payload=payload):
                get.return_value = response(payload)
                self.assertIsNone(scraper.get_current_price("005930"))

    @patch("scraper.requests.get")
    def test_http_errors_timeouts_and_bad_json_are_missing(self, get):
        for error in [requests.Timeout(), requests.ConnectionError()]:
            with self.subTest(error=type(error).__name__):
                get.side_effect = error
                self.assertIsNone(scraper.get_current_price("005930"))
        get.side_effect = None
        http_error = response({"datas": [stock_item()]}, status=503)
        http_error.raise_for_status.side_effect = requests.HTTPError()
        get.return_value = http_error
        self.assertIsNone(scraper.get_current_price("005930"))
        http_error.json.assert_not_called()
        malformed = response(None)
        malformed.json.side_effect = ValueError("not JSON")
        get.return_value = malformed
        self.assertIsNone(scraper.get_current_price("005930"))

    @patch("scraper.requests.get")
    def test_redirect_does_not_follow_new_unknown_source(self, get):
        get.return_value = response({"datas": [stock_item()]}, status=302)
        self.assertIsNone(scraper.get_current_price("005930"))
        get.return_value.json.assert_not_called()


class MarketTests(unittest.TestCase):
    @patch("scraper.requests.get")
    def test_indices_and_exchange_have_individual_signed_percentages(self, get):
        get.side_effect = [
            response({"datas": [
                stock_item("KOSPI", "2,500.15", "0.27", "5"),
                stock_item("KOSDAQ", "849.80", "0.38", "2"),
            ]}),
            response(exchange_payload()),
        ]
        self.assertEqual(scraper.get_market_info(), {
            "KOSPI": "2,500.15", "KOSPI_RATE": "-0.27%",
            "KOSDAQ": "849.80", "KOSDAQ_RATE": "+0.38%",
            "USD": "1,355.00", "USD_RATE": "-0.37%",
        })
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs["timeout"], (3, 5))

    @patch("scraper.requests.get")
    def test_index_failure_still_fetches_exchange(self, get):
        get.side_effect = [requests.Timeout(), response(exchange_payload())]
        result = scraper.get_market_info()
        self.assertEqual(result["KOSPI"], "-")
        self.assertEqual(result["KOSPI_RATE"], "-")
        self.assertEqual(result["USD"], "1,355.00")

    @patch("scraper.requests.get")
    def test_bad_one_index_does_not_discard_other_index(self, get):
        get.side_effect = [
            response({"datas": [stock_item("KOSPI", "NaN"), stock_item("KOSDAQ", "849.80", None)]}),
            requests.ConnectionError(),
        ]
        result = scraper.get_market_info()
        self.assertEqual(result["KOSPI"], "-")
        self.assertEqual(result["KOSDAQ"], "849.80")
        self.assertEqual(result["KOSDAQ_RATE"], "-")
        self.assertEqual(result["USD"], "-")
        self.assertEqual(result["USD_RATE"], "-")

    @patch("scraper.requests.get")
    def test_invalid_payloads_and_currency_leave_unknown_values(self, get):
        for payload in [None, [], {}, {"exchangeInfo": []}, {"exchangeInfo": {"reutersCode": "FX_JPYKRW", "closePrice": "999"}}]:
            with self.subTest(payload=payload):
                get.side_effect = [response({"datas": []}), response(payload)]
                self.assertTrue(all(value == "-" for value in scraper.get_market_info().values()))


if __name__ == "__main__":
    unittest.main()
