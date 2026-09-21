from bisect import bisect_left
from collections import OrderedDict
from copy import copy
from datetime import datetime
from typing import Any

import pyqtgraph as pg

from vnpy.chart.base import (
    AXIS_WIDTH,
    BLACK_COLOR,
    CURSOR_COLOR,
    GREY_COLOR,
    NORMAL_FONT,
    WHITE_COLOR
)
from vnpy.trader.ui import QtWidgets, QtCore, QtGui


# 曲线配色：黑底高对比度
CURVE_COLORS: list[str] = [
    "#ffe94b",
    "#69f0ae",
    "#ff9100",
    "#b388ff",
    "#40c4ff",
    "#ff80ab",
    "#c6ff00",
    "#1de9b6",
    "#8c9eff",
    "#ffd54f"
]

# 盈亏配色：与持仓表格保持一致（红涨绿跌）
PROFIT_COLOR = "#ff4b4b"
LOSS_COLOR = "#00e676"
FLAT_COLOR = "#9e9e9e"

METRIC_PNL = "pnl"
METRIC_RETURN = "return"


def date_to_timestamp(date_str: str) -> float:
    """日期字符串转换为秒级时间戳"""
    return datetime.strptime(date_str, "%Y-%m-%d").timestamp()


def pnl_color(value: float) -> str:
    """盈亏数值对应的颜色"""
    if value > 0:
        return PROFIT_COLOR
    if value < 0:
        return LOSS_COLOR
    return FLAT_COLOR


def format_value(value: float, metric: str) -> str:
    """指标数值格式化"""
    if metric == METRIC_RETURN:
        return f"{value * 100:.2f}%"
    return f"{value:,.2f}"


def fill_brush(color: str, alpha: int = 40) -> QtGui.QBrush:
    """曲线下方填充用的半透明画刷"""
    red, green, blue, _ = QtGui.QColor(color).getRgb()
    return pg.mkBrush(red, green, blue, alpha)


class ResultAxisItem(pg.AxisItem):
    """数值Y轴：按当前指标显示金额或百分比"""

    percent: bool = False

    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
        """"""
        if self.percent:
            return [f"{value * 100:.2f}%" for value in values]
        return super().tickStrings(values, scale, spacing)


class DateTickAxisItem(pg.DateAxisItem):
    """X轴：刻度固定显示为 yyyyMMdd

    pg.DateAxisItem 会随缩放把格式换成 %Y/%b/%d/%H:%M，这里统一成 8 位日期；
    同时把各缩放档位的示例文本也换成 8 位日期——示例文本是 pyqtgraph 估算
    标签宽度的依据，不改的话仍按 "YYYY"/"MMM" 估算密度，刻度会挤在一起重叠。
    """

    DATE_FORMAT: str = "%Y%m%d"
    EXAMPLE_TEXT: str = "20260101"

    def __init__(self, orientation: str = "bottom", **kwargs: Any) -> None:
        """"""
        super().__init__(orientation=orientation, **kwargs)

        # zoomLevels 里的对象是模块级共享的，先复制再改示例文本，避免影响其他图表
        levels: OrderedDict = OrderedDict()
        for density, zoom_level in self.zoomLevels.items():
            level = copy(zoom_level)
            level.exampleText = self.EXAMPLE_TEXT
            levels[density] = level
        self.zoomLevels = levels

    def tickStrings(self, values: list[float], scale: float, spacing: float) -> list[str]:
        """"""
        strings: list[str] = []

        for value in values:
            try:
                strings.append(datetime.fromtimestamp(value).strftime(self.DATE_FORMAT))
            except (OverflowError, OSError, ValueError):
                strings.append("")

        return strings


class CurveSample(pg.ItemSample):
    """图例里的示例图标：只画一条颜色线，不画填充三角形

    pyqtgraph 的 ItemSample 在曲线带 fillLevel/fillBrush 时会额外画一个三角形，
    单条曲线显示填充时图例上就会出现三角形，这里改成纯颜色标识。
    """

    def paint(self, p: QtGui.QPainter, *args: Any) -> None:
        """"""
        pen: QtGui.QPen = pg.mkPen(self.item.opts["pen"])
        color: QtGui.QColor = pen.color()

        p.setPen(pen)
        p.drawLine(0, 10, 20, 10)

        p.setPen(pg.mkPen(color))
        p.setBrush(pg.mkBrush(color))
        p.drawEllipse(QtCore.QPointF(10, 10), 4, 4)


