"""Read-only portfolio views and validated forms for ledger and planning tools.

Dialogs return proposed values. Saving a trade, target or alert remains the
caller's responsibility, so cancelling a dialog never changes portfolio data.
"""

import csv
from datetime import date as Date
from decimal import Decimal, InvalidOperation, localcontext
import re

from PyQt5.QtCore import QDate, Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from analytics import allocation_rows, get_targets
from portfolio import MAX_COST, MAX_PRICE, MAX_QUANTITY, build_snapshot
from trading import export_transactions_csv, summarize_activity


TYPE_LABELS = {
    "OPENING": "초기 보유", "BUY": "매수", "SELL": "매도", "DIVIDEND": "배당",
    "ADJUSTMENT": "보유 정정", "REMOVE": "보유 삭제",
}
_NUMBER = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def _label(text=""):
    label = QLabel(str(text))
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    return label


def _decimal(value, label, *, positive=False, maximum=MAX_COST):
    text = str(value).strip()
    if len(text) > 160 or not _NUMBER.fullmatch(text):
        raise ValueError(f"{label}에 쉼표 없이 올바른 숫자를 입력하세요.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label}에 올바른 숫자를 입력하세요.") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(f"{label}은(는) {'0보다 큰' if positive else '0 이상의'} 숫자여야 합니다.")
    if number > maximum or len(number.as_tuple().digits) > 120 or number.as_tuple().exponent < -80:
        raise ValueError(f"{label}의 금액 또는 소수 자릿수가 너무 큽니다.")
    return number


def _quantity(value, maximum):
    text = str(value).strip()
    if len(text) > 30 or not re.fullmatch(r"[0-9]+", text):
        raise ValueError("매도 수량을 정수로 입력하세요.")
    quantity = int(text)
    if not 1 <= quantity <= maximum:
        raise ValueError(f"매도 수량은 1주부터 보유 수량 {maximum:,}주까지 입력할 수 있습니다.")
    return quantity


def _text(number):
    return format(number, "f")


def _amount(number, *, signed=False):
    if number is None:
        return "—"
    return f"{number:+,.2f}원" if signed else f"{number:,.2f}원"


def _stored_number(value):
    if value is None or value == "":
        return None
    return Decimal(str(value))


class _NumberItem(QTableWidgetItem):
    def __init__(self, text, value):
        super().__init__(text)
        self.value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other):
        if isinstance(other, _NumberItem):
            if self.value is None:
                return other.value is not None
            return other.value is not None and self.value < other.value
        return super().__lt__(other)


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(52)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    return table


def _buttons(buttons):
    box = QDialogButtonBox(buttons)
    for button, label in ((QDialogButtonBox.Save, "저장"), (QDialogButtonBox.Cancel, "취소"),
                          (QDialogButtonBox.Close, "닫기")):
        widget = box.button(button)
        if widget is not None:
            widget.setText(label)
    return box


def _save_path(parent, title, filename):
    path, _ = QFileDialog.getSaveFileName(parent, title, filename, "CSV (*.csv)")
    if path and not path.lower().endswith(".csv"):
        path += ".csv"
    return path


