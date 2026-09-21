from typing import Any
from collections.abc import Callable

from vnpy.event import Event
from vnpy.trader.engine import (
    MainEngine,
    EventEngine,
    BaseEngine
)
from vnpy.trader.event import (
    EVENT_ORDER,
    EVENT_CONTRACT,
    EVENT_TIMER,
    EVENT_TRADE
)
from vnpy.trader.object import (
    ContractData,
    OrderData,
    TradeData,
    SubscribeRequest,
    TickData
)
from vnpy.trader.utility import load_json, save_json

from .base import ContractResult, PortfolioResult, get_trading_day


APP_NAME = "PortfolioManager"

EVENT_PM_CONTRACT = "ePmContract"
EVENT_PM_PORTFOLIO = "ePmPortfolio"
EVENT_PM_TRADE = "ePmTrade"
EVENT_PM_HISTORY = "ePmHistory"


class PortfolioEngine(BaseEngine):
    """"""
    setting_filename: str = "portfolio_manager_setting.json"
    data_filename: str = "portfolio_manager_data.json"
    order_filename: str = "portfolio_manager_order.json"
    history_filename: str = "portfolio_manager_history.json"

    # 历史快照落盘间隔（秒），避免每个刷新周期都写磁盘
    history_save_interval: int = 300

    # 组合默认初始资金（未单独设置时用这个算收益率）
    default_capital: float = 500000

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__(main_engine, event_engine, APP_NAME)

        self.get_tick: Callable[[str], TickData | None] = self.main_engine.get_tick
        self.get_contract: Callable[[str], ContractData | None] = self.main_engine.get_contract

        self.subscribed: set[str] = set()
        self.result_symbols: set[str] = set()
        self.order_reference_map: dict[str, str] = {}
        self.contract_results: dict[tuple[str, str], ContractResult] = {}
        self.portfolio_results: dict[str, PortfolioResult] = {}

        # 历史盈亏快照：reference -> 日期 -> 当日盈亏
        self.history: dict[str, dict[str, dict[str, float]]] = {}
        # 初始资金：reference -> 金额
        self.capitals: dict[str, float] = {}

        self.current_date: str = get_trading_day()
        self.history_save_seconds: int = 0
        # 是否已经算过一次盈亏：没算过就不能写快照，否则会把当日写成0
        self.pnl_computed: bool = False

        self.timer_count: int = 0
        self.timer_interval: int = 5

        self.load_setting()
        self.load_order()
        self.load_history()
        self.load_data()
        self.register_event()

    def register_event(self) -> None:
        """"""
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        self.event_engine.register(EVENT_CONTRACT, self.process_contract_event)

    def process_order_event(self, event: Event) -> None:
        """"""
        order: OrderData = event.data

        if order.vt_orderid not in self.order_reference_map:
            self.order_reference_map[order.vt_orderid] = order.reference
        else:
            order.reference = self.order_reference_map[order.vt_orderid]

    def process_trade_event(self, event: Event) -> None:
        """"""
        trade: TradeData = event.data

        reference: str = self.order_reference_map.get(trade.vt_orderid, "")
        if not reference:
            return

        vt_symbol: str = trade.vt_symbol
        key: tuple[str, str] = (reference, vt_symbol)

        contract_result: ContractResult | None = self.contract_results.get(key, None)
        if not contract_result:
            contract_result = ContractResult(self, reference, vt_symbol)
            self.contract_results[key] = contract_result

        contract_result.update_trade(trade)

        # 添加成交数据
        trade.reference = reference
        self.event_engine.put(Event(EVENT_PM_TRADE, trade))

        # 有持仓的合约才需要订阅tick数据
        if self.has_position(vt_symbol):
            self.result_symbols.add(vt_symbol)
            self.subscribe_symbol(vt_symbol)
        else:
            self.result_symbols.discard(vt_symbol)

    def process_timer_event(self, event: Event) -> None:
        """"""
        self.timer_count += 1
        if self.timer_count < self.timer_interval:
            return
        self.timer_count = 0

        # 跨交易日时归档前一日结果并滚动仓位
        self.check_date_change()

        for portfolio_result in self.portfolio_results.values():
            portfolio_result.clear_pnl()

        for contract_result in self.contract_results.values():
            contract_result.calculate_pnl()

            portfolio_result = self.get_portfolio_result(contract_result.reference)
            portfolio_result.trading_pnl += contract_result.trading_pnl
            portfolio_result.holding_pnl += contract_result.holding_pnl
            portfolio_result.total_pnl += contract_result.total_pnl

            event = Event(EVENT_PM_CONTRACT, contract_result.get_data())
            self.event_engine.put(event)

        # 把当日盈亏写入历史快照，并计算历史累计
        self.pnl_computed = True
        self.record_daily_result(self.current_date)
        self.save_history_periodically()

        for portfolio_result in self.portfolio_results.values():
            portfolio_result.history_pnl = self.get_history_pnl(
                portfolio_result.reference,
                self.current_date
            )
            portfolio_result.capital = self.get_capital(portfolio_result.reference)

            event = Event(EVENT_PM_PORTFOLIO, portfolio_result.get_data())
            self.event_engine.put(event)

        event = Event(EVENT_PM_HISTORY, self.get_history_data())
        self.event_engine.put(event)

    def process_contract_event(self, event: Event) -> None:
        """"""
        contract: ContractData = event.data
        if contract.vt_symbol not in self.result_symbols:
            return

        self.subscribe_symbol(contract.vt_symbol)

    def subscribe_symbol(self, vt_symbol: str) -> None:
        """订阅合约行情，自动去重"""
        if vt_symbol in self.subscribed:
            return

        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
        if not contract:
            return

        req: SubscribeRequest = SubscribeRequest(contract.symbol, contract.exchange)
        self.main_engine.subscribe(req, contract.gateway_name)

        # 记录已订阅：CTP等接口不支持退订，重复调用只会产生冗余请求
        self.subscribed.add(vt_symbol)

    def has_position(self, vt_symbol: str) -> bool:
        """检查合约是否还有持仓（同一合约可能同时被多个组合持有）"""
        return any(
            contract_result.last_pos
            for contract_result in self.contract_results.values()
            if contract_result.vt_symbol == vt_symbol
        )

    def load_data(self) -> None:
        """读取仓位存档；同一交易日重启时回放当日成交"""
        today: str = get_trading_day()
        data: dict = load_json(self.data_filename)

        date: str = data.pop("date", "")
        date_changed: bool = bool(date) and date != today

        for key, d in data.items():
            reference, vt_symbol = key.split(",", 1)

            # 跨交易日：把上一日收盘仓位作为今日开盘仓位
            pos: float = d["last_pos"] if date_changed else d["open_pos"]

            self.contract_results[(reference, vt_symbol)] = ContractResult(
                self,
                reference,
                vt_symbol,
                pos
            )

        if date_changed:
            self.save_data()
        else:
            # 当日重启：不回放的话，当日成交盈亏会从0重算，曲线偏低
            self.replay_trades()

        self.update_result_symbols()

    def replay_trades(self) -> None:
        """回放当日成交：重建当前仓位与成交成本（last_pos = open_pos + 当日成交）"""
        for trade in self.main_engine.get_all_trades():
            reference: str = self.order_reference_map.get(trade.vt_orderid, "")
            if not reference:
                continue

            key: tuple[str, str] = (reference, trade.vt_symbol)
            contract_result: ContractResult | None = self.contract_results.get(key, None)
            if not contract_result:
                contract_result = ContractResult(self, reference, trade.vt_symbol)
                self.contract_results[key] = contract_result

            contract_result.update_trade(trade)

    def update_result_symbols(self) -> None:
        """按当前仓位刷新需要订阅行情的合约集合"""
        self.result_symbols = {
            contract_result.vt_symbol
            for contract_result in self.contract_results.values()
            if contract_result.last_pos
        }

        # 合约信息可能还没加载，稍后由合约事件（process_contract_event）再订阅
        for vt_symbol in self.result_symbols:
            self.subscribe_symbol(vt_symbol)

    def save_data(self) -> None:
        """"""
        data: dict[str, Any] = {"date": get_trading_day()}

        for contract_result in self.contract_results.values():
            key: str = f"{contract_result.reference},{contract_result.vt_symbol}"
            data[key] = {
                "open_pos": contract_result.open_pos,
                "last_pos": contract_result.last_pos
            }

        save_json(self.data_filename, data)

    def load_setting(self) -> None:
        """"""
        setting: dict = load_json(self.setting_filename)
        if "timer_interval" in setting:
            self.timer_interval = setting["timer_interval"]
        if "capitals" in setting:
            self.capitals = {key: float(value) for key, value in setting["capitals"].items()}

    def save_setting(self) -> None:
        """"""
        setting: dict[str, Any] = {
            "timer_interval": self.timer_interval,
            "capitals": self.capitals
        }
        save_json(self.setting_filename, setting)

    def set_capital(self, reference: str, capital: float) -> None:
        """设置组合的初始资金，用于计算累计收益率"""
        self.capitals[reference] = capital

        portfolio_result: PortfolioResult | None = self.portfolio_results.get(reference, None)
        if portfolio_result:
            portfolio_result.capital = capital

        self.save_setting()

    def get_capital(self, reference: str) -> float:
        """获取组合的初始资金，未单独设置时用默认值"""
        return self.capitals.get(reference, self.default_capital)

    def load_history(self) -> None:
        """"""
        data: dict = load_json(self.history_filename)
        if not data:
            return

        self.history = data.get("history", {})

    def save_history(self) -> None:
        """"""
        data: dict[str, Any] = {"history": self.history}
        save_json(self.history_filename, data)

    def save_history_periodically(self) -> None:
        """按时间间隔落盘历史快照与仓位，避免崩溃丢当日数据"""
        self.history_save_seconds += self.timer_interval
        if self.history_save_seconds < self.history_save_interval:
            return

        self.history_save_seconds = 0
        self.save_history()
        self.save_data()

    def record_daily_result(self, date_str: str) -> None:
        """记录指定日期的盈亏快照，同一日期重复调用会覆盖（未计算过盈亏时跳过）"""
        if not self.pnl_computed:
            return

        for portfolio_result in self.portfolio_results.values():
            days: dict[str, dict[str, float]] = self.history.setdefault(
                portfolio_result.reference,
                {}
            )
            days[date_str] = {
                "trading_pnl": portfolio_result.trading_pnl,
                "holding_pnl": portfolio_result.holding_pnl,
                "total_pnl": portfolio_result.total_pnl
            }

    def get_history_pnl(self, reference: str, exclude_date: str = "") -> float:
        """获取组合的历史累计盈亏，可排除指定日期（当日盈亏单独计算）"""
        days: dict[str, dict[str, float]] = self.history.get(reference, {})
        return sum(
            day_data["total_pnl"]
            for date_str, day_data in days.items()
            if date_str != exclude_date
        )

    def get_history_data(self) -> dict[str, Any]:
        """获取历史快照数据（三层都拷一份，界面跳线程读取时不会读到半成品）"""
        references: set[str] = set(self.history) | set(self.capitals)
        references.update(self.portfolio_results.keys())

        data: dict[str, Any] = {
            "date": self.current_date,
            # 带上默认值，界面与曲线拿到的初始资金保持一致
            "capitals": {reference: self.get_capital(reference) for reference in references},
            "history": {
                reference: {
                    date_str: dict(day_data)
                    for date_str, day_data in list(days.items())
                }
                for reference, days in list(self.history.items())
            }
        }
        return data

    def check_date_change(self) -> None:
        """检测交易日切换：归档前一日结果，并把收盘仓位滚动为新的开盘仓位

        交易日按国内期货惯例：20:00 之后的夜盘归属下一个交易日，
        因此一夜的行情不会被拆成两个快照点。
        """
        today: str = get_trading_day()
        if today == self.current_date:
            return

        self.record_daily_result(self.current_date)
        self.current_date = today

        for contract_result in self.contract_results.values():
            contract_result.roll_to_next_day()

        self.save_data()
        self.save_history()

    def load_order(self) -> None:
        """"""
        order_data: dict = load_json(self.order_filename)

        date: str = order_data.get("date", "")
        today: str = get_trading_day()
        if date == today:
            self.order_reference_map = order_data["data"]

    def save_order(self) -> None:
        """"""
        order_data: dict[str, Any] = {
            "date": get_trading_day(),
            "data": self.order_reference_map
        }
        save_json(self.order_filename, order_data)

    def close(self) -> None:
        """"""
        self.record_daily_result(self.current_date)

        self.save_setting()
        self.save_data()
        self.save_order()
        self.save_history()

    def get_portfolio_result(self, reference: str) -> PortfolioResult:
        """"""
        portfolio_result: PortfolioResult | None = self.portfolio_results.get(reference, None)
        if not portfolio_result:
            portfolio_result = PortfolioResult(reference)
            self.portfolio_results[reference] = portfolio_result
        return portfolio_result

    def set_timer_interval(self, interval: int) -> None:
        """"""
        self.timer_interval = interval

    def get_timer_interval(self) -> int:
        """"""
        return self.timer_interval

    def get_all_reference_trades(self) -> list[TradeData]:
        """获取当日所有带组合标记的成交（按时间升序）"""
        trades: list[TradeData] = []

        for trade in self.main_engine.get_all_trades():
            reference: str = self.order_reference_map.get(trade.vt_orderid, "")
            if not reference:
                continue

            trade.reference = reference
            trades.append(trade)

        trades.sort(key=lambda trade: trade.datetime)
        return trades