import csv
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from vnpy.trader.constant import Direction, Exchange
from vnpy.trader.object import ContractData
from vnpy.event.engine import Event
from vnpy.trader.ui import QtWidgets, QtCore, QtGui

from vnpy.trader.engine import MainEngine, EventEngine
from vnpy.trader.ui.widget import (
    BaseCell,
    EnumCell,
    DirectionCell,
    TimeCell
)

from ..database import trade_key
from ..engine import (
    APP_NAME,
    EVENT_PM_CONTRACT,
    EVENT_PM_HISTORY,
    EVENT_PM_PORTFOLIO,
    EVENT_PM_TRADE,
    PortfolioEngine
)
from .chart import (
    METRIC_PNL,
    METRIC_RETURN,
    PortfolioChart,
    PortfolioSummaryTable,
    pnl_color
)


RED_COLOR = QtGui.QColor("red")
GREEN_COLOR = QtGui.QColor("green")
WHITE_COLOR = QtGui.QColor("white")

TREE_LABELS: list[str] = [
    "组合名称",
    "本地代码",
    "名称",
    "开盘仓位",
    "当前仓位",
    "交易盈亏",
    "持仓盈亏",
    "总盈亏",
    "多头成交",
    "空头成交"
]

TRADE_LABELS: list[str] = [
    "组合",
    "成交号",
    "委托号",
    "代码",
    "名称",
    "交易所",
    "方向",
    "价格",
    "数量",
    "时间",
    "接口",
    "标记"
]

# 成交记录各列默认宽度：列宽可自由拖动，不再按内容自适应，所以得给个初值
TRADE_COLUMN_WIDTHS: list[int] = [110, 140, 140, 130, 150, 80, 70, 90, 70, 170, 110, 120]

# “全部”快捷区间用的起始日期：本模块的成交只会从现在开始积累，用这个值占位即可
TRADE_ALL_START_DATE: date = date(2000, 1, 1)


def get_contract_name(main_engine: MainEngine, vt_symbol: str) -> str:
    """从主引擎获取合约名称，合约尚未加载时返回空字符串"""
    contract: ContractData | None = main_engine.get_contract(vt_symbol)
    if not contract:
        return ""

    return contract.name


def make_exchange(row: dict[str, Any]) -> Exchange | None:
    """把库里存的交易所枚举名还原回 Exchange，无法识别时返回 None（单元格留空）"""
    name: str = str(row.get("exchange") or "")
    if not name:
        return None

    try:
        return Exchange[name]
    except KeyError:
        return None


def make_direction(row: dict[str, Any]) -> Direction | None:
    """把库里存的方向枚举名还原回 Direction，无法识别时返回 None"""
    name: str = str(row.get("direction") or "")
    if not name:
        return None

    try:
        return Direction[name]
    except KeyError:
        return None


def format_number(value: Any) -> str:
    """价格/数量列显示：库里的 DECIMAL 转 float 后再 str，与内存模式下的显示一致"""
    if isinstance(value, (Decimal, int, float)):
        return str(float(value))
    if value is None:
        return ""
    return str(value)


def format_pnl(value: float) -> str:
    """盈亏数值：保留 2 位小数

    引擎算出来的是浮点（价格×乘数×手数），直接 str() 会拖一长串小数。
    """
    return f"{value:.2f}"