class TradeDialog(QDialog):
    """Collect one partial/full sale or a gross cash dividend without saving it."""

    def __init__(self, holding, kind="SELL", parent=None):
        super().__init__(parent)
        if kind not in ("SELL", "DIVIDEND"):
            raise ValueError("지원하지 않는 거래 종류입니다.")
        self.holding, self.kind = holding, kind
        self.setWindowTitle("매도 기록" if kind == "SELL" else "배당 기록")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(f"{holding.member} · {holding.name} ({holding.code})\n보유 {holding.quantity:,}주 · 매수 원금 {_amount(holding.cost_basis)}"))
        form = QFormLayout()
        self.date_input = QDateEdit(QDate.currentDate())
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("yyyy-MM-dd")
        self.date_input.setMaximumDate(QDate.currentDate())
        self.note_input = QLineEdit()
        self.note_input.setMaxLength(1000)
        self.note_input.setPlaceholderText("선택 · 거래 관련 메모")
        self.tax_input = QLineEdit("0")
        if kind == "SELL":
            self.price_input = QLineEdit()
            if holding.current_price is not None:
                self.price_input.setText(_text(holding.current_price))
            self.price_input.setPlaceholderText("실제 체결 단가 · 원")
            self.qty_input = QLineEdit("1")
            self.fee_input = QLineEdit("0")
            form.addRow("매도 단가(원)", self.price_input)
            form.addRow("매도 수량(주)", self.qty_input)
            form.addRow("수수료(원)", self.fee_input)
            inputs = [self.price_input, self.qty_input, self.fee_input, self.tax_input]
        else:
            self.amount_input = QLineEdit()
            self.amount_input.setPlaceholderText("세전 배당 총액 · 원")
            form.addRow("세전 배당금(원)", self.amount_input)
            inputs = [self.amount_input, self.tax_input]
        form.addRow("세금(원)", self.tax_input)
        form.addRow("거래일", self.date_input)
        form.addRow("메모", self.note_input)
        layout.addLayout(form)
        self.estimate_label = _label()
        self.estimate_label.setStyleSheet("background: #eaf0e9; border-radius: 8px; padding: 12px;")
        layout.addWidget(self.estimate_label)
        explanation = (
            "매도 수량에 비례한 매수 원금을 배분해 실현손익을 계산합니다. 체결가와 실제 수수료·세금을 입력하세요."
            if kind == "SELL" else "세후 배당금을 별도 수입으로 기록합니다. 보유 수량과 매수 원금은 바뀌지 않습니다."
        )
        layout.addWidget(_label(explanation))
        self.buttons = _buttons(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for widget in inputs:
            widget.textChanged.connect(self.update_estimate)
        self.update_estimate()

    def get_data(self):
        tax = _decimal(self.tax_input.text(), "세금")
        result = dict(tax=_text(tax), date=self.date_input.date().toString("yyyy-MM-dd"),
                      note=self.note_input.text().strip())
        if self.kind == "SELL":
            price = _decimal(self.price_input.text(), "매도 단가", positive=True, maximum=MAX_PRICE)
            quantity = _quantity(self.qty_input.text(), min(self.holding.quantity, MAX_QUANTITY))
            fee = _decimal(self.fee_input.text(), "수수료")
            result.update(price=_text(price), quantity=quantity, fee=_text(fee))
        else:
            amount = _decimal(self.amount_input.text(), "세전 배당금", positive=True)
            if tax > amount:
                raise ValueError("세금은 세전 배당금을 넘을 수 없습니다.")
            result["amount"] = _text(amount)
        return result

    def update_estimate(self, *_):
        try:
            values = self.get_data()
        except ValueError as exc:
            self.estimate_label.setText(str(exc))
            return
        with localcontext() as context:
            context.prec = 160
            if self.kind == "SELL":
                allocated = self.holding.cost_basis * values["quantity"] / self.holding.quantity
                net = Decimal(values["price"]) * values["quantity"] - Decimal(values["fee"]) - Decimal(values["tax"])
                self.estimate_label.setText(f"예상 실현손익 {_amount(net - allocated, signed=True)}\n배분 매수 원금 {_amount(allocated)} · 세후 매도대금 {_amount(net)}")
            else:
                net = Decimal(values["amount"]) - Decimal(values["tax"])
                self.estimate_label.setText(f"예상 세후 배당금 {_amount(net)}")

    def accept(self):
        try:
            self.get_data()
        except ValueError as exc:
            QMessageBox.warning(self, "입력 확인", str(exc))
            return
        super().accept()


class ActivityDialog(QDialog):
    """Searchable transaction ledger; filters never alter the stored records."""

    def __init__(self, data, member=None, parent=None):
        super().__init__(parent)
        self.data, self.member = data, member
        self.setWindowTitle("거래 · 배당 기록")
        self.resize(1220, 680)
        layout = QVBoxLayout(self)
        totals = summarize_activity(data, member=member)
        self.summary_label = _label(
            f"{'가족 전체' if member is None else member} · 실현손익 {_amount(totals['realized_profit'], signed=True)}"
            f"   /   세후 배당 {_amount(totals['dividends'])}   /   수수료 {_amount(totals['fees'])}   /   세금 {_amount(totals['taxes'])}"
        )
        layout.addWidget(self.summary_label)
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("종목 · 코드 · 가족 · 날짜 · 메모 검색")
        self.search.setClearButtonEnabled(True)
        self.type_combo = QComboBox()
        self.type_combo.addItem("모든 기록", None)
        for kind, label in TYPE_LABELS.items():
            self.type_combo.addItem(label, kind)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.type_combo)
        export_button = QPushButton("CSV 내보내기")
        export_button.clicked.connect(self.export_data)
        controls.addWidget(export_button)
        layout.addLayout(controls)
        self.table = _table(["거래일", "종류", "가족", "종목 / 코드", "수량", "단가(원)", "금액(원)", "수수료(원)", "세금(원)", "배분 원가(원)", "실현손익·순배당(원)", "메모"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 160)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.DescendingOrder)
        layout.addWidget(self.table, 1)
        self.status_label = _label("요약과 CSV는 선택한 가족의 전체 기록 기준입니다. 검색과 종류 필터는 표에만 적용됩니다.")
        layout.addWidget(self.status_label)
        close = _buttons(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self.search.textChanged.connect(self.render)
        self.type_combo.currentIndexChanged.connect(self.render)
        self.render()

    def render(self, *_):
        query, kind = self.search.text().strip().casefold(), self.type_combo.currentData()
        records = getattr(self.data, "metadata", {}).get("transactions", [])
        records = [record for record in records
                   if (self.member is None or record.get("member") == self.member)
                   and (kind is None or record.get("type") == kind)
                   and query in " ".join(str(record.get(key, "")) for key in ("date", "member", "name", "code", "note")).casefold()]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            cells = [QTableWidgetItem(str(record.get("date", ""))),
                     QTableWidgetItem(TYPE_LABELS.get(record.get("type"), str(record.get("type", "")))),
                     QTableWidgetItem(str(record.get("member", ""))),
                     QTableWidgetItem(f"{record.get('name', '')}\n{record.get('code', '')}")]
            for key in ("quantity", "price", "amount", "fee", "tax", "cost_basis", "realized_profit"):
                number = _stored_number(record.get(key))
                text = "—" if number is None else f"{number:,.0f}" if key == "quantity" else f"{number:,.2f}"
                cells.append(_NumberItem(text, number))
            cells.append(QTableWidgetItem(str(record.get("note", ""))))
            for column, item in enumerate(cells):
                item.setData(Qt.UserRole, record.get("id"))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)

    def export_data(self):
        path = _save_path(self, "거래 기록 내보내기", "transactions.csv")
        if not path:
            return
        try:
            export_transactions_csv(self.data, path, member=self.member)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "내보내기 실패", str(exc))
        else:
            self.status_label.setText("선택한 가족의 전체 거래 기록을 CSV로 내보냈습니다.")


