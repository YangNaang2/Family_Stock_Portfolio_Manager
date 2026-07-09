import os
import json
import requests
from bs4 import BeautifulSoup

# 데이터가 저장될 파일 이름
DATA_FILE = "family_stocks.json"

def get_current_price(code):
    """네이버 금융에서 종목 코드를 이용해 실시간 현재가를 가져오는 함수"""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        # 네이버 금융에서 현재가가 위치한 HTML 태그 추출
        today_div = soup.find('div', class_='today')
        price_text = today_div.find('span', class_='blind').text
        return int(price_text.replace(',', ''))
    except Exception:
        # 네트워크 오류나 종목 코드가 잘못된 경우 None 반환
        return None

def load_data():
    """프로그램 시작 시 JSON 파일에서 데이터를 읽어오는 함수"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    else:
        # 파일이 없으면 사용할 초기 데이터 구조 (테스트용 종목코드 및 매수단가 적용)
        return {
            "아빠": [
                {"name": "삼성전자", "code": "005930", "purchase_price": 72000, "quantity": 100},
                {"name": "현대차", "code": "005380", "purchase_price": 240000, "quantity": 50}
            ],
            "엄마": [
                {"name": "카카오", "code": "035720", "purchase_price": 48000, "quantity": 200},
                {"name": "NAVER", "code": "035420", "purchase_price": 190000, "quantity": 30}
            ],
            "나": [
                {"name": "SK하이닉스", "code": "000660", "purchase_price": 160000, "quantity": 40}
            ]
        }

def save_data(data):
    """데이터가 변경될 때마다 JSON 파일에 저장하는 함수"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def display_portfolio(member_name, family_stocks):
    """1. 실시간 주가를 반영하여 수익률과 총 자산을 출력하는 함수"""
    print(f"\n=== 📊 {member_name}의 주식 포트폴리오 (실시간 조회) ===")
    stocks = family_stocks.get(member_name, [])
    
    if not stocks:
        print("등록된 주식 정보가 없습니다.")
        return

    total_purchase_val = 0  # 총 매수 금액
    total_current_val = 0   # 총 평가 금액

    print(f"{'주식명(코드)':<14} | {'매수단가':<10} | {'현재가':<10} | {'수량':<5} | {'평가손익':<12} | {'수익률':<7}")
    print("-" * 85)
    
    for stock in stocks:
        code = stock['code']
        # 실시간 가격 가져오기 (인터넷 안되면 기존 매수단가로 대체해 에러 방지)
        current_price = get_current_price(code)
        if current_price is None:
            current_price = stock['purchase_price']
            price_status = "(조회실패)"
        else:
            price_status = ""

        purchase_price = stock['purchase_price']
        qty = stock['quantity']

        buy_amount = purchase_price * qty       # 이 종목을 산 총 금액
        current_amount = current_price * qty    # 이 종목의 현재 가치
        
        profit = current_amount - buy_amount    # 평가손익
        # 수익률 계산: (평가손익 / 매수금액) * 100
        roi = (profit / buy_amount) * 100 if buy_amount > 0 else 0 

        total_purchase_val += buy_amount
        total_current_val += current_amount

        # 수익률 양수/음수에 따른 기호 표시
        roi_sign = "+" if roi > 0 else ""
        
        stock_label = f"{stock['name']}({code})"
        print(f"{stock_label:<14} | {purchase_price:>10,}원 | {current_price:>10,}원{price_status} | {qty:>4}주 | {profit:>11,}원 | {roi_sign}{roi:>6.2f}%")
        
    total_profit = total_current_val - total_purchase_val
    total_roi = (total_profit / total_purchase_val) * 100 if total_purchase_val > 0 else 0
    total_roi_sign = "+" if total_profit > 0 else ""

    print("-" * 85)
    print(f"💰 총 매수금액: {total_purchase_val:,}원 ➡️ 총 평가금액: {total_current_val:,}원")
    print(f"📈 총 평가손익: {total_profit:,}원 ({total_roi_sign}{total_roi:.2f}%)")