class PortfolioManager(QtWidgets.QWidget):
    """"""

    signal_contract: QtCore.Signal = QtCore.Signal(Event)
    signal_portfolio: QtCore.Signal = QtCore.Signal(Event)
    signal_trade: QtCore.Signal = QtCore.Signal(Event)
    signal_history: QtCore.Signal = QtCore.Signal(Event)
    # 历史成交查询结果：后台线程 emit，Qt 自动排队到界面线程
    signal_trades_loaded: QtCore.Signal = QtCore.Signal(dict)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.portfolio_engine: PortfolioEngine = main_engine.get_engine(APP_NAME)

        self.column_count: int = len(TREE_LABELS)
        self.contract_items: dict[tuple[str, str], QtWidgets.QTreeWidgetItem] = {}
        self.portfolio_items: dict[str, QtWidgets.QTreeWidgetItem] = {}

        # 查询序号：日期/筛选条件变得比后台线程快时，用序号丢弃过期结果
        self.trade_query_token: int = 0
        # 查询在途期间收到的实时成交：查库结果落地后要补插回去，
        # 否则查库快照早于成交时会"刚显示又消失"（数据库里其实已经有了）
        self.trade_query_running: bool = False
        self.trade_pending_rows: list[dict[str, Any]] = []

        self.init_ui()
        self.register_event()
        self.init_data()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle("投资组合")

        self.tree: QtWidgets.QTreeWidget = self.create_tree()
        self.chart: PortfolioChart = PortfolioChart()
        self.summary: PortfolioSummaryTable = PortfolioSummaryTable()
        self.summary.setMinimumHeight(80)
        self.summary.signal_capital.connect(self.portfolio_engine.set_capital)
        self.summary.signal_visible.connect(self.apply_visible_references)

        self.monitor: PortfolioTradeMonitor = PortfolioTradeMonitor()

        tabs: QtWidgets.QTabWidget = QtWidgets.QTabWidget()
        tabs.addTab(self.create_chart_tab(), "收益曲线")
        tabs.addTab(self.create_holding_tab(), "持仓明细")
        tabs.addTab(self.create_trade_tab(), "成交记录")

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(self.create_toolbar())
        vbox.addWidget(tabs)
        self.setLayout(vbox)

    def create_toolbar(self) -> QtWidgets.QHBoxLayout:
        """"""
        interval_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        interval_spin.setMinimum(1)
        interval_spin.setMaximum(60)
        interval_spin.setSuffix("秒")
        interval_spin.setValue(self.portfolio_engine.get_timer_interval())
        interval_spin.valueChanged.connect(self.portfolio_engine.set_timer_interval)

        self.summary_label: QtWidgets.QLabel = QtWidgets.QLabel()

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(self.summary_label)
        hbox.addStretch()
        hbox.addWidget(QtWidgets.QLabel("刷新频率"))
        hbox.addWidget(interval_spin)
        return hbox

    def create_tree(self) -> QtWidgets.QTreeWidget:
        """"""
        tree: QtWidgets.QTreeWidget = QtWidgets.QTreeWidget()
        tree.setColumnCount(self.column_count)
        tree.setHeaderLabels(TREE_LABELS)
        tree.header().setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        tree.header().setStretchLastSection(False)

        tree.setColumnWidth(0, 150)
        tree.setColumnWidth(1, 140)
        tree.setColumnWidth(2, 150)
        for column in range(3, self.column_count):
            tree.setColumnWidth(column, 90)

        delegate: TreeDelegate = TreeDelegate()
        tree.setItemDelegate(delegate)
        return tree

    def create_chart_tab(self) -> QtWidgets.QWidget:
        """"""
        metric_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        metric_combo.addItem("累计总盈亏", METRIC_PNL)
        metric_combo.addItem("累计收益率", METRIC_RETURN)
        metric_combo.currentIndexChanged.connect(
            lambda _: self.set_chart_metric(metric_combo.currentData())
        )

        reset_button: QtWidgets.QPushButton = QtWidgets.QPushButton("重置视图")
        reset_button.clicked.connect(self.chart.reset_view)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel("显示指标"))
        hbox.addWidget(metric_combo)
        hbox.addSpacing(20)
        hbox.addWidget(QtWidgets.QLabel("鼠标移动显示数值，滚轮缩放，拖拽平移"))
        hbox.addStretch()
        hbox.addWidget(reset_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.create_chart_splitter(), 1)

        widget: QtWidgets.QWidget = QtWidgets.QWidget()
        widget.setLayout(vbox)
        return widget

    def create_chart_splitter(self) -> QtWidgets.QSplitter:
        """汇总表与曲线之间的分隔条：可上下拖动，默认 4:6"""
        splitter: QtWidgets.QSplitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        splitter.addWidget(self.summary)
        splitter.addWidget(self.chart)

        # 不允许拖到 0：否则拖没了就找不回来
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        # 权重相等才会按 400:600 等比缩放（只有一个非0时，另一个会吃掉全部差值）
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 600])
        return splitter

    def create_holding_tab(self) -> QtWidgets.QWidget:
        """"""
        expand_button: QtWidgets.QPushButton = QtWidgets.QPushButton("全部展开")
        expand_button.clicked.connect(self.tree.expandAll)

        collapse_button: QtWidgets.QPushButton = QtWidgets.QPushButton("全部折叠")
        collapse_button.clicked.connect(self.tree.collapseAll)

        resize_button: QtWidgets.QPushButton = QtWidgets.QPushButton("调整列宽")
        resize_button.clicked.connect(self.resize_columns)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(expand_button)
        hbox.addWidget(collapse_button)
        hbox.addWidget(resize_button)
        hbox.addStretch()

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.tree)

        widget: QtWidgets.QWidget = QtWidgets.QWidget()
        widget.setLayout(vbox)
        return widget

    def create_trade_tab(self) -> QtWidgets.QWidget:
        """"""
        # 日期范围与快捷区间：默认只看当日（与原来"只显示当日成交"的习惯一致）
        self.trade_start_date: QtWidgets.QDateEdit = self.create_trade_date_edit()
        self.trade_end_date: QtWidgets.QDateEdit = self.create_trade_date_edit()

        self.trade_query_button: QtWidgets.QPushButton = QtWidgets.QPushButton("查询")
        self.trade_query_button.clicked.connect(self.refresh_trades)

        today_button: QtWidgets.QPushButton = QtWidgets.QPushButton("今天")
        today_button.clicked.connect(lambda: self.apply_trade_quick_range(0))

        week_button: QtWidgets.QPushButton = QtWidgets.QPushButton("近一周")
        week_button.clicked.connect(lambda: self.apply_trade_quick_range(7))

        month_button: QtWidgets.QPushButton = QtWidgets.QPushButton("近一月")
        month_button.clicked.connect(lambda: self.apply_trade_quick_range(30))

        all_button: QtWidgets.QPushButton = QtWidgets.QPushButton("全部")
        all_button.clicked.connect(
            lambda: self.set_trade_date_range(TRADE_ALL_START_DATE, date.today())
        )

        range_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        range_layout.addWidget(QtWidgets.QLabel("日期"))
        range_layout.addWidget(self.trade_start_date)
        range_layout.addWidget(QtWidgets.QLabel("~"))
        range_layout.addWidget(self.trade_end_date)
        range_layout.addWidget(today_button)
        range_layout.addWidget(week_button)
        range_layout.addWidget(month_button)
        range_layout.addWidget(all_button)
        range_layout.addWidget(self.trade_query_button)
        range_layout.addStretch()

        # 组合/合约改为服务端查询条件，可选项来自库里出现过的值
        self.trade_reference_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.trade_reference_combo.setMinimumWidth(140)
        self.trade_reference_combo.addItem("全部组合", "")
        self.trade_reference_combo.currentIndexChanged.connect(self.apply_trade_filter)

        self.trade_symbol_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.trade_symbol_combo.setMinimumWidth(140)
        self.trade_symbol_combo.addItem("全部合约", "")
        self.trade_symbol_combo.currentIndexChanged.connect(self.apply_trade_filter)

        clear_button: QtWidgets.QPushButton = QtWidgets.QPushButton("清空筛选")
        clear_button.clicked.connect(self.clear_trade_filter)

        export_button: QtWidgets.QPushButton = QtWidgets.QPushButton("导出CSV")
        export_button.clicked.connect(self.export_trades)

        filter_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        filter_layout.addWidget(QtWidgets.QLabel("组合"))
        filter_layout.addWidget(self.trade_reference_combo)
        filter_layout.addWidget(QtWidgets.QLabel("合约"))
        filter_layout.addWidget(self.trade_symbol_combo)
        filter_layout.addWidget(clear_button)
        filter_layout.addStretch()
        filter_layout.addWidget(export_button)

        self.trade_status_label: QtWidgets.QLabel = QtWidgets.QLabel()

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(range_layout)
        vbox.addLayout(filter_layout)
        vbox.addWidget(self.trade_status_label)
        vbox.addWidget(self.monitor)

        widget: QtWidgets.QWidget = QtWidgets.QWidget()
        widget.setLayout(vbox)
        return widget

    def create_trade_date_edit(self) -> QtWidgets.QDateEdit:
        """日期选择框：带日历弹出，默认今天"""
        date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit()
        date_edit.setCalendarPopup(True)
        date_edit.setDisplayFormat("yyyy-MM-dd")
        date_edit.setDate(QtCore.QDate.currentDate())
        date_edit.setMinimumWidth(120)
        date_edit.dateChanged.connect(self.on_trade_date_changed)
        return date_edit

    def get_trade_date_range(self) -> tuple[date, date]:
        """当前起止日期（QDate → datetime.date）"""
        start: QtCore.QDate = self.trade_start_date.date()
        end: QtCore.QDate = self.trade_end_date.date()
        return (
            date(start.year(), start.month(), start.day()),
            date(end.year(), end.month(), end.day())
        )

    def set_trade_date_range(self, start_date: date, end_date: date) -> None:
        """同时设置起止日期，只触发一次查询"""
        for date_edit in (self.trade_start_date, self.trade_end_date):
            date_edit.blockSignals(True)

        self.trade_start_date.setDate(
            QtCore.QDate(start_date.year, start_date.month, start_date.day)
        )
        self.trade_end_date.setDate(
            QtCore.QDate(end_date.year, end_date.month, end_date.day)
        )

        for date_edit in (self.trade_start_date, self.trade_end_date):
            date_edit.blockSignals(False)

        self.refresh_trades()

    def apply_trade_quick_range(self, days: int) -> None:
        """快捷区间：days 为向前回溯的天数（0 = 仅今天）"""
        today: date = date.today()
        self.set_trade_date_range(today - timedelta(days=days), today)

    def on_trade_date_changed(self) -> None:
        """日期变化：起始晚于结束时把被改的那个同步到另一端，避免查出空结果"""
        sender: Any = self.sender()
        start, end = self.get_trade_date_range()

        if start > end:
            is_start: bool = sender is self.trade_start_date
            source: QtWidgets.QDateEdit = (
                self.trade_start_date if is_start else self.trade_end_date
            )
            target: QtWidgets.QDateEdit = (
                self.trade_end_date if is_start else self.trade_start_date
            )

            target.blockSignals(True)
            target.setDate(source.date())
            target.blockSignals(False)

        self.refresh_trades()

    def refresh_trades(self) -> None:
        """按当前日期范围与筛选条件重查成交记录"""
        if not self.portfolio_engine.is_trade_db_ready():
            # 未加载 SqlApp：退回主引擎内存里的当日成交（没有历史可查）
            self.trade_query_running = False
            self.trade_pending_rows = []
            self.monitor.set_range(date.min, date.max)
            self.monitor.set_rows(self.portfolio_engine.get_reference_trade_rows())
            self.trade_status_label.setText("未加载 SqlApp，仅显示当日内存成交，无法查询历史")
            return

        start, end = self.get_trade_date_range()
        reference: str = self.trade_reference_combo.currentData() or ""
        symbol: str = self.trade_symbol_combo.currentData() or ""

        # 实时成交插入时用同一区间判断是否落在当前视图内
        self.monitor.set_range(start, end)

        self.trade_query_token += 1
        token: int = self.trade_query_token
        self.trade_query_running = True
        self.trade_pending_rows = []

        self.trade_query_button.setEnabled(False)
        self.trade_status_label.setText("查询中…")

        thread: threading.Thread = threading.Thread(
            target=self.run_trade_query,
            args=(token, start, end, reference, symbol),
            daemon=True
        )
        thread.start()

    def run_trade_query(
        self,
        token: int,
        start_date: date,
        end_date: date,
        reference: str,
        symbol: str
    ) -> None:
        """后台线程：查库（同步接口，线程各自持有连接），结果通过信号回界面线程"""
        data: dict[str, Any] = self.portfolio_engine.query_trades(
            start_date,
            end_date,
            reference,
            symbol
        )
        data["token"] = token
        self.signal_trades_loaded.emit(data)

    def process_trades_loaded(self, data: dict[str, Any]) -> None:
        """查询返回：刷新表格与状态栏（只采纳最后一次查询的结果）"""
        if data.get("token") != self.trade_query_token:
            return

        self.trade_query_running = False
        self.trade_query_button.setEnabled(True)

        error: str = data.get("error", "")
        if error:
            self.trade_status_label.setText(f"查询失败：{error}")
            return

        rows: list[dict[str, Any]] = data.get("rows", [])
        self.monitor.set_rows(rows)

        # 补插查询期间到达的实时成交（set_rows 会把它们连同旧结果一起清掉；
        # 已经包含在查询结果里的会按 (交易日, vt_tradeid) 去重，不会重复）
        for row in self.trade_pending_rows:
            self.monitor.append_row(row)
        self.trade_pending_rows = []

        # 历史查询里出现过的组合/合约也补进筛选下拉框（下次查询就能按它筛）
        for row in rows:
            self.add_trade_filter_option(row)

        if data.get("truncated"):
            limit: int = self.portfolio_engine.sql_settings.max_rows
            self.trade_status_label.setText(
                f"已显示最新的 {limit} 条，可能有更多记录，请缩小日期范围"
            )
        else:
            self.trade_status_label.setText(f"共 {len(rows)} 条成交记录")

    def init_trade_filter_options(self) -> None:
        """初始化筛选下拉框：优先取库里出现过的组合/合约（历史组合也能筛到）"""
        references, symbols = self.portfolio_engine.get_trade_filter_options()

        for reference in references:
            if self.trade_reference_combo.findData(reference) < 0:
                self.trade_reference_combo.addItem(reference, reference)

        for symbol in symbols:
            if self.trade_symbol_combo.findData(symbol) < 0:
                self.trade_symbol_combo.addItem(symbol, symbol)

        # 未接入数据库时至少把内存里的成交塞进去
        for row in self.portfolio_engine.get_reference_trade_rows():
            self.add_trade_filter_option(row)

    def register_event(self) -> None:
        """"""
        self.signal_contract.connect(self.process_contract_event)
        self.signal_portfolio.connect(self.process_portfolio_event)
        self.signal_trade.connect(self.process_trade_event)
        self.signal_history.connect(self.process_history_event)
        self.signal_trades_loaded.connect(self.process_trades_loaded)

        self.event_engine.register(EVENT_PM_CONTRACT, self.signal_contract.emit)
        self.event_engine.register(EVENT_PM_PORTFOLIO, self.signal_portfolio.emit)
        self.event_engine.register(EVENT_PM_TRADE, self.signal_trade.emit)
        self.event_engine.register(EVENT_PM_HISTORY, self.signal_history.emit)

    def init_data(self) -> None:
        """初始化成交记录与历史盈亏"""
        self.init_trade_filter_options()
        self.refresh_trades()
        self.update_history(self.portfolio_engine.get_history_data())

    def get_portfolio_item(self, reference: str) -> QtWidgets.QTreeWidgetItem:
        """"""
        portfolio_item: QtWidgets.QTreeWidgetItem | None = self.portfolio_items.get(reference, None)

        if not portfolio_item:
            portfolio_item = QtWidgets.QTreeWidgetItem()
            portfolio_item.setText(0, reference)
            for i in range(2, self.column_count):
                portfolio_item.setTextAlignment(i, QtCore.Qt.AlignmentFlag.AlignCenter)

            self.portfolio_items[reference] = portfolio_item
            self.tree.addTopLevelItem(portfolio_item)
            portfolio_item.setExpanded(True)

        return portfolio_item

    def get_contract_item(self, reference: str, vt_symbol: str) -> QtWidgets.QTreeWidgetItem:
        """"""
        key: tuple[str, str] = (reference, vt_symbol)
        contract_item: QtWidgets.QTreeWidgetItem | None = self.contract_items.get(key, None)

        if not contract_item:
            contract_item = QtWidgets.QTreeWidgetItem()
            contract_item.setText(1, vt_symbol)
            contract_item.setText(2, get_contract_name(self.main_engine, vt_symbol))
            for i in range(2, self.column_count):
                contract_item.setTextAlignment(i, QtCore.Qt.AlignmentFlag.AlignCenter)

            self.contract_items[key] = contract_item

            portfolio_item: QtWidgets.QTreeWidgetItem = self.get_portfolio_item(reference)
            portfolio_item.addChild(contract_item)

        return contract_item

    def process_contract_event(self, event: Event) -> None:
        """"""
        contract_result: dict = event.data

        contract_item: QtWidgets.QTreeWidgetItem = self.get_contract_item(
            contract_result["reference"],
            contract_result["vt_symbol"]
        )

        # 合约信息可能比成交晚加载，名称空缺时每次推送都补一下
        if not contract_item.text(2):
            contract_item.setText(
                2,
                get_contract_name(self.main_engine, contract_result["vt_symbol"])
            )

        contract_item.setText(3, str(contract_result["open_pos"]))
        contract_item.setText(4, str(contract_result["last_pos"]))
        contract_item.setText(5, format_pnl(contract_result["trading_pnl"]))
        contract_item.setText(6, format_pnl(contract_result["holding_pnl"]))
        contract_item.setText(7, format_pnl(contract_result["total_pnl"]))
        contract_item.setText(8, str(contract_result["long_volume"]))
        contract_item.setText(9, str(contract_result["short_volume"]))

        self.update_item_color(contract_item, contract_result)

    def process_portfolio_event(self, event: Event) -> None:
        """"""
        portfolio_result: dict = event.data

        portfolio_item: QtWidgets.QTreeWidgetItem = self.get_portfolio_item(portfolio_result["reference"])
        portfolio_item.setText(5, format_pnl(portfolio_result["trading_pnl"]))
        portfolio_item.setText(6, format_pnl(portfolio_result["holding_pnl"]))
        portfolio_item.setText(7, format_pnl(portfolio_result["total_pnl"]))

        self.update_item_color(portfolio_item, portfolio_result)

    def process_trade_event(self, event: Event) -> None:
        """"""
        row: dict[str, Any] = event.data

        self.monitor.append_row(row)
        self.add_trade_filter_option(row)
        if self.trade_query_running:
            self.trade_pending_rows.append(row)
    def process_history_event(self, event: Event) -> None:
        """"""
        self.update_history(event.data)

    def update_history(self, data: dict) -> None:
        """刷新收益曲线、汇总表格与顶部汇总信息"""
        history: dict = data.get("history", {})
        capitals: dict = data.get("capitals", {})
        date_str: str = data.get("date", "")

        # 先同步勾选状态，再一次性重绘曲线与汇总
        self.summary.update_data(history, capitals, date_str)
        self.chart.update_data(history, capitals, self.summary.get_visible_references())

        day_pnl: float = 0.0
        cum_pnl: float = 0.0
        for days in history.values():
            for trade_date, day_data in days.items():
                if trade_date == date_str:
                    day_pnl += day_data["total_pnl"]
                cum_pnl += day_data["total_pnl"]

        self.summary_label.setText(
            f"数据日期: {date_str} ｜ 组合: {len(history)} 个 ｜ "
            f"当日合计: <span style='color:{pnl_color(day_pnl)}'>{day_pnl:,.2f}</span> ｜ "
            f"累计合计: <span style='color:{pnl_color(cum_pnl)}'>{cum_pnl:,.2f}</span>"
        )

    def update_item_color(
        self,
        item: QtWidgets.QTreeWidgetItem,
        result: dict
    ) -> None:
        start_column: int = 5
        for n, pnl in enumerate([
            result["trading_pnl"],
            result["holding_pnl"],
            result["total_pnl"]
        ]):
            i: int = n + start_column

            if pnl > 0:
                item.setForeground(i, RED_COLOR)
            elif pnl < 0:
                item.setForeground(i, GREEN_COLOR)
            else:
                item.setForeground(i, WHITE_COLOR)

    def resize_columns(self) -> None:
        """"""
        for i in range(self.column_count):
            self.tree.resizeColumnToContents(i)

    def set_chart_metric(self, metric: str) -> None:
        """"""
        self.chart.set_metric(metric)

    def apply_visible_references(self) -> None:
        """按汇总表勾选状态刷新图表上的曲线"""
        self.chart.set_visible_references(self.summary.get_visible_references())

    def add_trade_filter_option(self, row: dict[str, Any]) -> None:
        """把新的组合/合约加入筛选下拉框"""
        reference: str = row.get("reference", "")

        if reference and self.trade_reference_combo.findData(reference) < 0:
            self.trade_reference_combo.addItem(reference, reference)

        symbol: str = row.get("symbol", "")

        if symbol and self.trade_symbol_combo.findData(symbol) < 0:
            self.trade_symbol_combo.addItem(symbol, symbol)

    def apply_trade_filter(self) -> None:
        """筛选条件变化：同步表格的过滤状态（实时成交用）并重新查询"""
        self.monitor.set_filter(
            self.trade_reference_combo.currentData() or "",
            self.trade_symbol_combo.currentData() or ""
        )
        self.refresh_trades()

    def clear_trade_filter(self) -> None:
        """清空组合/合约筛选（只触发一次查询）"""
        for combo in (self.trade_reference_combo, self.trade_symbol_combo):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)

        self.apply_trade_filter()

    def export_trades(self) -> None:
        """导出当前筛选后的成交记录"""
        filename: str = f"trade_record_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出成交记录",
            filename,
            "CSV文件 (*.csv)"
        )
        if not filepath:
            return

        count: int = self.monitor.export_csv(filepath)
        QtWidgets.QMessageBox.information(
            self,
            "导出完成",
            f"已导出 {count} 条成交记录到\n{filepath}"
        )

    def show(self) -> None:
        """"""
        self.showMaximized()