class TargetsDialog(QDialog):
    """Edit target percentages without placing orders or modifying holdings."""

    def __init__(self, data, quotes, member=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("목표 비중 설정")
        self.resize(780, 530)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(f"{'가족 전체' if member is None else member}의 목표 비중을 설정하세요. 합계는 100% 이하여야 합니다."))
        layout.addWidget(_label("미배정 비중은 아직 종목에 배분하지 않은 목표입니다. 실제 현금 잔액을 뜻하지 않습니다."))
        self.table = _table(["종목 / 코드", "현재 비중", "목표 비중(%)", "차이(현재 − 목표)"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.target_inputs, self._current_percent = {}, {}
        targets = get_targets(data, member=member)
        rows = allocation_rows(data, quotes, member=member)
        self.table.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            code = entry["code"]
            current = entry.get("current_percent")
            self._current_percent[code] = current
            self.table.setItem(row, 0, QTableWidgetItem(f"{entry.get('name', code)}\n{code}"))
            self.table.setItem(row, 1, QTableWidgetItem("미확인" if current is None else f"{current:.2f}%"))
            target = QLineEdit(str(targets.get(code, "0")))
            target.setAlignment(Qt.AlignRight)
            target.setMaxLength(160)
            target.setAccessibleName(f"{code} 목표 비중")
            self.target_inputs[code] = target
            self.table.setCellWidget(row, 2, target)
            self.table.setItem(row, 3, QTableWidgetItem())
            target.textChanged.connect(self.update_weights)
        layout.addWidget(self.table, 1)
        self.total_label = _label()
        layout.addWidget(self.total_label)
        if not rows:
            layout.addWidget(_label("보유 종목을 추가하면 목표 비중을 설정할 수 있습니다."))
        if any(entry.get("current_percent") is None for entry in rows):
            layout.addWidget(_label("일부 종목의 시세가 없거나 이전 시세여서 현재 비중과 목표 대비 차이를 계산하지 않았습니다."))
        self.buttons = _buttons(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.update_weights()

    def get_targets(self):
        targets = {}
        with localcontext() as context:
            context.prec = 160
            total = Decimal(0)
            for code, widget in self.target_inputs.items():
                number = _decimal(widget.text().strip() or "0", "목표 비중", maximum=Decimal(100))
                total += number
                if number:
                    targets[code] = _text(number)
            if total > 100:
                raise ValueError("목표 비중의 합계는 100%를 넘을 수 없습니다.")
        return targets

    def update_weights(self, *_):
        try:
            targets = self.get_targets()
        except ValueError as exc:
            self.total_label.setText(str(exc))
            self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
            for row in range(self.table.rowCount()):
                self.table.item(row, 3).setText("—")
            return
        with localcontext() as context:
            context.prec = 160
            total = sum((Decimal(value) for value in targets.values()), Decimal(0))
            self.total_label.setText(f"목표 합계 {total:.2f}% · 미배정 {100 - total:.2f}%")
            for row, (code, current) in enumerate(self._current_percent.items()):
                drift = None if current is None else current - Decimal(targets.get(code, "0"))
                self.table.item(row, 3).setText("미확인" if drift is None else f"{drift:+.2f}%p")
        self.buttons.button(QDialogButtonBox.Save).setEnabled(True)

    def accept(self):
        try:
            self.get_targets()
        except ValueError as exc:
            QMessageBox.warning(self, "목표 비중 확인", str(exc))
            return
        super().accept()


class AlertDialog(QDialog):
    """Create a threshold checked by the running app when quotes refresh."""

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("가격 알림 추가")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.code_combo, self.direction_combo = QComboBox(), QComboBox()
        seen = set()
        for holding in build_snapshot(data, {}).holdings:
            if holding.code not in seen:
                self.code_combo.addItem(f"{holding.name} ({holding.code})", holding.code)
                seen.add(holding.code)
        self.direction_combo.addItem("이 가격 이상", "above")
        self.direction_combo.addItem("이 가격 이하", "below")
        self.price_input = QLineEdit()
        self.price_input.setPlaceholderText("알림 기준 가격 · 원")
        form.addRow("종목", self.code_combo)
        form.addRow("조건", self.direction_combo)
        form.addRow("기준 가격(원)", self.price_input)
        layout.addLayout(form)
        layout.addWidget(_label("앱에서 시세를 새로고침할 때 확인합니다. 앱을 닫으면 알림을 확인하지 않습니다."))
        self.buttons = _buttons(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(bool(seen))
        layout.addWidget(self.buttons)
        if not seen:
            layout.addWidget(_label("먼저 보유 종목을 추가하세요."))

    def get_data(self):
        code = self.code_combo.currentData()
        if not code:
            raise ValueError("알림을 설정할 보유 종목이 없습니다.")
        price = _decimal(self.price_input.text(), "기준 가격", positive=True, maximum=MAX_PRICE)
        return dict(code=code, direction=self.direction_combo.currentData(), price=_text(price))

    def accept(self):
        try:
            self.get_data()
        except ValueError as exc:
            QMessageBox.warning(self, "알림 확인", str(exc))
            return
        super().accept()


def _csv_safe(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")) else text


class HistoryDialog(QDialog):
    """Saved stock-only valuations, deliberately not presented as returns."""

    def __init__(self, data, member=None, parent=None):
        super().__init__(parent)
        self.member = member
        scope = "*" if member is None else member
        self.records = sorted(
            [record for record in getattr(data, "metadata", {}).get("snapshots", []) if record.get("member") == scope],
            key=lambda record: (record.get("date", ""), record.get("recorded_at", "")),
        )
        self.setWindowTitle("자산 기록")
        self.resize(940, 720)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(f"{'가족 전체' if member is None else member} · 저장한 날짜별 주식 자산"))
        layout.addWidget(_label("주식 평가금액만 기록합니다. 현금·입출금은 포함되지 않아 그래프 변화가 투자 수익률을 뜻하지 않습니다. 같은 날짜의 기록은 마지막 저장값을 표시합니다."))
        self.figure = Figure(figsize=(8, 3), facecolor="white")
        self.canvas = FigureCanvas(self.figure)
        layout.addWidget(self.canvas, 1)
        self.table = _table(["날짜", "매수 원금(원)", "평가금액(원)", "평가손익(원)", "보유 종목", "시세 상태"])
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setRowCount(len(self.records))
        for row, record in enumerate(self.records):
            cells = [QTableWidgetItem(str(record.get("date", "")))]
            for field in ("total_cost", "market_value", "profit"):
                value = _stored_number(record.get(field))
                cells.append(_NumberItem("—" if value is None else f"{value:,.2f}", value))
            count = record.get("holding_count", 0)
            cells.append(_NumberItem(str(count), count))
            status = "완전 시세" if not record.get("missing_count") and not record.get("stale_count") else "일부 또는 이전 시세"
            cells.append(QTableWidgetItem(status))
            for column, item in enumerate(cells):
                item.setToolTip(str(record.get("recorded_at", "")))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.DescendingOrder)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.status_label = _label(f"저장된 기록 {len(self.records)}건")
        actions.addWidget(self.status_label, 1)
        export_button = QPushButton("CSV 내보내기")
        export_button.clicked.connect(self.export_data)
        actions.addWidget(export_button)
        close = _buttons(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.render_chart()

    def render_chart(self):
        self.figure.clear()
        axes = self.figure.add_subplot(111)
        if self.records:
            positions = [Date.fromisoformat(record["date"]) for record in self.records]
            for field, label, color in (("market_value", "평가금액", "#287d70"), ("total_cost", "매수 원금", "#8c9aba")):
                axes.plot(positions, [float(record[field]) for record in self.records], marker="o", markersize=4, label=label, color=color)
            step = max(1, (len(positions) + 5) // 6)
            ticks = sorted(set(list(range(0, len(positions), step)) + [len(positions) - 1]))
            axes.set_xticks([positions[index] for index in ticks])
            axes.set_xticklabels([self.records[index]["date"] for index in ticks], rotation=25, ha="right")
            maximum = max(float(record[field]) for record in self.records for field in ("market_value", "total_cost"))
            unit = 10000 if maximum >= 100000 else 1
            axes.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / unit:,.0f}"))
            axes.set_ylabel("만원" if unit == 10000 else "원")
            axes.legend(frameon=False)
            axes.grid(axis="y", alpha=0.15)
            axes.spines[["top", "right"]].set_visible(False)
        else:
            axes.text(0.5, 0.5, "시세 조회 후 자산 기록을 저장하세요", ha="center", va="center", transform=axes.transAxes)
            axes.set_axis_off()
        self.figure.subplots_adjust(left=0.12, right=0.97, top=0.92, bottom=0.26)
        self.canvas.draw_idle()

    def export_data(self):
        path = _save_path(self, "자산 기록 내보내기", "portfolio_history.csv")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["가족", "날짜", "기록시각", "매수원금(원)", "평가금액(원)", "평가손익(원)", "보유종목", "시세미확인", "이전시세"])
                for record in self.records:
                    writer.writerow([
                        _csv_safe("가족 전체" if self.member is None else self.member),
                        _csv_safe(record.get("date", "")), _csv_safe(record.get("recorded_at", "")),
                        record.get("total_cost", ""), record.get("market_value", ""), record.get("profit", ""),
                        record.get("holding_count", 0), record.get("missing_count", 0), record.get("stale_count", 0),
                    ])
        except OSError as exc:
            QMessageBox.critical(self, "내보내기 실패", str(exc))
        else:
            self.status_label.setText(f"자산 기록 {len(self.records)}건을 CSV로 내보냈습니다.")
