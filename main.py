import sys
import os

# PyQt5 플러그인 경로 설정
import PyQt5
plugin_path = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = plugin_path

from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QComboBox, QTableWidget, QTableWidgetItem, 
                             QPushButton, QLabel, QInputDialog, QMessageBox, QHeaderView,
                             QDialog, QFormLayout, QLineEdit, QSpinBox, QDialogButtonBox)
from PyQt5.QtCore import Qt, QTimer, QDateTime

# 🌟 Matplotlib 라이브러리 추가
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

# 한글 폰트 설정 (윈도우의 맑은 고딕)
plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

from data_manager import load_data, save_data
from scraper import get_current_price, get_market_info # 🌟 get_market_info 추가

class AddStockDialog(QDialog):
    """🌟 하나의 창에서 주식 정보를 한 번에 입력받는 맞춤형 팝업창"""
    def __init__(self, existing_stocks, parent=None):
        super().__init__(parent)
        self.setWindowTitle("주식 추가 및 매수 기록")
        self.resize(350, 250)
        self.existing_stocks = existing_stocks
        
        layout = QFormLayout(self)
        
        # 1. 작업 종류 선택 (드롭다운)
        self.combo = QComboBox()
        self.combo.addItem("➕ 새로운 주식 등록 (직접 입력)")
        for stock in self.existing_stocks:
            self.combo.addItem(f"{stock['name']} ({stock['code']})")
        layout.addRow("작업 선택:", self.combo)
        
        # 2. 종목명
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("예: 삼성전자")
        layout.addRow("종목명:", self.name_input)
        
        # 3. 종목코드
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("예: 005930")
        layout.addRow("종목코드:", self.code_input)
        
        # 4. 평단가
        self.price_input = QSpinBox()
        self.price_input.setRange(1, 100000000) # 최대 1억 원까지 입력 가능
        self.price_input.setSingleStep(100)
        layout.addRow("매수 단가(원):", self.price_input)
        
        # 5. 수량
        self.qty_input = QSpinBox()
        self.qty_input.setRange(1, 1000000)
        layout.addRow("수량(주):", self.qty_input)
        
        # 6. 확인 / 취소 버튼
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addRow(self.buttons)
        
        # 드롭다운 메뉴를 바꿀 때마다 실행될 이벤트 연결
        self.combo.currentIndexChanged.connect(self.on_combo_changed)
        
    def on_combo_changed(self, index):
        """기존 주식을 선택하면 이름과 코드를 자동으로 채우고 잠금 처리"""
        if index == 0: # 신규 등록 선택 시
            self.name_input.clear()
            self.code_input.clear()
            self.name_input.setEnabled(True)
            self.code_input.setEnabled(True)
        else: # 기존 주식 선택 시
            stock = self.existing_stocks[index - 1]
            self.name_input.setText(stock['name'])
            self.code_input.setText(stock['code'])
            self.name_input.setEnabled(False) # 수정 못하게 잠금
            self.code_input.setEnabled(False) # 수정 못하게 잠금
            
    def get_data(self):
        """입력된 데이터를 딕셔너리로 반환"""
        return {
            "is_new": self.combo.currentIndex() == 0,
            "name": self.name_input.text().strip(),
            "code": self.code_input.text().strip(),
            "price": self.price_input.value(),
            "quantity": self.qty_input.value()
        }
    
class EditStockDialog(QDialog):
    """🌟 잘못 입력한 평단가와 수량을 수정하는 전용 팝업창"""
    def __init__(self, stock_info, parent=None):
        super().__init__(parent)
        self.setWindowTitle("주식 정보 수정")
        self.resize(300, 150)
        
        layout = QFormLayout(self)
        
        # 1. 종목명과 코드는 확인용으로 텍스트만 보여줌 (수정 불가)
        layout.addRow("대상 종목:", QLabel(f"<b>{stock_info['name']}</b> ({stock_info['code']})"))
        
        # 2. 평단가 수정 칸 (기존 값이 기본으로 채워져 있음)
        self.price_input = QSpinBox()
        self.price_input.setRange(1, 100000000)
        self.price_input.setSingleStep(100)
        self.price_input.setValue(stock_info['purchase_price'])
        layout.addRow("수정할 단가(원):", self.price_input)
        
        # 3. 수량 수정 칸 (기존 값이 기본으로 채워져 있음)
        self.qty_input = QSpinBox()
        self.qty_input.setRange(1, 1000000)
        self.qty_input.setValue(stock_info['quantity'])
        layout.addRow("수정할 수량(주):", self.qty_input)
        
        # 4. 버튼
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addRow(self.buttons)
        
    def get_data(self):
        """수정된 단가와 수량 반환"""
        return {
            "price": self.price_input.value(),
            "quantity": self.qty_input.value()
        }