class PortfolioChart(QtWidgets.QWidget):
    """组合累计盈亏曲线：支持多组合对比与十字光标读数"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        """"""
        super().__init__(parent)

        # 不能叫 self.metric：QPaintDevice 有个虚函数就叫 metric()，同名的实例属性
        # 会把它盖掉，Qt 一旦调到该虚函数就会抛 "'str' object is not callable"
        self.current_metric: str = METRIC_PNL

        self.history: dict[str, dict[str, dict[str, float]]] = {}
        self.capitals: dict[str, float] = {}

        # 曲线绘图数据：reference -> {日期列表, x坐标, 数值, 累计盈亏}
        self.curve_data: dict[str, dict[str, Any]] = {}
        self.curve_colors: dict[str, str] = {}
        self.curve_items: dict[str, pg.PlotDataItem] = {}

        # 需要显示在图表中的组合，None 表示全部显示
        self.visible_references: set[str] | None = None

        self.sorted_dates: list[str] = []
        self.sorted_xs: list[float] = []

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        # 与市场情绪模块一致的风格：黑底、右侧数值轴、灰轴白字
        self.plot: pg.PlotWidget = pg.PlotWidget(
            axisItems={
                "right": ResultAxisItem(orientation="right"),
                "bottom": DateTickAxisItem(orientation="bottom")
            },
            background=BLACK_COLOR
        )

        plot_item: pg.PlotItem = self.plot.getPlotItem()
        plot_item.showGrid(x=True, y=True, alpha=0.2)
        plot_item.setMenuEnabled(False)
        plot_item.hideButtons()
        plot_item.hideAxis("left")
        plot_item.showAxis("right")
        plot_item.setLabel("bottom", "日期")
        plot_item.getViewBox().setMouseEnabled(x=True, y=False)
        plot_item.getViewBox().setBackgroundColor(BLACK_COLOR)

        for name in ("right", "bottom"):
            axis: pg.AxisItem = plot_item.getAxis(name)
            axis.setPen(pg.mkPen(color=GREY_COLOR, width=AXIS_WIDTH))
            axis.setTextPen(pg.mkPen(color=WHITE_COLOR))
            axis.tickFont = NORMAL_FONT
            axis.setStyle(tickTextOffset=6)

        self.value_axis: ResultAxisItem = plot_item.getAxis("right")

        # pen=None：LegendItem.paint 会按这里的 pen 画外框，给 NoPen 就不画边框了
        self.legend: pg.LegendItem = self.plot.addLegend(
            offset=(10, 10),
            labelTextColor=WHITE_COLOR,
            brush=pg.mkBrush(0, 0, 0, 120),
            pen=None,
            sampleType=CurveSample
        )

        crosshair_pen: QtGui.QPen = pg.mkPen(WHITE_COLOR, width=1)
        self.vline: pg.InfiniteLine = pg.InfiniteLine(angle=90, movable=False, pen=crosshair_pen)
        self.hline: pg.InfiniteLine = pg.InfiniteLine(angle=0, movable=False, pen=crosshair_pen)
        for line in (self.vline, self.hline):
            line.setZValue(2)
            line.setVisible(False)
            self.plot.addItem(line, ignoreBounds=True)

        self.hover_scatter: pg.ScatterPlotItem = pg.ScatterPlotItem(
            symbol="o",
            size=11,
            pen=pg.mkPen(BLACK_COLOR, width=1)
        )
        self.hover_scatter.setZValue(2)
        self.plot.addItem(self.hover_scatter)

        self.info_text: pg.TextItem = pg.TextItem(
            anchor=(0, 1),
            color=BLACK_COLOR,
            fill=CURSOR_COLOR,
            border=CURSOR_COLOR
        )
        self.info_text.setFont(NORMAL_FONT)
        self.info_text.setZValue(3)
        self.info_text.setVisible(False)
        self.plot.addItem(self.info_text, ignoreBounds=True)

        self.plot.scene().sigMouseMoved.connect(self.process_mouse_moved)
        self.plot.viewport().installEventFilter(self)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.addWidget(self.plot)
        self.setLayout(vbox)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        """鼠标移出图表区域时隐藏十字光标"""
        if event.type() == QtCore.QEvent.Type.Leave:
            self.hide_hover()

        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_metric(self, metric: str) -> None:
        """切换显示的指标：累计盈亏 / 累计收益率"""
        if metric == self.current_metric:
            return

        self.current_metric = metric
        self.hide_hover()
        self.redraw()

        # 强制Y轴刻度重新生成（金额 / 百分比）
        self.value_axis.picture = None
        self.value_axis.update()

    def set_visible_references(self, references: set[str]) -> None:
        """设置需要显示的组合（None 表示全部显示）"""
        if self.visible_references == references:
            return

        self.visible_references = references
        self.redraw()

    def is_visible(self, reference: str) -> bool:
        """该组合当前是否显示在图表中"""
        if self.visible_references is None:
            return True
        return reference in self.visible_references

    def update_data(
        self,
        history: dict[str, dict[str, dict[str, float]]],
        capitals: dict[str, float],
        visible_references: set[str] | None = None
    ) -> None:
        """更新历史盈亏快照、初始资金与需要显示的组合"""
        self.history = history
        self.capitals = capitals
        if visible_references is not None:
            self.visible_references = visible_references
        self.redraw()

    def reset_view(self) -> None:
        """恢复自动缩放"""
        self.plot.enableAutoRange()

    # ------------------------------------------------------------------
    # 绘图逻辑
    # ------------------------------------------------------------------
    def redraw(self) -> None:
        """"""
        self.build_curve_data()
        self.update_curves()

        self.value_axis.percent = self.current_metric == METRIC_RETURN

    def build_curve_data(self) -> None:
        """把历史快照整理为绘图用的坐标序列"""
        self.curve_data = {}

        dates: set[str] = set()

        for reference, days in self.history.items():
            if not days:
                continue

            capital: float = self.capitals.get(reference, 0)

            # 没填初始资金时收益率无意义，不画成一条0%平线误导人
            if self.current_metric == METRIC_RETURN and not capital:
                continue

            date_list: list[str] = []
            xs: list[float] = []
            values: list[float] = []
            cum_pnl: dict[str, float] = {}

            cum: float = 0
            for date_str in sorted(days.keys()):
                cum += days[date_str]["total_pnl"]

                date_list.append(date_str)
                xs.append(date_to_timestamp(date_str))
                cum_pnl[date_str] = cum

                if self.current_metric == METRIC_RETURN:
                    values.append(cum / capital)
                else:
                    values.append(cum)

            self.curve_data[reference] = {
                "dates": date_list,
                "xs": xs,
                "values": values,
                "cum_pnl": cum_pnl
            }
            dates.update(date_list)

        self.sorted_dates = sorted(dates)
        self.sorted_xs = [date_to_timestamp(date_str) for date_str in self.sorted_dates]

    def get_curve_color(self, reference: str) -> str:
        """给组合分配固定颜色：同一组合的颜色不因勾选其他组合而变化"""
        color: str | None = self.curve_colors.get(reference, None)
        if color:
            return color

        used: set[str] = set(self.curve_colors.values())
        for candidate in CURVE_COLORS:
            if candidate not in used:
                self.curve_colors[reference] = candidate
                return candidate

        color = CURVE_COLORS[len(self.curve_colors) % len(CURVE_COLORS)]
        self.curve_colors[reference] = color
        return color

    def update_curves(self) -> None:
        """"""
        visible_data: dict[str, dict[str, Any]] = {
            reference: data
            for reference, data in self.curve_data.items()
            if self.is_visible(reference)
        }

        # 移除已隐藏或已消失的组合（颜色保留，重新勾选时颜色不变）
        for reference in list(self.curve_items.keys()):
            if reference in visible_data:
                continue

            self.plot.removeItem(self.curve_items.pop(reference))
            self.legend.removeItem(reference)

        # 只显示一条曲线时，按情绪模块的做法给曲线下方加半透明填充
        single: bool = len(visible_data) == 1

        for reference, data in visible_data.items():
            color: str = self.get_curve_color(reference)

            item: pg.PlotDataItem | None = self.curve_items.get(reference, None)
            if not item:
                item = self.plot.plot([], [], name=reference)
                self.curve_items[reference] = item

            values: list[float] = data["values"]
            fill_level: float | None = None
            if single and values:
                fill_level = min(values)

            item.setData(
                data["xs"],
                values,
                pen=pg.mkPen(color, width=2),
                symbol="o",
                symbolSize=5,
                symbolBrush=color,
                symbolPen=None,
                fillLevel=fill_level,
                brush=fill_brush(color)
            )

    # ------------------------------------------------------------------
    # 十字光标
    # ------------------------------------------------------------------
    def process_mouse_moved(self, pos: QtCore.QPointF) -> None:
        """"""
        if not self.curve_data or not self.sorted_dates:
            return

        if not self.plot.sceneBoundingRect().contains(pos):
            self.hide_hover()
            return

        view_box: pg.ViewBox = self.plot.getViewBox()
        point: QtCore.QPointF = view_box.mapSceneToView(pos)

        date_str: str = self.sorted_dates[self.find_nearest_date_index(point.x())]
        x: float = date_to_timestamp(date_str)

        values: dict[str, float] = {}
        for reference, data in self.curve_data.items():
            if not self.is_visible(reference):
                continue

            cum_pnl: dict[str, float] = data["cum_pnl"]
            if date_str in cum_pnl:
                values[reference] = data["values"][data["dates"].index(date_str)]

        if not values:
            self.hide_hover()
            return

        # 光标就近吸附到某条曲线
        active: str = min(values, key=lambda reference: abs(values[reference] - point.y()))
        y: float = values[active]

        self.vline.setPos(x)
        self.hline.setPos(y)
        self.vline.setVisible(True)
        self.hline.setVisible(True)

        spots: list[dict[str, Any]] = []
        for reference, value in values.items():
            color: str = self.curve_colors.get(reference, FLAT_COLOR)
            spots.append({
                "pos": (x, value),
                "brush": pg.mkBrush(color),
                "pen": pg.mkPen(color, width=1),
                "size": 11 if reference == active else 8
            })
        self.hover_scatter.setData(spots=spots)

        self.update_info_text(x, y, date_str, values)

    def find_nearest_date_index(self, x: float) -> int:
        """"""
        index: int = bisect_left(self.sorted_xs, x)

        if index <= 0:
            return 0
        if index >= len(self.sorted_xs):
            return len(self.sorted_xs) - 1

        before: float = self.sorted_xs[index - 1]
        after: float = self.sorted_xs[index]
        return index if (after - x) < (x - before) else index - 1

    def update_info_text(
        self,
        x: float,
        y: float,
        date_str: str,
        values: dict[str, float]
    ) -> None:
        """"""
        lines: list[str] = [f"<b>{date_str}</b>"]

        for reference, value in values.items():
            lines.append(f"{reference}  {format_value(value, self.current_metric)}")

        self.info_text.setHtml("<br>".join(lines))
        self.info_text.setPos(x, y)
        self.info_text.setVisible(True)

    def hide_hover(self) -> None:
        """"""
        self.vline.setVisible(False)
        self.hline.setVisible(False)
        self.info_text.setVisible(False)
        self.hover_scatter.setData(spots=[])


class PortfolioSummaryTable(QtWidgets.QTableWidget):
    """组合盈亏汇总：初始资金 / 当日盈亏 / 累计盈亏 / 累计收益率 / 曲线显示"""

    signal_capital: QtCore.Signal = QtCore.Signal(str, float)
    signal_visible: QtCore.Signal = QtCore.Signal()

    LABELS: list[str] = ["组合", "初始资金", "当日盈亏", "累计盈亏", "累计收益率", "显示"]

    # 默认显示在曲线上的组合数量
    DEFAULT_VISIBLE: int = 3
    CHECK_COLUMN: int = 5

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        """"""
        super().__init__(parent)

        self.capital_spins: dict[str, QtWidgets.QDoubleSpinBox] = {}
        self.visible_checks: dict[str, QtWidgets.QCheckBox] = {}
        self.row_map: dict[str, int] = {}

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setColumnCount(len(self.LABELS))
        self.setHorizontalHeaderLabels(self.LABELS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(self.SelectionBehavior.SelectRows)

        # 所有列平分宽度：表格横向铺满，右侧不留空白
        header: QtWidgets.QHeaderView = self.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)

    def update_data(
        self,
        history: dict[str, dict[str, dict[str, float]]],
        capitals: dict[str, float],
        date_str: str
    ) -> None:
        """"""
        for reference in sorted(history.keys()):
            if reference not in self.row_map:
                self.add_reference(reference, capitals.get(reference, 0))

        for reference, row in self.row_map.items():
            days: dict[str, dict[str, float]] = history.get(reference, {})
            day_pnl: float = days.get(date_str, {}).get("total_pnl", 0.0)
            cum_pnl: float = sum(day["total_pnl"] for day in days.values())

            capital: float = capitals.get(reference, 0)
            cum_return: float = cum_pnl / capital if capital else 0.0

            spin: QtWidgets.QDoubleSpinBox = self.capital_spins[reference]
            if not spin.hasFocus() and spin.value() != capital:
                spin.setValue(capital)

            self.set_value(row, 2, f"{day_pnl:,.2f}", day_pnl)
            self.set_value(row, 3, f"{cum_pnl:,.2f}", cum_pnl)
            self.set_value(
                row,
                4,
                f"{cum_return * 100:.2f}%" if capital else "-",
                cum_return
            )

    def add_reference(self, reference: str, capital: float) -> None:
        """"""
        row: int = self.rowCount()
        self.insertRow(row)

        item: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(reference)
        item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setItem(row, 0, item)

        spin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
        spin.setRange(0, 1e12)
        spin.setDecimals(2)
        spin.setGroupSeparatorShown(True)
        spin.setSuffix(" 元")
        spin.setMaximumHeight(26)
        spin.setToolTip("填写该组合的初始资金，用于计算累计收益率（填 0 则不显示收益率曲线）")
        spin.setValue(capital)
        spin.editingFinished.connect(
            lambda reference=reference, spin=spin: self.emit_capital(reference, spin.value())
        )
        self.setCellWidget(row, 1, spin)

        check: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
        check.setChecked(self.visible_count() < self.DEFAULT_VISIBLE)
        check.setToolTip("勾选后在下方的收益曲线中显示")
        check.stateChanged.connect(lambda _: self.signal_visible.emit())

        holder: QtWidgets.QWidget = QtWidgets.QWidget()
        holder_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(check, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setCellWidget(row, self.CHECK_COLUMN, holder)

        for column in range(2, self.CHECK_COLUMN):
            self.set_value(row, column, "-", 0.0)

        self.capital_spins[reference] = spin
        self.visible_checks[reference] = check
        self.row_map[reference] = row

    def emit_capital(self, reference: str, capital: float) -> None:
        """提交初始资金

        控件销毁过程中 editingFinished 仍可能触发，此时 C++ 对象已失效，
        直接 emit 会抱 RuntimeError，这里兼作兜底。
        """
        try:
            self.signal_capital.emit(reference, capital)
        except RuntimeError:
            pass

    def visible_count(self) -> int:
        """当前勾选显示曲线的组合数量"""
        return sum(1 for check in self.visible_checks.values() if check.isChecked())

    def get_visible_references(self) -> set[str]:
        """获取勾选了显示曲线的组合"""
        return {
            reference
            for reference, check in self.visible_checks.items()
            if check.isChecked()
        }

    def set_value(self, row: int, column: int, text: str, value: float) -> None:
        """"""
        item: QtWidgets.QTableWidgetItem | None = self.item(row, column)
        if not item:
            item = QtWidgets.QTableWidgetItem()
            item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, column, item)

        item.setText(text)
        item.setForeground(QtGui.QBrush(QtGui.QColor(pnl_color(value))))
