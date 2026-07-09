import requests
from bs4 import BeautifulSoup

def get_current_price(code):
    """종목 코드를 이용해 현재가와 전일대비 등락률을 가져오는 함수"""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        today_div = soup.find('div', class_='today')
        
        # 현재가
        price_text = today_div.find('span', class_='blind').text
        current_price = int(price_text.replace(',', ''))
        
        # 전일대비 등락률 (%)
        # 네이버 금융 페이지 내 'n_set_stat' 혹은 'tah' 클래스 내부 텍스트 추출
        rate_element = soup.select_one('.no_today .n_set_stat .blind')
        if not rate_element:
            # 보조 선택자 (페이지 구조가 다를 경우 대비)
            rate_element = soup.select_one('.no_today .tah.p11')
            
        change_rate = rate_element.text.strip() if rate_element else "0%"
        
        return {"price": current_price, "rate": change_rate}
    except Exception:
        return None

def get_market_info():
    """코스피, 코스닥, 환율 및 각 지수의 등락률까지 가져오는 함수"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    result = {
        "KOSPI": "-", "KOSPI_RATE": "0%", 
        "KOSDAQ": "-", "KOSDAQ_RATE": "0%", 
        "USD": "-", "USD_RATE": "0%"
    }
    
    try:
        # 1. 코스피, 코스닥
        url_sise = "https://finance.naver.com/sise/"
        res_sise = requests.get(url_sise, headers=headers)
        soup_sise = BeautifulSoup(res_sise.text, 'html.parser')
        
        result["KOSPI"] = soup_sise.select_one('#KOSPI_now').text
        result["KOSPI_RATE"] = soup_sise.select_one('#KOSPI_change').text.strip().replace('상승', '+').replace('하락', '-')
        
        result["KOSDAQ"] = soup_sise.select_one('#KOSDAQ_now').text
        result["KOSDAQ_RATE"] = soup_sise.select_one('#KOSDAQ_change').text.strip().replace('상승', '+').replace('하락', '-')
        
        # 2. 환율
        url_market = "https://finance.naver.com/marketindex/"
        res_market = requests.get(url_market, headers=headers)
        soup_market = BeautifulSoup(res_market.text, 'html.parser')
        
        result["USD"] = soup_market.select_one('.value').text
        result["USD_RATE"] = soup_market.select_one('.change').text.strip()
        
    except Exception:
        pass
        
    return result