class TradeTimeCell(TimeCell):
    """成交时间：在 vnpy TimeCell 基础上补上 yyyyMMdd 日期

    TimeCell 只显示 %H:%M:%S，成交记录跨交易日时看不出是哪一天。
    """

    def set_content(self, content: datetime | None, data: Any) -> None:
        """"""
        if content is None:
            return

        # 从数据库读回的是本地时区的 naive datetime（vnpy 推送的则带时区）
        if content.tzinfo is None:
            content = content.replace(tzinfo=self.local_tz)
        else:
            content = content.astimezone(self.local_tz)

        millisecond: int = int(content.microsecond / 1000)

        self._text = f"{content.strftime('%Y%m%d %H:%M:%S')}.{millisecond:03d}"
        self._data = data
        self.setText(self._text)


class PortfolioTradeMonitor(QtWidgets.QTableWidget):
    """成交记录表格

    行数据统一是 dict（键见 ``database.TRADE_COLUMNS``）：查库刷新与实时插入共用
    同一套渲染逻辑，界面只跟这一种结构打交道。
    """

    def __init__(self) -> None:
        """"""
        super().__init__()

        # 已插入的行：键 (交易日, vt_tradeid)（CTP 的 tradeid 只在当日内唯一）
        self.trade_keys: set[tuple[str, str]] = set()
        self.filter_reference: str = ""
        self.filter_symbol: str = ""
        # 当前显示的日期范围：实时成交只有落在范围内才插入
        self.start_date: date | None = None
        self.end_date: date | None = None

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setColumnCount(len(TRADE_LABELS))
        self.setHorizontalHeaderLabels(TRADE_LABELS)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(self.SelectionBehavior.SelectRows)

        # Interactive：列宽可以用鼠标拖动调整（不再按内容自适应，所以下面给各列一个初值）
        header: QtWidgets.QHeaderView = self.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(50)

        for column, width in enumerate(TRADE_COLUMN_WIDTHS):
            self.setColumnWidth(column, width)

    def set_range(self, start_date: date, end_date: date) -> None:
        """记录当前显示的日期范围（实时成交的可见性判断用）"""
        self.start_date = start_date
        self.end_date = end_date

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        """整体刷新（查询结果，已按时间倒序）"""
        # 几千行 × 12 列逐个建单元格很慢，先关掉重绘
        self.setUpdatesEnabled(False)
        try:
            self.setRowCount(0)
            self.trade_keys.clear()

            for row in rows:
                self.insert_trade_row(self.rowCount(), row)
        finally:
            self.setUpdatesEnabled(True)

    def append_row(self, row: dict[str, Any]) -> None:
        """实时成交：落在当前日期范围内且未出现过时才插到最上面"""
        if not self.is_row_in_range(row):
            return

        key: tuple[str, str] = trade_key(row)
        if key in self.trade_keys:
            return

        self.insert_trade_row(0, row)

    def is_row_in_range(self, row: dict[str, Any]) -> bool:
        """成交日期是否落在当前显示的区间内"""
        if self.start_date is None or self.end_date is None:
            return True

        trade_date: Any = row.get("trade_date")
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        if not isinstance(trade_date, date):
            return True

        return self.start_date <= trade_date <= self.end_date

    def insert_trade_row(self, index: int, row: dict[str, Any]) -> None:
        """在第 index 行插入一行成交"""
        self.trade_keys.add(trade_key(row))
        self.insertRow(index)

        for column, cell in enumerate(self.create_cells(row)):
            self.setItem(index, column, cell)

        self.update_row_visible(index)

    def create_cells(self, row: dict[str, Any]) -> list[QtWidgets.QTableWidgetItem]:
        """按列顺序生成单元格（顺序与 TRADE_LABELS 一致）"""
        return [
            BaseCell(row.get("reference", ""), row),
            BaseCell(row.get("tradeid", ""), row),
            BaseCell(row.get("orderid", ""), row),
            BaseCell(row.get("symbol", ""), row),
            BaseCell(row.get("name", ""), row),
            EnumCell(make_exchange(row), row),
            DirectionCell(make_direction(row), row),
            BaseCell(format_number(row.get("price")), row),
            BaseCell(format_number(row.get("volume")), row),
            TradeTimeCell(row.get("trade_time"), row),
            BaseCell(row.get("gateway_name", ""), row),
            BaseCell(row.get("mark", ""), row),
        ]

    def update_row_visible(self, row: int) -> None:
        """"""
        reference_item: QtWidgets.QTableWidgetItem | None = self.item(row, 0)
        symbol_item: QtWidgets.QTableWidgetItem | None = self.item(row, 3)

        if not reference_item or not symbol_item:
            return

        visible: bool = (
            (not self.filter_reference or reference_item.text() == self.filter_reference)
            and (not self.filter_symbol or symbol_item.text() == self.filter_symbol)
        )

        if visible:
            self.showRow(row)
        else:
            self.hideRow(row)

    def set_filter(self, reference: str, symbol: str) -> None:
        """"""
        self.filter_reference = reference
        self.filter_symbol = symbol

        for row in range(self.rowCount()):
            self.update_row_visible(row)

    def export_csv(self, filepath: str) -> int:
        """导出可见的成交记录，返回导出的行数"""
        count: int = 0

        with open(filepath, mode="w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(TRADE_LABELS)

            for row in range(self.rowCount()):
                if self.isRowHidden(row):
                    continue

                values: list[str] = []
                for column in range(len(TRADE_LABELS)):
                    item: QtWidgets.QTableWidgetItem | None = self.item(row, column)
                    values.append(item.text() if item else "")

                writer.writerow(values)
                count += 1

        return count


class TreeDelegate(QtWidgets.QStyledItemDelegate):
    """"""

    def sizeHint(
        self,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex
    ) -> QtCore.QSize:
        """"""
        size: QtCore.QSize = super().sizeHint(option, index)
        size.setHeight(40)
        return size
