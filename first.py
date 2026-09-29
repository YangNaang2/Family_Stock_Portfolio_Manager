"""기존 터미널 메뉴. GUI와 같은 시세·계산·안전 저장 모듈을 사용한다."""

import sys

from data_manager import DataError, load_data, save_data
from portfolio import build_snapshot
from trading import record_buy, record_sell, record_dividend, remove_holding, summarize_activity
from scraper import get_current_price


def amount(value, suffix="원", precision=2):
    return "미확인" if value is None else f"{value:,.{precision}f}{suffix}"


def display_portfolio(member_name, family_stocks):
    stocks = family_stocks.get(member_name, [])
    if not stocks:
        print("등록된 주식 정보가 없습니다.")
        return
    print(f"\n=== {member_name}의 포트폴리오 · 네이버 시세 조회 ===")
    quotes = {code: get_current_price(code) for code in {stock["code"] for stock in stocks}}
    snapshot = build_snapshot(family_stocks, quotes, member_name)
    for holding in snapshot.holdings:
        print(f"{holding.name} ({holding.code}) · {holding.quantity:,}주 | "
              f"평균 매수가 {amount(holding.purchase_price)} | 현재가 {amount(holding.current_price)} | "
              f"평가손익 {amount(holding.profit)} | 수익률 {amount(holding.roi, '%')}")
    known = snapshot.missing_count < len(snapshot.holdings)
    print(f"총 매수금액: {amount(snapshot.total_cost)}")
    if snapshot.missing_count:
        print(f"시세 미확인 {snapshot.missing_count}건 제외 · 평가금액·손익·수익률은 조회된 종목만 반영합니다.")
    print(f"평가금액: {amount(snapshot.market_value if known else None)} | "
          f"평가손익: {amount(snapshot.profit if known else None)} | 수익률: {amount(snapshot.roi, '%')}")
    print("미실현 손익 · 매도 비용 미포함")
    summary = summarize_activity(family_stocks, member_name)
    print(f"누적 실현손익: {amount(summary['realized_profit'])} | 순배당: {amount(summary['dividends'])}")


def persist(candidate, previous):
    try:
        save_data(candidate)
    except (DataError, OSError) as exc:
        print(f"저장 실패 · 변경 사항을 적용하지 않았습니다: {exc}")
        return previous
    print("저장 완료")
    return candidate


def add_stock(member_name, family_stocks):
    print(f"\n[{member_name}] 주식 매수 기록 추가 · 같은 종목코드는 합산합니다.")
    name = input("종목명: ").strip()
    code = input("6자리 종목코드: ").strip()
    price = input("매수 단가(원): ").strip()
    try:
        quantity = int(input("매수 수량(주): "))
        candidate = record_buy(family_stocks, member_name, name, code, price, quantity)
    except ValueError as exc:
        print(f"입력 확인: {exc}")
        return family_stocks
    return persist(candidate, family_stocks)


def delete_stock(member_name, family_stocks):
    stocks = build_snapshot(family_stocks, {}, member_name).holdings
    if not stocks:
        print("삭제할 주식이 없습니다.")
        return family_stocks
    for index, stock in enumerate(stocks, 1):
        print(f"{index}. {stock.name} ({stock.code}) · {stock.quantity:,}주")
    try:
        index = int(input("삭제할 주식 번호: ")) - 1
        if not 0 <= index < len(stocks):
            raise ValueError("목록에 있는 번호를 선택해 주세요.")
    except ValueError as exc:
        print(f"입력 확인: {exc}")
        return family_stocks
    stock = stocks[index]
    if input(f"{stock.name} ({stock.code})의 보유 기록을 삭제할까요? 매도 거래로 기록되지 않습니다. [y/N]: ").strip().lower() != "y":
        return family_stocks
    try:
        candidate = remove_holding(family_stocks, member_name, stock.code)
    except ValueError as exc:
        print(f"삭제 기록 확인: {exc}")
        return family_stocks
    return persist(candidate, family_stocks)


def sell_stock(member_name, family_stocks):
    try:
        code = input("매도할 종목코드: ").strip()
        price = input("매도 단가(원): ").strip()
        quantity = int(input("매도 수량: "))
        fee = input("수수료(원, Enter=0): ").strip() or "0"
        tax = input("세금(원, Enter=0): ").strip() or "0"
        candidate = record_sell(family_stocks, member_name, code, price, quantity, fee=fee, tax=tax)
    except ValueError as exc:
        print(f"입력 확인: {exc}")
        return family_stocks
    return persist(candidate, family_stocks)


def add_dividend(member_name, family_stocks):
    try:
        code = input("배당 종목코드: ").strip()
        gross = input("세전 배당액(원): ").strip()
        tax = input("원천징수 세금(원, Enter=0): ").strip() or "0"
        candidate = record_dividend(family_stocks, member_name, code, gross, tax=tax)
    except ValueError as exc:
        print(f"입력 확인: {exc}")
        return family_stocks
    return persist(candidate, family_stocks)


def member_menu(member_name, family_stocks):
    while True:
        print(f"\n[{member_name}]  1. 포트폴리오 조회  2. 매수 기록 추가  3. 보유 기록 삭제  4. 매도 기록  5. 배당 기록  0. 돌아가기")
        choice = input("작업 번호: ").strip()
        if choice == "1":
            display_portfolio(member_name, family_stocks)
        elif choice == "2":
            family_stocks = add_stock(member_name, family_stocks)
        elif choice == "3":
            family_stocks = delete_stock(member_name, family_stocks)
        elif choice == "4":
            family_stocks = sell_stock(member_name, family_stocks)
        elif choice == "5":
            family_stocks = add_dividend(member_name, family_stocks)
        elif choice == "0":
            return family_stocks
        else:
            print("목록에 있는 번호를 선택해 주세요.")


def main():
    try:
        family_stocks = load_data()
    except (DataError, OSError) as exc:
        print(f"포트폴리오를 열 수 없습니다: {exc}")
        return 1
    while True:
        print("\n=== 가족 주식 관리 프로그램 ===")
        members = list(family_stocks)
        for index, member in enumerate(members, 1):
            print(f"{index}. {member}")
        choice = input("가족 번호 (0: 종료): ").strip()
        if choice == "0":
            return 0
        try:
            index = int(choice) - 1
            if not 0 <= index < len(members):
                raise ValueError("목록에 있는 번호를 선택해 주세요.")
        except ValueError as exc:
            print(f"입력 확인: {exc}")
            continue
        family_stocks = member_menu(members[index], family_stocks)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n프로그램을 종료합니다.")
