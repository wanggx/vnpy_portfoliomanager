import csv
from datetime import datetime
from typing import Any

from vnpy.trader.object import ContractData, OrderData, TradeData
from vnpy.event.engine import Event
from vnpy.trader.ui import QtWidgets, QtCore, QtGui

from vnpy.trader.engine import MainEngine, EventEngine
from vnpy.trader.ui.widget import (
    BaseCell,
    EnumCell,
    DirectionCell,
    TimeCell
)

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
    "开平",
    "价格",
    "数量",
    "时间",
    "接口",
    "标记"
]

# 成交记录各列默认宽度：列宽可自由拖动，不再按内容自适应，所以得给个初值
TRADE_COLUMN_WIDTHS: list[int] = [110, 140, 140, 130, 150, 80, 70, 70, 90, 70, 170, 110, 120]


def get_contract_name(main_engine: MainEngine, vt_symbol: str) -> str:
    """从主引擎获取合约名称，合约尚未加载时返回空字符串"""
    contract: ContractData | None = main_engine.get_contract(vt_symbol)
    if not contract:
        return ""

    return contract.name


def get_order_mark(main_engine: MainEngine, vt_orderid: str) -> str:
    """从主引擎获取委托的标记（mark）

    vnpy 的 TradeData 没有 mark 字段（OrderData / OrderRequest 才有），
    所以成交流水里要按 vt_orderid 回到委托上取。
    """
    order: OrderData | None = main_engine.get_order(vt_orderid)
    if not order:
        return ""

    return order.mark


class PortfolioManager(QtWidgets.QWidget):
    """"""

    signal_contract: QtCore.Signal = QtCore.Signal(Event)
    signal_portfolio: QtCore.Signal = QtCore.Signal(Event)
    signal_trade: QtCore.Signal = QtCore.Signal(Event)
    signal_history: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.portfolio_engine: PortfolioEngine = main_engine.get_engine(APP_NAME)

        self.column_count: int = len(TREE_LABELS)
        self.contract_items: dict[tuple[str, str], QtWidgets.QTreeWidgetItem] = {}
        self.portfolio_items: dict[str, QtWidgets.QTreeWidgetItem] = {}

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

        self.monitor: PortfolioTradeMonitor = PortfolioTradeMonitor(self.main_engine)

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

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel("组合"))
        hbox.addWidget(self.trade_reference_combo)
        hbox.addWidget(QtWidgets.QLabel("合约"))
        hbox.addWidget(self.trade_symbol_combo)
        hbox.addWidget(clear_button)
        hbox.addStretch()
        hbox.addWidget(export_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.monitor)

        widget: QtWidgets.QWidget = QtWidgets.QWidget()
        widget.setLayout(vbox)
        return widget

    def register_event(self) -> None:
        """"""
        self.signal_contract.connect(self.process_contract_event)
        self.signal_portfolio.connect(self.process_portfolio_event)
        self.signal_trade.connect(self.process_trade_event)
        self.signal_history.connect(self.process_history_event)

        self.event_engine.register(EVENT_PM_CONTRACT, self.signal_contract.emit)
        self.event_engine.register(EVENT_PM_PORTFOLIO, self.signal_portfolio.emit)
        self.event_engine.register(EVENT_PM_TRADE, self.signal_trade.emit)
        self.event_engine.register(EVENT_PM_HISTORY, self.signal_history.emit)

    def init_data(self) -> None:
        """初始化已有的成交记录与历史盈亏"""
        for trade in self.portfolio_engine.get_all_reference_trades():
            self.monitor.update_trade(trade)
            self.add_trade_filter_option(trade)

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
        contract_item.setText(5, str(contract_result["trading_pnl"]))
        contract_item.setText(6, str(contract_result["holding_pnl"]))
        contract_item.setText(7, str(contract_result["total_pnl"]))
        contract_item.setText(8, str(contract_result["long_volume"]))
        contract_item.setText(9, str(contract_result["short_volume"]))

        self.update_item_color(contract_item, contract_result)

    def process_portfolio_event(self, event: Event) -> None:
        """"""
        portfolio_result: dict = event.data

        portfolio_item: QtWidgets.QTreeWidgetItem = self.get_portfolio_item(portfolio_result["reference"])
        portfolio_item.setText(5, str(portfolio_result["trading_pnl"]))
        portfolio_item.setText(6, str(portfolio_result["holding_pnl"]))
        portfolio_item.setText(7, str(portfolio_result["total_pnl"]))

        self.update_item_color(portfolio_item, portfolio_result)

    def process_trade_event(self, event: Event) -> None:
        """"""
        trade: TradeData = event.data

        self.monitor.update_trade(trade)
        self.add_trade_filter_option(trade)

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

    def add_trade_filter_option(self, trade: TradeData) -> None:
        """把新的组合/合约加入筛选下拉框"""
        reference: str = getattr(trade, "reference", "")

        if reference and self.trade_reference_combo.findData(reference) < 0:
            self.trade_reference_combo.addItem(reference, reference)

        if self.trade_symbol_combo.findData(trade.symbol) < 0:
            self.trade_symbol_combo.addItem(trade.symbol, trade.symbol)

    def apply_trade_filter(self) -> None:
        """"""
        self.monitor.set_filter(
            self.trade_reference_combo.currentData(),
            self.trade_symbol_combo.currentData()
        )

    def clear_trade_filter(self) -> None:
        """"""
        self.trade_reference_combo.setCurrentIndex(0)
        self.trade_symbol_combo.setCurrentIndex(0)

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

        content = content.astimezone(self.local_tz)
        millisecond: int = int(content.microsecond / 1000)

        self._text = f"{content.strftime('%Y%m%d %H:%M:%S')}.{millisecond:03d}"
        self._data = data
        self.setText(self._text)