def add_stock(member_name, family_stocks):
    """2. 주식을 새로 추가하는 함수 (종목코드와 매수단가 입력)"""
    print(f"\n➕ [{member_name}] 주식 추가하기")
    name = input("👉 주식 이름을 입력하세요 (예: 삼성전자): ").strip()
    code = input("👉 6자리 종목코드를 입력하세요 (예: 005930): ").strip()
    
    if not name or len(code) != 6:
        print("❌ 주식 이름이나 종목코드(6자리)가 올바르지 않습니다.")
        return

    try:
        purchase_price = int(input("👉 평단가(내가 산 가격)를 입력하세요: "))
        quantity = int(input("👉 보유 수량(주)을 입력하세요: "))
    except ValueError:
        print("❌ 가격과 수량은 숫자로만 입력해야 합니다.")
        return

    stocks = family_stocks[member_name]
    
    # 이미 가지고 있는 종목코드라면 평단가 평점 및 수량 누적 계산
    for stock in stocks:
        if stock['code'] == code:
            old_total = stock['purchase_price'] * stock['quantity']
            new_total = purchase_price * quantity
            stock['quantity'] += quantity
            # 가중평균을 이용한 새로운 매수단가(평단가) 계산
            stock['purchase_price'] = int((old_total + new_total) / stock['quantity'])
            print(f"✅ 기존 보유 종목입니다. 수량이 더해지고 평균 매수단가가 {stock['purchase_price']:,}원으로 갱신되었습니다.")
            save_data(family_stocks)
            return

    stocks.append({"name": name, "code": code, "purchase_price": purchase_price, "quantity": quantity})
    save_data(family_stocks)
    print(f"✅ [{name}] 주식이 성공적으로 등록되었습니다.")


def delete_stock(member_name, family_stocks):
    """3. 등록된 주식을 삭제하는 함수"""
    print(f"\n❌ [{member_name}] 주식 삭제하기")
    stocks = family_stocks[member_name]
    
    if not stocks:
        print("삭제할 주식이 없습니다.")
        return

    print("현재 보유 주식 목록:")
    for i, stock in enumerate(stocks, 1):
        print(f" [{i}] {stock['name']}({stock['code']}) - {stock['quantity']}주")
    
    try:
        choice = int(input("👉 삭제할 주식의 번호를 선택하세요: ")) - 1
        if 0 <= choice < len(stocks):
            removed = stocks.pop(choice)
            save_data(family_stocks)
            print(f"✅ [{removed['name']}] 주식이 완전히 삭제되었습니다.")
        else:
            print("❌ 잘못된 번호입니다.")
    except ValueError:
        print("❌ 숫자를 입력해주세요.")


def member_menu(member_name, family_stocks):
    """가족 구성원 세부 메뉴"""
    while True:
        print(f"\n⚙️ [{member_name}]님 관리 메뉴")
        print("1. 실시간 주식 포트폴리오 조회 및 수익률 확인")
        print("2. 주식 매수 기록 추가")
        print("3. 주식 삭제")
        print("0. 메인 화면으로 돌아가기")
        
        choice = input("\n👉 원하시는 작업 번호를 선택하세요: ")
        
        if choice == '1':
            display_portfolio(member_name, family_stocks)
        elif choice == '2':
            add_stock(member_name, family_stocks)
        elif choice == '3':
            delete_stock(member_name, family_stocks)
        elif choice == '0':
            break
        else:
            print("❌ 잘못된 번호입니다.")


def main():
    # 프로그램 시작 시 파일에서 데이터 불러오기
    family_stocks = load_data()
    
    while True:
        print("\n=== 🏠 가족 주식 관리 프로그램 (실시간 연동형) ===")
        members = list(family_stocks.keys())
        
        for i, member in enumerate(members, 1):
            print(f"{i}. {member}")
        print("0. 프로그램 종료")

        choice = input("\n👉 관리할 가족 번호를 선택하세요: ")

        if choice == '0':
            print("프로그램을 종료합니다. 자산이 날마다 증식하기를 바랍니다!")
            break

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(members):
                member_menu(members[idx], family_stocks)
            else:
                print("❌ 목록에 있는 번호를 선택해주세요.")
        except ValueError:
            print("❌ 숫자를 입력해주세요.")

if __name__ == "__main__":
    main()