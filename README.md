# 👨‍👩‍👧‍👦 가족 주식 관리 시스템 Pro (Family Stock Portfolio Manager)
<p align="left">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/PyQt5-41CD52?style=flat-square&logo=qt&logoColor=white"/>
  <img src="https://img.shields.io/badge/Matplotlib-11557c?style=flat-square"/>
  <img src="https://img.shields.io/badge/BeautifulSoup4-8B0000?style=flat-square"/>
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat-square"/>
</p>

## 👨‍💻 프로젝트 동기

<div align="center">
  <strong>"저번에 산거 수익률이 지금 얼마지?"</strong>
</div>
<br>
<div align="center">
투자를 하다 보면 기술적으로 '지금이 매수 적기'라고 판단되는 순간이 있다.<br>
나를 포함한 가족 구성원 모두가 각자의 성향에 따라 다양한 종목을 운용하고 있다.<br>
좋은 매수 타이밍이 올 때마다 매번 가족들에게 메신저를 보내 보유 현황과 평단가를 일일이 물어봐야 했다.<br>
</div>

**"가족들의 전체 주식 현황을 실시간으로 파악하고 있다면 훨씬 스마트하게 관리할 수 있지 않을까?"**

그렇기에 파이썬과 PyQt5를 활용하여 가족 구성원별 주식 자산을 실시간으로 관리하고 시각화하는 데스크톱 애플리케이션을 개발하였다.<br>
네이버 금융의 실시간 데이터를 크롤링하여 정확한 수익률과 시장 지수를 제공한다.

![main_screen](https://github.com/YangNaang2/Family_Stock_Portfolio_Manager/blob/main/images/%EB%A9%94%EC%9D%B8%ED%99%94%EB%A9%B4.png) 

### 📂 프로젝트 구조
```text
주식관리/
├── main.py            # 프로그램의 메인 실행 파일 및 GUI 로직
├── scraper.py         # 네이버 금융 실시간 주가 및 시장 지수 크롤링 엔진
├── data_manager.py    # 주식 데이터(JSON) 로드 및 저장 관리 모듈
├── .gitignore         # 깃허브 업로드 시 제외할 파일 설정 (보안용)
└── family_stocks.json # 가족 주식 데이터가 저장되는 파일 (자동 생성)
```

## ✨ 주요 기능

### 📊 실시간 시장 대시보드
- 상단 인덱스 바를 통해 **KOSPI, KOSDAQ 지수** 및 **원/달러 환율**과 전일 대비 등락률을 실시간으로 확인.

### 👨‍👩‍👧‍👦 가족별 포트폴리오 관리
- 드롭다운 메뉴를 통해 아빠, 엄마, 나, 동생 등 가족 구성원별 독립된 자산 현황을 조회.

### 📈 스마트 주식 관리 (CRUD)
- **추가**: 신규 종목 등록 및 기존 보유 종목의 추가 매수(물타기/불타기) 기능을 지원하며, 가중평균 평단가를 자동 계산.
  <p align="left">
    <img src="https://github.com/YangNaang2/Family_Stock_Portfolio_Manager/raw/main/images/%EC%8B%A0%EA%B7%9C%EC%A3%BC%EC%8B%9D%EC%9E%85%EB%A0%A5.png" width="45%" />
    <img src="https://github.com/YangNaang2/Family_Stock_Portfolio_Manager/raw/main/images/%EA%B8%B0%EC%A1%B4%EC%A3%BC%EC%8B%9D%EC%B6%94%EA%B0%80.png" width="45%" />
  </p>

- **수정/삭제**: 잘못 입력된 평단가나 수량을 직관적인 팝업창에서 즉시 수정하거나 삭제할 수 있다.
  <p align="left">
    <img src="https://github.com/YangNaang2/Family_Stock_Portfolio_Manager/raw/main/images/%EC%A3%BC%EC%8B%9D%EC%88%98%EC%A0%95.png" width="45%" />
  </p>

### 🍰 자산 비중 시각화
- Matplotlib 기반의 파이 차트를 통해 구성원별 포트폴리오 비중을 한눈에 파악.

### 💾 데이터 영구 저장
- 모든 데이터는 `family_stocks.json` 파일에 안전하게 기록되어 프로그램을 재시작해도 유지.

### 🕒 실시간 동기화
- 1초 단위 실시간 시계와 새로고침 기능을 통해 최신 금융 정보를 유지.

## 🚀 시작하기

### 필수 라이브러리 설치 후 실행
터미널에서 아래 명령어를 입력하여 설치 후 실행.

```bash
pip install PyQt5 requests beautifulsoup4 matplotlib
python main.py
```