class PortfolioTradeMonitor(QtWidgets.QTableWidget):
    """"""

    def __init__(self, main_engine: MainEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.trade_ids: set[str] = set()
        self.filter_reference: str = ""
        self.filter_symbol: str = ""

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

    def update_trade(self, trade: TradeData) -> None:
        """"""
        if trade.vt_tradeid in self.trade_ids:
            return
        self.trade_ids.add(trade.vt_tradeid)

        self.insertRow(0)

        reference: str = getattr(trade, "reference", "")
        reference_cell: BaseCell = BaseCell(reference, trade)
        tradeid_cell: BaseCell = BaseCell(trade.tradeid, trade)
        orderid_cell: BaseCell = BaseCell(trade.orderid, trade)
        mark_cell: BaseCell = BaseCell(
            get_order_mark(self.main_engine, trade.vt_orderid),
            trade
        )
        symbol_cell: BaseCell = BaseCell(trade.symbol, trade)
        name_cell: BaseCell = BaseCell(
            get_contract_name(self.main_engine, trade.vt_symbol),
            trade
        )
        exchange_cell: EnumCell = EnumCell(trade.exchange, trade)
        direction_cell: DirectionCell = DirectionCell(trade.direction, trade)
        offset_cell: EnumCell = EnumCell(trade.offset, trade)
        price_cell: BaseCell = BaseCell(trade.price, trade)
        volume_cell: BaseCell = BaseCell(trade.volume, trade)
        datetime_cell: TradeTimeCell = TradeTimeCell(trade.datetime, trade)
        gateway_cell: BaseCell = BaseCell(trade.gateway_name, trade)

        self.setItem(0, 0, reference_cell)
        self.setItem(0, 1, tradeid_cell)
        self.setItem(0, 2, orderid_cell)
        self.setItem(0, 3, symbol_cell)
        self.setItem(0, 4, name_cell)
        self.setItem(0, 5, exchange_cell)
        self.setItem(0, 6, direction_cell)
        self.setItem(0, 7, offset_cell)
        self.setItem(0, 8, price_cell)
        self.setItem(0, 9, volume_cell)
        self.setItem(0, 10, datetime_cell)
        self.setItem(0, 11, gateway_cell)
        self.setItem(0, 12, mark_cell)

        self.update_row_visible(0)

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