class StockApp(QWidget):
    def __init__(self):
        super().__init__()
        self.data = load_data()
        self.initUI()

    def initUI(self):
        self.setWindowTitle('가족 주식 포트폴리오 관리기 Pro')
        self.resize(1100, 600) # 차트가 들어가므로 창 크기를 넓힘

        main_layout = QVBoxLayout()

        # [0] 🌟 상단 마켓 인덱스 바 (코스피, 코스닥, 환율)
        self.market_label = QLabel("시장 지수 불러오는 중...")
        self.market_label.setStyleSheet("font-size: 13px; font-weight: bold; background-color: #f1f2f6; padding: 5px;")
        main_layout.addWidget(self.market_label)

        # [1] 컨트롤 레이아웃
        top_layout = QHBoxLayout()
        self.member_combo = QComboBox()
        self.member_combo.addItems(self.data.keys())
        self.member_combo.currentIndexChanged.connect(self.refresh_table)
        
        top_layout.addWidget(QLabel("👨‍👩‍👧‍👦 가족 선택:"))
        top_layout.addWidget(self.member_combo)

        btn_add = QPushButton("➕ 주식 추가")
        btn_add.clicked.connect(self.add_stock)
        top_layout.addWidget(btn_add)

        # 🌟 새롭게 추가된 주식 수정 버튼
        btn_edit = QPushButton("✏️ 주식 수정")
        btn_edit.clicked.connect(self.edit_stock)
        top_layout.addWidget(btn_edit)

        btn_delete = QPushButton("❌ 주식 삭제")
        btn_delete.clicked.connect(self.delete_stock)
        top_layout.addWidget(btn_delete)
        
        btn_refresh = QPushButton("🔄 새로고침 (차트/지수)")
        btn_refresh.clicked.connect(self.refresh_table)
        top_layout.addWidget(btn_refresh)

        main_layout.addLayout(top_layout)

        # 🌟 [2] 중앙 데이터 레이아웃 (표와 차트를 가로로 배치)
        center_layout = QHBoxLayout()

        # 왼쪽: 주식 데이터 표
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["종목명(코드)", "매수단가", "현재가", "수량", "평가손익", "수익률"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents) 
        for i in range(1, 6):
            self.table.horizontalHeader().setSectionResizeMode(i, QHeaderView.Stretch)
        center_layout.addWidget(self.table, stretch=6) # 표 비율 6

        # 오른쪽: 파이 차트 (Matplotlib 캔버스)
        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        center_layout.addWidget(self.canvas, stretch=4) # 차트 비율 4

        main_layout.addLayout(center_layout)

        # [3] 하단 요약 정보
        self.summary_label = QLabel("불러오는 중...")
        self.summary_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #2c3e50; padding: 5px;")
        self.summary_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.summary_label)

        # [4] 최하단 상태바 (시계)
        status_layout = QHBoxLayout()
        self.time_label = QLabel()
        self.time_label.setStyleSheet("font-size: 12px; color: #7f8c8d; font-weight: bold;")
        self.time_label.setAlignment(Qt.AlignRight)
        
        status_layout.addStretch(1)
        status_layout.addWidget(self.time_label)
        main_layout.addLayout(status_layout)

        self.setLayout(main_layout)
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_clock)
        self.timer.start(1000)
        
        self.update_clock()
        self.refresh_table()

    def update_clock(self):
        current_datetime = QDateTime.currentDateTime().toString("yyyy년 MM월 dd일 hh:mm:ss")
        self.time_label.setText(f"🕒 현재 시간: {current_datetime}")

    def update_market_info(self):
        """시장 지수 라벨 업데이트 (등락률 포함)"""
        m = get_market_info()
        
        # 지수별 색상 코드 (상승은 빨강, 하락은 파랑)
        k_color = "red" if "+" in m['KOSPI_RATE'] else "blue"
        kd_color = "red" if "+" in m['KOSDAQ_RATE'] else "blue"
        
        text = (f"<b>📊 KOSPI:</b> {m['KOSPI']} (<span style='color:{k_color}'>{m['KOSPI_RATE']}</span>)  |  "
                f"<b>📈 KOSDAQ:</b> {m['KOSDAQ']} (<span style='color:{kd_color}'>{m['KOSDAQ_RATE']}</span>)  |  "
                f"<b>💵 환율:</b> {m['USD']}원")
        self.market_label.setText(text)

    def refresh_table(self):
        """선택된 가족의 주식 데이터를 표와 차트에 업데이트 (등락률 및 번호 라벨 반영)"""
        member = self.member_combo.currentText()
        if not member: return
        
        # 상단 시장 지수(코스피/코스닥/환율) 업데이트
        self.update_market_info()

        stocks = self.data.get(member, [])
        self.table.setRowCount(len(stocks))

        total_buy = 0
        total_current = 0
        
        chart_labels = []
        chart_sizes = []

        for row, stock in enumerate(stocks):
            # 🌟 스크래퍼로부터 {price, rate} 딕셔너리 수신
            stock_info = get_current_price(stock['code'])
            
            if stock_info:
                curr_price = stock_info['price']
                day_rate = stock_info['rate'] # 예: "+2.15%" 또는 "-1.02%"
            else:
                curr_price = stock['purchase_price']
                day_rate = "0%"

            buy_val = stock['purchase_price'] * stock['quantity']
            curr_val = curr_price * stock['quantity']
            profit = curr_val - buy_val
            roi = (profit / buy_val * 100) if buy_val > 0 else 0

            total_buy += buy_val
            total_current += curr_val
            
            # 🌟 차트용 데이터: 이름을 "1번", "2번"으로 간략화
            if curr_val > 0:
                chart_labels.append(f"{row + 1}번")
                chart_sizes.append(curr_val)

            # [열 0] 종목명 (코드)
            self.table.setItem(row, 0, QTableWidgetItem(f"{stock['name']} ({stock['code']})"))
            
            # [열 1] 매수단가
            self.table.setItem(row, 1, QTableWidgetItem(f"{stock['purchase_price']:,}원"))
            
            # [열 2] 현재가 (등락률 포함 및 색상 적용)
            price_item = QTableWidgetItem(f"{curr_price:,}원 ({day_rate})")
            if "+" in day_rate:
                price_item.setForeground(Qt.red)
            elif "-" in day_rate:
                price_item.setForeground(Qt.blue)
            self.table.setItem(row, 2, price_item)
            
            # [열 3] 수량
            self.table.setItem(row, 3, QTableWidgetItem(f"{stock['quantity']}주"))
            
            # [열 4, 5] 평가손익 및 수익률 (색상 적용)
            profit_item = QTableWidgetItem(f"{profit:,}원")
            roi_item = QTableWidgetItem(f"{roi:.2f}%")
            
            if profit > 0:
                profit_item.setForeground(Qt.red)
                roi_item.setForeground(Qt.red)
            elif profit < 0:
                profit_item.setForeground(Qt.blue)
                roi_item.setForeground(Qt.blue)
                
            self.table.setItem(row, 4, profit_item)
            self.table.setItem(row, 5, roi_item)

        # 🌟 파이 차트 업데이트
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        if chart_sizes:
            ax.pie(chart_sizes, labels=chart_labels, autopct='%1.1f%%', startangle=90, colors=plt.cm.Pastel1.colors)
            ax.set_title(f"[{member}] 포트폴리오 비중", fontweight="bold")
            self.figure.tight_layout() # 차트 여백 최적화
        else:
            ax.text(0.5, 0.5, '데이터 없음', ha='center', va='center')
        self.canvas.draw()

        # 🌟 하단 총 자산 요약 업데이트 (색상 포함)
        total_profit = total_current - total_buy
        total_roi = (total_profit / total_buy * 100) if total_buy > 0 else 0
        
        profit_color = "black"
        if total_profit > 0: profit_color = "red"
        elif total_profit < 0: profit_color = "blue"

        summary_text = (f"💰 총 매수: {total_buy:,}원  |  ➡️ 총 평가: {total_current:,}원  |  "
                        f"📈 총 손익: <span style='color:{profit_color}'>{total_profit:,}원 ({total_roi:.2f}%)</span>")
        self.summary_label.setText(summary_text)
    
    def add_stock(self):
        """새 주식 추가 또는 기존 주식 추가 매수 (통합 다이얼로그 버전)"""
        member = self.member_combo.currentText()
        if not member: return
        
        existing_stocks = self.data.get(member, [])
        
        # 새로 만든 맞춤형 창 띄우기
        dialog = AddStockDialog(existing_stocks, self)
        
        # 사용자가 창에서 '확인(Ok)'을 눌렀을 때만 데이터 처리
        if dialog.exec_():
            result = dialog.get_data()
            
            # A. 새로운 주식 등록
            if result['is_new']:
                if not result['name'] or not result['code']:
                    QMessageBox.warning(self, "입력 오류", "종목명과 종목코드를 모두 입력해주세요.")
                    return
                
                self.data[member].append({
                    "name": result['name'], 
                    "code": result['code'], 
                    "purchase_price": result['price'], 
                    "quantity": result['quantity']
                })
                msg = f"✅ [{result['name']}] 주식이 새로 등록되었습니다."
                
            # B. 기존 주식 추가 매수 (물타기/불타기)
            else:
                for stock in existing_stocks:
                    if stock['name'] == result['name'] and stock['code'] == result['code']:
                        old_total = stock['purchase_price'] * stock['quantity']
                        new_total = result['price'] * result['quantity']
                        
                        stock['quantity'] += result['quantity']
                        stock['purchase_price'] = int((old_total + new_total) / stock['quantity'])
                        
                        msg = f"✅ [{result['name']}] {result['quantity']}주 추가 매수 완료!\n(새로운 평단가: {stock['purchase_price']:,}원 적용됨)"
                        break

            # 데이터 저장 및 표 새로고침
            save_data(self.data)
            self.refresh_table()
            QMessageBox.information(self, "성공", msg)


    def delete_stock(self):
        member = self.member_combo.currentText()
        if not member: return
        
        current_row = self.table.currentRow()
        if current_row < 0:
            QMessageBox.warning(self, "경고", "삭제할 주식을 표에서 먼저 클릭(선택)해주세요.")
            return

        stock_name = self.data[member][current_row]['name']
        reply = QMessageBox.question(self, '삭제 확인', f"'{stock_name}' 주식을 정말 삭제하시겠습니까?", QMessageBox.Yes | QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            del self.data[member][current_row]
            save_data(self.data)
            self.refresh_table()
            
    def edit_stock(self):
        """🌟 표에서 선택된 주식의 정보를 수정하는 기능"""
        member = self.member_combo.currentText()
        if not member: return
        
        current_row = self.table.currentRow()
        
        if current_row < 0:
            QMessageBox.warning(self, "경고", "수정할 주식을 표에서 먼저 클릭(선택)해주세요.")
            return

        # 표에서 선택한 줄의 주식 데이터 원본 가져오기
        stock = self.data[member][current_row]
        
        # 수정 창 띄우기 (기존 데이터를 넘겨줌)
        dialog = EditStockDialog(stock, self)
        
        if dialog.exec_():
            new_data = dialog.get_data()
            
            # 원본 데이터 덮어쓰기
            stock['purchase_price'] = new_data['price']
            stock['quantity'] = new_data['quantity']
            
            # 저장 및 새로고침
            save_data(self.data)
            self.refresh_table()
            QMessageBox.information(self, "성공", f"[{stock['name']}] 정보가 올바르게 수정되었습니다.")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = StockApp()
    ex.show()
    sys.exit(app.exec_())