"""가족 포트폴리오: 시세 조회를 UI 스레드에서 분리한 PyQt5 앱."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

from PyQt5.QtCore import Qt, QDate, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QShortcut, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)
from matplotlib import font_manager, rcParams
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from data_manager import DataError, ensure_portfolio_data, load_data, save_data
from portfolio import Holding, _positive_decimal, build_snapshot, export_csv, validate_stock_input
from trading import record_buy, record_sell, record_dividend, correct_holding, remove_holding, summarize_activity
from analytics import add_snapshot, set_targets, set_alert, remove_alert, check_alerts, rename_member, remove_member
from feature_ui import TradeDialog, ActivityDialog, TargetsDialog, AlertDialog, HistoryDialog
from scraper import get_current_price, get_market_info

COLORS = ["#287d70", "#77afa2", "#d3b77d", "#8c9aba", "#c8d7cd", "#c6c1b6"]
UP, DOWN, MUTED = "#bd5146", "#326baf", "#78827f"


def configure_fonts():
    available = {font.name for font in font_manager.fontManager.ttflist}
    for name in ("Malgun Gothic", "AppleGothic", "Noto Sans CJK JP", "NanumGothic", "DejaVu Sans"):
        if name in available:
            rcParams["font.family"] = name
            break
    rcParams["axes.unicode_minus"] = False


def money(value, decimals=0):
    return "—" if value is None else f"{value:,.{decimals}f}원"


def signed(value, suffix="원"):
    if value is None:
        return "—"
    return f"{value:+,.2f}%" if suffix == "%" else f"{value:+,.0f}원"


class QuoteWorker(QThread):
    completed = pyqtSignal(object, object)

    def __init__(self, codes, parent=None):
        super().__init__(parent)
        self.codes = tuple(sorted(set(codes)))

    def run(self):
        quotes, market = {}, {}
        # 가족별 중복 보유 종목도 한 번만 조회한다.
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {pool.submit(get_current_price, code): code for code in self.codes}
            jobs[pool.submit(get_market_info)] = None
            for future in as_completed(jobs):
                if self.isInterruptionRequested():
                    for pending in jobs:
                        pending.cancel()
                    break
                code = jobs[future]
                try:
                    result = future.result()
                except Exception:
                    # 공급자 예외가 UI까지 전파되는 것을 막는 스레드 경계.
                    result = None
                if code is None:
                    market = result or {}
                elif result:
                    quotes[code] = dict(result, stale=False, updated_at=datetime.now().isoformat(timespec="seconds"))
                else:
                    quotes[code] = None
        if not self.isInterruptionRequested():
            self.completed.emit(quotes, market)


class NumberItem(QTableWidgetItem):
    def __init__(self, text, value=None):
        super().__init__(text)
        self.sort_value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, NumberItem):
            if self.sort_value is None:
                return other.sort_value is not None
            return other.sort_value is not None and self.sort_value < other.sort_value
        return super().__lt__(other)


class StockDialog(QDialog):
    def __init__(self, stocks, parent=None, stock=None):
        super().__init__(parent)
        self.stocks = stocks
        self.is_edit = stock is not None
        self.setWindowTitle("보유 정보 수정" if stock else "주식 추가 · 추가 매수")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.combo = QComboBox()
        self.combo.addItem("새 종목 입력")
        for holding in stocks:
            self.combo.addItem(f"{holding['name']} ({holding['code']})")
        if not stock:
            form.addRow("매수 대상", self.combo)
        self.name_input, self.code_input, self.price_input = QLineEdit(), QLineEdit(), QLineEdit()
        self.name_input.setPlaceholderText("예: 삼성전자")
        self.code_input.setPlaceholderText("6자리 종목코드 · 예: 005930")
        self.price_input.setPlaceholderText("원 단위 · 소수 입력 가능")
        self.qty_input = QLineEdit("1")
        self.qty_input.setPlaceholderText("1 이상의 정수")
        for title, widget in (("종목명", self.name_input), ("종목코드", self.code_input),
                              ("평균 매수가" if stock else "매수 단가", self.price_input), ("수량", self.qty_input)):
            form.addRow(title, widget)
        self.fee_input = QLineEdit("0")
        self.date_input = QDateEdit(QDate.currentDate())
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("yyyy-MM-dd")
        self.date_input.setMaximumDate(QDate.currentDate())
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("선택 입력")
        if not stock:
            form.addRow("매수 수수료(원)", self.fee_input)
            form.addRow("거래일", self.date_input)
        form.addRow("수정 사유" if stock else "메모", self.note_input)
        hint = QLabel("수정은 보유 원가를 다시 설정합니다. 추가 매수는 ‘주식 추가’를 이용하세요." if stock else
                      "같은 종목코드를 입력하면 기존 보유분에 합산합니다.")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.combo.currentIndexChanged.connect(self.select_stock)
        if stock:
            self.name_input.setText(stock["name"])
            self.code_input.setText(stock["code"])
            self.name_input.setReadOnly(True)
            self.code_input.setReadOnly(True)
            self.price_input.setText(str(stock["purchase_price"]))
            self.qty_input.setText(str(stock["quantity"]))

    def select_stock(self, index):
        holding = self.stocks[index - 1] if index else None
        self.name_input.setText(holding["name"] if holding else "")
        self.code_input.setText(holding["code"] if holding else "")
        self.name_input.setReadOnly(bool(holding))
        self.code_input.setReadOnly(bool(holding))

    def get_data(self):
        text = self.qty_input.text().strip()
        if not text.isascii() or not text.isdigit():
            raise ValueError("수량은 1 이상의 정수로 입력하세요.")
        result = dict(name=self.name_input.text().strip(), code=self.code_input.text().strip(),
                      price=self.price_input.text().strip(), quantity=int(text))
        if not self.is_edit:
            result.update(fee=self.fee_input.text().strip(), date=self.date_input.date().toString("yyyy-MM-dd"), note=self.note_input.text().strip())
        else:
            result["reason"] = self.note_input.text().strip()
        return result

    def accept(self):
        try:
            values = self.get_data()
            validate_stock_input(**{key: values[key] for key in ("name", "code", "price", "quantity")})
            try:
                fee = Decimal(self.fee_input.text().strip())
            except InvalidOperation:
                raise ValueError("수수료는 0 이상의 숫자여야 합니다.") from None
            if not fee.is_finite() or fee < 0:
                raise ValueError("수수료는 0 이상의 숫자여야 합니다.")
        except (ValueError, InvalidOperation) as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))
            return
        super().accept()


class StockApp(QWidget):
    def __init__(self, data_file=None, *, demo=False, auto_refresh=True):
        super().__init__()
        configure_fonts()
        self.data_file, self.demo = data_file, demo
        self.data = demo_portfolio() if demo else load_data(data_file)
        self.quotes = demo_quotes() if demo else {}
        self.worker = None
        self._closing = self._pending_refresh = False
        self._active_alert_ids = set()
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self.auto_refresh_quotes)
        self.init_ui()
        self.render()
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.update_clock)
        self.clock.start(1000)
        self.update_clock()
        if demo:
            self.status_label.setText("데모 · 가상 데이터와 예시 시세입니다. 변경 사항은 저장되지 않습니다.")
            self.refresh_button.setEnabled(False)
        elif auto_refresh:
            self.refresh_quotes()

    def init_ui(self):
        self.setWindowTitle("가족 포트폴리오 · Family Folio")
        self.resize(1500, 1010)
        self.setMinimumSize(1100, 740)
        self.setStyleSheet("""
            QWidget { background: #f5f6f2; color: #223c35; font-size: 13px; }
            QLabel { background: transparent; }
            QLabel#eyebrow { color: #287d70; font-size: 11px; font-weight: bold; }
            QLabel#title { font-size: 28px; font-weight: bold; }
            QLabel#muted { color: #78827f; }
            QFrame#card, QFrame#panel { background: white; border: 1px solid #e0e6df; border-radius: 12px; }
            QLabel#cardValue { font-size: 25px; font-weight: bold; }
            QPushButton, QToolButton { background: white; border: 1px solid #d8e0d8; border-radius: 6px; padding: 9px 14px; }
            QPushButton:hover, QToolButton:hover { background: #eaf0e9; }
            QPushButton:disabled { color: #9fa8a3; }
            QPushButton#primary { background: #287d70; color: white; border-color: #287d70; }
            QPushButton#primary:hover { background: #20665b; }
            QLineEdit, QComboBox, QSpinBox { background: white; border: 1px solid #d8e0d8; border-radius: 6px; padding: 8px; }
            QTableWidget { background: white; alternate-background-color: #fafbf8; border: none; gridline-color: #eef1eb; selection-background-color: #dfede6; selection-color: #223c35; }
            QTableWidget::item { padding: 4px; }
            QHeaderView::section { background: #f7f9f5; color: #66756d; border: none; border-bottom: 1px solid #e4e9e1; padding: 10px 4px; font-size: 12px; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(18)
        heading, titles = QHBoxLayout(), QVBoxLayout()
        for text, name in (("FAMILY FOLIO   /   우리 가족의 투자 기록", "eyebrow"),
                           ("함께 보는 우리 가족 자산", "title"), ("흩어진 보유 내역을 한곳에서 살펴보세요.", "muted")):
            label = QLabel(text)
            label.setObjectName(name)
            titles.addWidget(label)
        heading.addLayout(titles)
        heading.addStretch()
        self.refresh_button = self.button("시세 새로고침", self.refresh_quotes)
        heading.addWidget(self.refresh_button)
        root.addLayout(heading)
        self.market_label = QLabel("KOSPI  —        KOSDAQ  —        USD/KRW  —")
        self.market_label.setTextFormat(Qt.PlainText)
        self.market_label.setStyleSheet("background: #eaf0e9; border-radius: 8px; padding: 12px; color: #52675c;")
        root.addWidget(self.market_label)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("포트폴리오"))
        self.member_combo = QComboBox()
        self.member_combo.setMinimumWidth(160)
        self.member_combo.addItem("가족 전체", None)
        for member in self.data:
            self.member_combo.addItem(member, member)
        self.member_combo.currentIndexChanged.connect(self.render)
        controls.addWidget(self.member_combo)
        controls.addWidget(self.button("+ 가족 추가", self.add_member))
        controls.addStretch()
        self.privacy = QCheckBox("금액 숨김")
        self.privacy.setToolTip("대시보드의 금액·수량을 가립니다. 별도 창과 내보내기는 원래 값을 사용합니다.")
        self.privacy.toggled.connect(self.render)
        controls.addWidget(self.privacy)
        controls.addWidget(self.button("CSV 내보내기", self.export_data))
        add = self.button("+ 주식 추가", self.add_stock)
        add.setObjectName("primary")
        controls.addWidget(add)
        root.addLayout(controls)
        tools = QHBoxLayout()
        for title, action in (("거래 내역", self.show_activity), ("목표 비중", self.edit_targets),
                              ("자산 변화", self.show_history), ("오늘 자산 기록", self.capture_snapshot)):
            tools.addWidget(self.button(title, action))
        alert_button = QToolButton()
        alert_button.setText("가격 알림")
        alert_button.setPopupMode(QToolButton.InstantPopup)
        alert_menu = QMenu(alert_button)
        alert_menu.addAction("새 가격 알림", self.add_price_alert)
        alert_menu.addAction("알림 삭제", self.delete_price_alert)
        alert_button.setMenu(alert_menu)
        tools.addWidget(alert_button)
        data_button = QToolButton()
        data_button.setText("데이터 · 가족")
        data_button.setPopupMode(QToolButton.InstantPopup)
        data_menu = QMenu(data_button)
        for title, action in (("전체 JSON 백업", self.backup_data), ("JSON 가져오기 / 복구", self.import_data),
                              ("선택 가족 이름 변경", self.rename_selected_member), ("빈 가족 삭제", self.delete_selected_member)):
            data_menu.addAction(title, action)
        data_button.setMenu(data_menu)
        tools.addWidget(data_button)
        tools.addStretch()
        tools.addWidget(QLabel("자동 조회"))
        self.interval_combo = QComboBox()
        for title, seconds in (("사용 안 함", 0), ("1분마다", 60), ("5분마다", 300), ("15분마다", 900)):
            self.interval_combo.addItem(title, seconds)
        self.interval_combo.currentIndexChanged.connect(self.change_interval)
        self.interval_combo.setEnabled(not self.demo)
        tools.addWidget(self.interval_combo)
        root.addLayout(tools)
        cards = QHBoxLayout()
        self.card_values, self.card_notes = [], []
        for title in ("총 매수금액", "평가금액", "평가손익", "수익률"):
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(18, 15, 18, 15)
            label, value, note = QLabel(title), QLabel("—"), QLabel("")
            label.setObjectName("muted")
            value.setObjectName("cardValue")
            note.setObjectName("muted")
            for widget in (label, value, note):
                layout.addWidget(widget)
            cards.addWidget(card)
            self.card_values.append(value)
            self.card_notes.append(note)
        root.addLayout(cards)
        self.activity_label = QLabel()
        self.activity_label.setTextFormat(Qt.PlainText)
        self.activity_label.setStyleSheet("color: #52675c; padding: 2px;")
        root.addWidget(self.activity_label)
        self.alert_label = QLabel()
        self.alert_label.setTextFormat(Qt.PlainText)
        self.alert_label.setWordWrap(True)
        self.alert_label.setStyleSheet("color: #876729; background: #fff4d9; padding: 10px; border-radius: 6px;")
        self.alert_label.hide()
        root.addWidget(self.alert_label)
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setStyleSheet("color: #876729; padding: 2px;")
        root.addWidget(self.notice_label)
        center = QHBoxLayout()
        panel = QFrame()
        panel.setObjectName("panel")
        table_layout = QVBoxLayout(panel)
        table_layout.setContentsMargins(16, 14, 16, 12)
        table_header = QHBoxLayout()
        self.holdings_label = QLabel("보유 종목")
        self.holdings_label.setStyleSheet("font-weight: bold; font-size: 16px;")
        table_header.addWidget(self.holdings_label)
        table_header.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("종목명 · 코드 · 가족 검색")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(250)
        self.search.textChanged.connect(self.render)
        table_header.addWidget(self.search)
        table_layout.addLayout(table_header)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(["가족", "종목명 / 코드", "수량", "평균 매수가", "현재가", "평가금액", "평가손익", "수익률", "시세 상태"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(54)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)
        self.table.itemDoubleClicked.connect(self.edit_stock)
        table_layout.addWidget(self.table)
        actions = QHBoxLayout()
        actions.addWidget(self.button("매도 기록", self.sell_stock))
        actions.addWidget(self.button("배당 기록", self.add_dividend))
        actions.addWidget(self.button("선택 항목 수정", self.edit_stock))
        actions.addWidget(self.button("선택 항목 삭제", self.delete_stock))
        actions.addStretch()
        table_layout.addLayout(actions)
        hint = QLabel("검색은 표에만 적용됩니다. 요약 · 비중 · CSV는 선택한 가족 전체 기준입니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        table_layout.addWidget(hint)
        center.addWidget(panel, 3)
        chart_panel = QFrame()
        chart_panel.setObjectName("panel")
        chart_panel.setMinimumWidth(275)
        chart_layout = QVBoxLayout(chart_panel)
        chart_layout.setContentsMargins(18, 18, 18, 18)
        chart_title = QLabel("종목별 자산 비중")
        chart_title.setStyleSheet("font-weight: bold; font-size: 16px;")
        chart_layout.addWidget(chart_title)
        self.chart_note = QLabel("평가금액 기준")
        self.chart_note.setObjectName("muted")
        self.chart_note.setWordWrap(True)
        chart_layout.addWidget(self.chart_note)
        self.figure = Figure(figsize=(3, 3), facecolor="white")
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setMinimumHeight(210)
        chart_layout.addWidget(self.canvas, 1)
        self.legend_label = QLabel()
        self.legend_label.setTextFormat(Qt.PlainText)
        self.legend_label.setWordWrap(True)
        self.legend_label.setStyleSheet("color: #52675c;")
        chart_layout.addWidget(self.legend_label)
        center.addWidget(chart_panel, 1)
        root.addLayout(center, 1)
        footer = QHBoxLayout()
        self.status_label, self.time_label = QLabel("시세를 새로고침해 주세요."), QLabel()
        self.status_label.setObjectName("muted")
        self.status_label.setTextFormat(Qt.PlainText)
        self.time_label.setObjectName("muted")
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.time_label)
        root.addLayout(footer)
        self.shortcuts = []
        for sequence, action in (("Ctrl+R", self.refresh_quotes), ("Ctrl+F", self.search.setFocus),
                                 ("Ctrl+N", self.add_stock), ("Ctrl+E", self.export_data), ("Ctrl+T", self.show_activity)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(action)
            self.shortcuts.append(shortcut)

    @staticmethod
    def button(text, action):
        button = QPushButton(text)
        button.clicked.connect(action)
        return button

    def update_clock(self):
        self.time_label.setText(datetime.now().strftime("%Y.%m.%d  %H:%M:%S"))

    def snapshot(self):
        return build_snapshot(self.data, self.quotes, self.member_combo.currentData())

    def render(self, *_):
        snapshot, selected = self.snapshot(), self.selected_key()
        partial = bool(snapshot.missing_count)
        known = any(h.current_price is not None for h in snapshot.holdings)
        display_total = snapshot.market_value if known or not snapshot.holdings else None
        display_profit = snapshot.profit if known or not snapshot.holdings else None
        private = self.privacy.isChecked()
        for widget, value in zip(self.card_values, [money(snapshot.total_cost), money(display_total), signed(display_profit), signed(snapshot.roi, "%")]):
            widget.setText("••••••" if private else value)
        color = UP if snapshot.profit > 0 else DOWN if snapshot.profit < 0 else "#223c35"
        for widget in self.card_values[2:]:
            widget.setStyleSheet(f"color: {color};")
        for note, text in zip(self.card_notes, [f"{len(snapshot.holdings)}개 보유 내역", "일부 종목 합계" if partial else "보유 수량 × 조회 시세", "조회된 종목 원가 기준" if partial else "매수 원가 대비", "미실현 · 매도 비용 미포함"]):
            note.setText(text)
        activity = summarize_activity(self.data, self.member_combo.currentData())
        self.activity_label.setText("실현손익 · 순배당 금액 숨김" if private else
                                    f"누적 실현손익  {signed(activity['realized_profit'])}     순배당  {money(activity['dividends'])}     거래 원장에 기록된 내역 기준")
        messages = []
        if partial:
            messages.append(f"시세 미확인 {snapshot.missing_count}건 · 해당 종목은 평가금액·손익·수익률·비중에서 제외됩니다.")
        if snapshot.stale_count:
            messages.append(f"이전 시세 {snapshot.stale_count}건 포함 · 현재 가격과 다를 수 있습니다.")
        if not snapshot.holdings:
            messages.append("아직 보유 종목이 없습니다. ‘주식 추가’로 첫 기록을 만들어 보세요.")
        self.notice_label.setText("  ".join(messages))
        self.notice_label.setVisible(bool(messages))
        query = self.search.text().strip().casefold()
        holdings = [h for h in snapshot.holdings if query in f"{h.member} {h.name} {h.code}".casefold()]
        self.holdings_label.setText(f"보유 종목  {len(holdings)}")
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(holdings))
        self.table.clearSelection()
        for row, holding in enumerate(holdings):
            state = "미확인" if holding.current_price is None else "이전 시세" if holding.stale else "조회 완료"
            cells = [QTableWidgetItem(holding.member), QTableWidgetItem(f"{holding.name}\n{holding.code}"),
                     NumberItem(f"{holding.quantity:,}", holding.quantity), NumberItem(money(holding.purchase_price, 2), holding.purchase_price),
                     NumberItem(money(holding.current_price), holding.current_price), NumberItem(money(holding.market_value), holding.market_value),
                     NumberItem(signed(holding.profit), holding.profit), NumberItem(signed(holding.roi, "%"), holding.roi), QTableWidgetItem(state)]
            for column, item in enumerate(cells):
                item.setData(Qt.UserRole, (holding.member, holding.code))
                if column in (6, 7) and holding.profit is not None:
                    item.setForeground(QColor(UP if holding.profit > 0 else DOWN if holding.profit < 0 else MUTED))
                if column in (4, 8):
                    tooltip = (f"마지막 성공 조회: {holding.updated_at}\n전일 대비: {self.quotes.get(holding.code, {}).get('rate', '-')}"
                               if holding.updated_at else "성공한 시세 조회가 없습니다.")
                    item.setToolTip(tooltip)
                if private and 2 <= column <= 7:
                    item.setText("••••")
                    item.setToolTip("")
                self.table.setItem(row, column, item)
            if (holding.member, holding.code) == selected:
                self.table.selectRow(row)
        self.table.setSortingEnabled(True)
        self.render_chart(snapshot)
        self.update_alerts(notify=False)

    def render_chart(self, snapshot):
        totals, names = {}, {}
        for holding in snapshot.holdings:
            if holding.market_value is not None:
                totals[holding.code] = totals.get(holding.code, Decimal(0)) + holding.market_value
                names[holding.code] = holding.name
        parts = [(names[code], value) for code, value in sorted(totals.items(), key=lambda item: item[1], reverse=True)]
        if len(parts) > 5:
            parts = parts[:5] + [("기타", sum(value for _, value in parts[5:]))]
        self.figure.clear()
        ax = self.figure.add_axes([0.06, 0.06, 0.88, 0.88])
        total = sum(value for _, value in parts)
        if total:
            ax.pie([float(value) for _, value in parts], colors=COLORS, startangle=90,
                   counterclock=False, wedgeprops=dict(width=0.25, edgecolor="white", linewidth=3))
            ax.text(0, 0.10, str(len(totals)), ha="center", va="center", fontsize=30, color="#223c35", fontweight="bold")
            ax.text(0, -0.22, "보유 종목", ha="center", va="center", fontsize=11, color=MUTED)
            self.legend_label.setText("\n\n".join(f"{index + 1:02d}  {name}   {value / total * 100:.1f}%" for index, (name, value) in enumerate(parts)))
        else:
            ax.text(0.5, 0.5, "시세 조회 후\n비중을 확인하세요" if snapshot.holdings else "첫 종목을 추가하세요", ha="center", va="center", transform=ax.transAxes, fontsize=12, color=MUTED)
            self.legend_label.setText("")
        ax.set_axis_off()
        self.chart_note.setText("평가금액 기준" + (" · 미확인 종목 제외" if snapshot.missing_count else "") + (" · 이전 시세 포함" if snapshot.stale_count else ""))
        self.canvas.draw_idle()

    def selected_key(self):
        row = self.table.currentRow() if hasattr(self, "table") else -1
        item = self.table.item(row, 0) if row >= 0 else None
        return tuple(item.data(Qt.UserRole)) if item and self.table.selectionModel().hasSelection() else None

    def selected_holding(self):
        key = self.selected_key()
        if not key:
            QMessageBox.information(self, "종목 선택", "표에서 대상 종목을 먼저 선택하세요.")
            return None
        return next(h for h in self.snapshot().holdings if (h.member, h.code) == key)

    def sell_stock(self):
        holding = self.selected_holding()
        if holding is None:
            return
        dialog = TradeDialog(holding, kind="SELL", parent=self)
        if dialog.exec_():
            try:
                candidate = record_sell(self.data, holding.member, holding.code, **dialog.get_data())
            except ValueError as exc:
                QMessageBox.warning(self, "매도 기록 확인", str(exc))
                return
            self.persist(candidate)

    def add_dividend(self):
        key = self.selected_key()
        if key:
            holding = next(h for h in self.snapshot().holdings if (h.member, h.code) == key)
        else:
            member = self.member_combo.currentData()
            history = {(row["member"], row["code"]): row for row in self.data.metadata["transactions"]
                       if member is None or row["member"] == member}
            if not history:
                QMessageBox.information(self, "배당 대상 선택", "보유 종목을 선택하세요. 전량 매도한 종목도 거래 이력이 있으면 선택할 수 있습니다.")
                return
            entries = list(history.values())
            labels = [f"{row['member']} · {row['name']} ({row['code']})" for row in entries]
            label, ok = QInputDialog.getItem(self, "배당 대상 선택", "보유 또는 과거 거래 종목", labels, 0, False)
            if not ok:
                return
            row = entries[labels.index(label)]
            holding = Holding(row["member"], row["name"], row["code"], 0, Decimal(0), Decimal(0),
                              None, None, None, None, False, "")
        dialog = TradeDialog(holding, kind="DIVIDEND", parent=self)
        if dialog.exec_():
            try:
                candidate = record_dividend(self.data, holding.member, holding.code, **dialog.get_data())
            except ValueError as exc:
                QMessageBox.warning(self, "배당 기록 확인", str(exc))
                return
            self.persist(candidate)

    def show_activity(self):
        ActivityDialog(self.data, member=self.member_combo.currentData(), parent=self).exec_()

    def edit_targets(self):
        member = self.member_combo.currentData()
        dialog = TargetsDialog(self.data, self.quotes, member=member, parent=self)
        if dialog.exec_():
            try:
                candidate = set_targets(self.data, member, dialog.get_targets())
            except ValueError as exc:
                QMessageBox.warning(self, "목표 비중 확인", str(exc))
                return
            self.persist(candidate)

    def capture_snapshot(self):
        try:
            candidate = add_snapshot(self.data, self.quotes, member=self.member_combo.currentData())
        except ValueError as exc:
            QMessageBox.warning(self, "자산 기록 확인", str(exc))
            return
        if self.persist(candidate):
            self.status_label.setText("오늘의 주식 평가액 기록 완료 · 같은 가족의 같은 날짜 기록은 최신 값으로 갱신됩니다.")

    def show_history(self):
        HistoryDialog(self.data, member=self.member_combo.currentData(), parent=self).exec_()

    def add_price_alert(self):
        dialog = AlertDialog(self.data, parent=self)
        if dialog.exec_():
            try:
                candidate = set_alert(self.data, **dialog.get_data())
            except ValueError as exc:
                QMessageBox.warning(self, "알림 조건 확인", str(exc))
                return
            if self.persist(candidate):
                self.update_alerts(notify=True)

    def delete_price_alert(self):
        alerts = self.data.metadata["alerts"]
        if not alerts:
            QMessageBox.information(self, "가격 알림", "등록된 가격 알림이 없습니다.")
            return
        labels = [f"{index + 1}. {a['code']} · {a['price']}원 {'이상' if a['direction'] == 'above' else '이하'}" for index, a in enumerate(alerts)]
        label, ok = QInputDialog.getItem(self, "가격 알림 삭제", "삭제할 조건", labels, 0, False)
        if ok:
            self.persist(remove_alert(self.data, alerts[labels.index(label)]["id"]))

    def update_alerts(self, notify=False):
        if not hasattr(self, "alert_label"):
            return
        hits = check_alerts(self.data, self.quotes)
        hit_ids = {hit["id"] for hit in hits}
        if notify:
            if hit_ids - self._active_alert_ids and not self.demo:
                QApplication.beep()
            # 조회 불능은 조건 해제로 간주하지 않는다.
            def unknown(alert):
                quote = self.quotes.get(alert["code"]) or {}
                if quote.get("stale"):
                    return True
                try:
                    _positive_decimal(quote.get("price"), "현재가")
                except ValueError:
                    return True
                return False
            unknown_ids = {a["id"] for a in self.data.metadata["alerts"] if unknown(a)}
            self._active_alert_ids = hit_ids | (self._active_alert_ids & unknown_ids)
        self.alert_label.setVisible(bool(hits))
        if self.privacy.isChecked():
            self.alert_label.setText(f"가격 조건에 도달한 알림 {len(hits)}건 · 금액 숨김")
        else:
            self.alert_label.setText("가격 조건 도달  ·  " + "   |   ".join(f"{a['code']} {money(a['current_price'])}" for a in hits))

    def change_interval(self):
        seconds = self.interval_combo.currentData()
        self.auto_timer.stop()
        if seconds and not self.demo:
            self.auto_timer.start(seconds * 1000)

    def auto_refresh_quotes(self):
        if self.worker is None and not self._closing:
            self.refresh_quotes()

    def reload_members(self, selected=None):
        self.member_combo.blockSignals(True)
        self.member_combo.clear()
        self.member_combo.addItem("가족 전체", None)
        for member in self.data:
            self.member_combo.addItem(member, member)
        index = self.member_combo.findData(selected)
        self.member_combo.setCurrentIndex(max(index, 0))
        self.member_combo.blockSignals(False)
        self.render()

    def rename_selected_member(self):
        member = self.member_combo.currentData()
        if member is None:
            QMessageBox.information(self, "가족 선택", "이름을 바꿀 가족을 먼저 선택하세요.")
            return
        name, ok = QInputDialog.getText(self, "가족 이름 변경", "새 이름", text=member)
        if ok and name.strip() != member:
            try:
                candidate = rename_member(self.data, member, name.strip())
            except ValueError as exc:
                QMessageBox.warning(self, "이름 확인", str(exc))
                return
            if self.persist(candidate):
                self.reload_members(name.strip())

    def delete_selected_member(self):
        member = self.member_combo.currentData()
        if member is None:
            QMessageBox.information(self, "가족 선택", "삭제할 빈 가족을 먼저 선택하세요.")
            return
        try:
            candidate = remove_member(self.data, member)
        except ValueError as exc:
            QMessageBox.warning(self, "가족 삭제 확인", str(exc))
            return
        if QMessageBox.question(self, "빈 가족 삭제", f"{member} 구성원을 삭제할까요?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            if self.persist(candidate):
                self.reload_members()

    def backup_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "전체 데이터 백업", f"family_backup_{datetime.now():%Y%m%d_%H%M%S}.json", "JSON (*.json)")
        if not path:
            return
        if Path(path).resolve() == getattr(self.data, "_source_path", None):
            QMessageBox.warning(self, "백업 경로 확인", "현재 사용 중인 데이터 파일과 다른 경로를 선택하세요.")
            return
        try:
            save_data(ensure_portfolio_data(self.data), path)
        except (DataError, OSError) as exc:
            QMessageBox.critical(self, "백업 실패", str(exc))
        else:
            self.status_label.setText(f"전체 데이터 백업 완료 · {Path(path).name}")

    def import_data(self):
        path, _ = QFileDialog.getOpenFileName(self, "JSON 가져오기 / 복구", "", "JSON 또는 백업 (*.json *.bak);;모든 파일 (*)")
        if not path:
            return
        try:
            imported = load_data(path)
        except (DataError, OSError) as exc:
            QMessageBox.critical(self, "가져오기 실패", str(exc))
            return
        message = (f"가족 {len(imported)}명, 보유 내역 {sum(len(rows) for rows in imported.values())}건을 가져옵니다.\n"
                   "현재 데이터 전체를 선택한 파일의 내용으로 바꿀까요? 현재 파일은 .bak으로 보관됩니다.")
        if QMessageBox.question(self, "데이터 교체 확인", message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        candidate = ensure_portfolio_data(self.data)
        candidate.clear()
        candidate.update(deepcopy(imported))
        candidate.metadata = deepcopy(imported.metadata)
        if self.persist(candidate):
            self.quotes = {}
            self._active_alert_ids.clear()
            self.reload_members()
            self.refresh_quotes()

    def refresh_quotes(self):
        if self.demo or self._closing:
            return
        if self.worker is not None:
            self._pending_refresh = True
            return
        codes = {stock["code"] for stocks in self.data.values() for stock in stocks}
        codes.update(alert["code"] for alert in self.data.metadata["alerts"])
        self.quotes = {code: dict(quote, stale=True) for code, quote in self.quotes.items() if code in codes}
        self.render()
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("시세 조회 중…")
        self.status_label.setText("시세 조회 중 · 가족 선택과 보유 내역 수정은 계속할 수 있습니다.")
        self.worker = QuoteWorker(codes, self)
        self.worker.completed.connect(self.apply_quotes)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def apply_quotes(self, results, market):
        for code, quote in results.items():
            if quote:
                self.quotes[code] = quote
        failures = sum(quote is None for quote in results.values())
        self.status_label.setText(f"조회 시도 {datetime.now():%H:%M:%S} · 종목 {len(results) - failures}/{len(results)}건 성공 · 네이버 금융 조회값")
        self.market_label.setText("      ".join(f"{name}  {market.get(key, '—')}  {market.get(key + '_RATE', '')}"
                                              for name, key in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ"), ("USD/KRW", "USD"))))
        self.render()
        self.update_alerts(notify=True)

    def worker_finished(self):
        worker, self.worker = self.worker, None
        if worker:
            worker.deleteLater()
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("시세 새로고침")
        if self._closing:
            self.close()
        elif self._pending_refresh:
            self._pending_refresh = False
            self.refresh_quotes()

    def persist(self, candidate):
        try:
            if not self.demo:
                save_data(candidate, self.data_file)
        except (DataError, OSError) as exc:
            QMessageBox.critical(self, "저장 실패", f"변경 사항을 적용하지 못했습니다.\n{exc}")
            return False
        self.data = candidate
        self.reload_members(self.member_combo.currentData())
        if not self.demo:
            self.status_label.setText("보유 내역 저장 완료 · 기존 파일이 있으면 .bak에 이전 버전을 보관합니다.")
        return True

    def add_member(self):
        name, ok = QInputDialog.getText(self, "가족 추가", "구성원 이름")
        if not ok:
            return
        name = name.strip()
        if not name or name == "*" or name in self.data:
            QMessageBox.warning(self, "이름 확인", "비어 있지 않은 새 이름을 입력하세요.")
            return
        candidate = deepcopy(self.data)
        candidate[name] = []
        if self.persist(candidate):
            self.member_combo.setCurrentIndex(self.member_combo.findData(name))

    def add_stock(self):
        member = self.member_combo.currentData()
        if member is None:
            if not self.data:
                self.add_member()
                return
            member, ok = QInputDialog.getItem(self, "보유 가족 선택", "어느 가족의 매수 내역인가요?", list(self.data), 0, False)
            if not ok:
                return
        dialog = StockDialog(self.data[member], self)
        if dialog.exec_():
            try:
                result = dialog.get_data()
                candidate = record_buy(self.data, member, **result)
            except ValueError as exc:
                QMessageBox.warning(self, "입력 확인", str(exc))
                return
            if self.persist(candidate) and result["code"] not in self.quotes:
                self.refresh_quotes()

    def edit_stock(self, *_):
        key = self.selected_key()
        if not key:
            QMessageBox.information(self, "항목 선택", "수정할 보유 종목을 선택하세요.")
            return
        member, code = key
        holding = next(h for h in self.snapshot().holdings if (h.member, h.code) == key)
        stock = dict(name=holding.name, code=code, purchase_price=str(holding.purchase_price), quantity=holding.quantity)
        dialog = StockDialog([], self, stock=stock)
        if dialog.exec_():
            result = dialog.get_data()
            # 변경 없이 확인한 경우 반복소수 평단가로 정확한 원가를 덮지 않는다.
            if Decimal(result["price"]) == Decimal(str(stock["purchase_price"])) and result["quantity"] == stock["quantity"]:
                return
            try:
                candidate = correct_holding(self.data, member, code, result["price"], result["quantity"], reason=result.get("reason", ""))
            except ValueError as exc:
                QMessageBox.warning(self, "입력 확인", str(exc))
                return
            self.persist(candidate)

    def delete_stock(self):
        key = self.selected_key()
        if not key:
            QMessageBox.information(self, "항목 선택", "삭제할 보유 종목을 선택하세요.")
            return
        member, code = key
        stock = next(stock for stock in self.data[member] if stock["code"] == code)
        if QMessageBox.question(self, "보유 내역 삭제", f"{member}의 {stock['name']} ({code}) 내역을 삭제할까요?\n매도 거래나 실현손익으로 기록되지는 않습니다.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            try:
                candidate = remove_holding(self.data, member, code)
            except ValueError as exc:
                QMessageBox.warning(self, "삭제 기록 확인", str(exc))
                return
            self.persist(candidate)

    def export_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "포트폴리오 내보내기", f"portfolio_{datetime.now():%Y%m%d}.csv", "CSV (*.csv)")
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            try:
                export_csv(self.snapshot(), path)
            except OSError as exc:
                QMessageBox.critical(self, "내보내기 실패", str(exc))
            else:
                self.status_label.setText(f"CSV 내보내기 완료 · {Path(path).name}")

    def closeEvent(self, event):
        self.auto_timer.stop()
        if self.worker is not None:
            self._closing = True
            self.worker.requestInterruption()
            self.status_label.setText("진행 중인 시세 요청을 마무리한 후 자동으로 종료합니다…")
            self.setEnabled(False)
            event.ignore()
        else:
            event.accept()


def demo_data():
    return {
        "아빠": [{"name": "삼성전자", "code": "005930", "purchase_price": 71000, "quantity": 60},
                 {"name": "현대차", "code": "005380", "purchase_price": 220000, "quantity": 12}],
        "엄마": [{"name": "삼성전자", "code": "005930", "purchase_price": 68000, "quantity": 30},
                 {"name": "NAVER", "code": "035420", "purchase_price": 205000, "quantity": 15}],
        "나": [{"name": "SK하이닉스", "code": "000660", "purchase_price": 175000, "quantity": 20}],
    }


def demo_quotes():
    return {code: dict(price=price, rate="—", stale=False, updated_at="데모 예시 시세")
            for code, price in (("005930", 76200), ("005380", 245000), ("035420", 198000), ("000660", 192000))}


def demo_portfolio():
    """Synthetic examples for holdings, trades, targets and history; never save."""
    today = date.today()
    data = ensure_portfolio_data(demo_data())
    data = record_buy(data, "나", "SK하이닉스", "000660", "180000", 2,
                      date=(today - timedelta(days=2)).isoformat(), fee="1000", note="데모 추가 매수")
    data = record_sell(data, "나", "000660", "192000", 2,
                       date=(today - timedelta(days=1)).isoformat(), fee="1000", tax="500", note="데모 부분 매도")
    data = record_dividend(data, "엄마", "005930", "30000", tax="4620", note="데모 배당")
    data = set_targets(data, None, {"005930": "40", "000660": "25", "005380": "20", "035420": "15"})
    for days, factor in ((14, Decimal("0.93")), (7, Decimal("0.96")), (0, Decimal("1"))):
        quotes = {code: dict(quote, price=int(Decimal(quote["price"]) * factor)) for code, quote in demo_quotes().items()}
        data = add_snapshot(data, quotes, date=(today - timedelta(days=days)).isoformat())
    return data


def main():
    parser = argparse.ArgumentParser(description="가족 주식 포트폴리오 관리기")
    parser.add_argument("--demo", action="store_true", help="실제 파일·네트워크를 사용하지 않는 가상 데이터 체험")
    parser.add_argument("--data", type=Path, help="별도로 사용할 포트폴리오 JSON 파일")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    try:
        window = StockApp(args.data, demo=args.demo)
    except (DataError, OSError) as exc:
        QMessageBox.critical(None, "포트폴리오를 열 수 없습니다", f"{exc}\n\n원본 JSON과 .bak 파일을 확인하세요. 기존 파일은 초기화하지 않습니다.")
        return 1
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
