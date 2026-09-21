from typing import TYPE_CHECKING
from datetime import date, datetime, timedelta

from vnpy.trader.object import TickData, TradeData, ContractData
from vnpy.trader.constant import Direction

if TYPE_CHECKING:
    from .engine import PortfolioEngine


def get_trading_day(now: datetime | None = None) -> str:
    """取交易日：按**自然日**，遇周六日顺延到周一（没有节假日日历，长假只能近似）

    本地以 A 股为主，而 A 股没有夜盘，所以不再把 20:00 之后算作下一个交易日：
    那条期货夜盘口径会让当天 20:00 过后的“数据日期”跳到次日（同时把当日盈亏
    也计到次日名下）。若以后要跑期货夜盘，需要把 20:00 换日的逻辑加回来，
    否则一夜的行情会被拆成两个快照点。
    """
    day: date = (now or datetime.now()).date()

    while day.weekday() >= 5:
        day += timedelta(days=1)

    return day.strftime("%Y-%m-%d")


class ContractResult:
    """"""

    def __init__(
        self,
        engine: "PortfolioEngine",
        reference: str,
        vt_symbol: str,
        open_pos: float = 0
    ) -> None:
        """"""
        super().__init__()

        self.engine: PortfolioEngine = engine

        self.reference: str = reference
        self.vt_symbol: str = vt_symbol

        # 本地以 A 股为主（无融券），仓位为负没有意义：老的存档里若存了负数，一律归零
        self.open_pos: float = max(open_pos, 0)
        self.last_pos: float = self.open_pos

        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0

        self.trades: dict[str, TradeData] = {}
        self.new_trades: list[TradeData] = []

        self.long_volume: float = 0
        self.short_volume: float = 0
        self.long_cost: float = 0
        self.short_cost: float = 0

    def update_trade(self, trade: TradeData) -> None:
        """"""
        # 过滤重复成交
        if trade.vt_tradeid in self.trades:
            return
        self.trades[trade.vt_tradeid] = trade
        self.new_trades.append(trade)

        if trade.direction == Direction.LONG:
            self.last_pos += trade.volume
        else:
            self.last_pos -= trade.volume

        # 持仓量不会为负（A 股无融券）。变负说明买入没进账——例如建仓发生在本模块开始
        # 记账之前，卖出量大于本模块记录的买入量（成交里没带 reference 的也会被丢弃）。
        # 这里夹到 0，避免持仓明细里出现负数；空头成交量仍如实累计，可据此看出差额。
        if self.last_pos < 0:
            self.last_pos = 0
    def roll_to_next_day(self) -> None:
        """自然日切换：收盘仓位滚动为新的开盘仓位，并清空当日累计"""
        self.open_pos = self.last_pos

        self.trading_pnl = 0
        self.holding_pnl = 0
        self.total_pnl = 0

        # 成交缓存按日清空，避免次日重复计算成交盈亏
        self.trades.clear()
        self.new_trades.clear()

        self.long_volume = 0
        self.short_volume = 0
        self.long_cost = 0
        self.short_cost = 0
    def calculate_pnl(self) -> None:
        """"""
        vt_symbol: str = self.vt_symbol

        contract: ContractData | None = self.engine.get_contract(vt_symbol)
        tick: TickData | None = self.engine.get_tick(vt_symbol)
        if not contract or not tick:
            return

        last_price: float = tick.last_price
        size: float = contract.size

        # 计算新成交额
        for trade in self.new_trades:
            trade_volume: float = trade.volume
            trade_cost: float = trade.price * trade_volume * size

            if trade.direction == Direction.LONG:
                self.long_cost += trade_cost
                self.long_volume += trade_volume
            else:
                self.short_cost += trade_cost
                self.short_volume += trade_volume

        self.new_trades.clear()

        # 计算成交利润
        long_value: float = self.long_volume * last_price * size
        long_pnl: float = long_value - self.long_cost

        shrot_value: float = self.short_volume * last_price * size
        short_pnl: float = self.short_cost - shrot_value

        self.trading_pnl = long_pnl + short_pnl

        # 计算未实现利润和总利润
        #
        # 基准用 tick.pre_close（昨收盘）：vnpy 的 TickData 没有昨结算价字段，
        # vnpy_ctp 也只映射了 PreClosePrice，而国内期货盯市应以昨结算价为基准，
        # 因此期货的持仓盈亏会有（昨结算-昨收）*持仓*乘数 的偏差，无数据源可修。
        self.holding_pnl = (last_price - tick.pre_close) * self.open_pos * size
        self.total_pnl = self.holding_pnl + self.trading_pnl

    def get_data(self) -> dict:
        """获取数据字典"""
        data: dict = {
            "reference": self.reference,
            "vt_symbol": self.vt_symbol,
            "open_pos": self.open_pos,
            "last_pos": self.last_pos,
            "trading_pnl": self.trading_pnl,
            "holding_pnl": self.holding_pnl,
            "total_pnl": self.total_pnl,
            "long_volume": self.long_volume,
            "short_volume": self.short_volume,
            "long_cost": self.long_cost,
            "short_cost": self.short_cost
        }
        return data


class PortfolioResult:
    """"""

    def __init__(self, reference: str) -> None:
        """"""
        super().__init__()

        self.reference: str = reference
        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0

        # 历史累计盈亏（不含当日）与初始资金
        self.history_pnl: float = 0
        self.capital: float = 0

    def clear_pnl(self) -> None:
        """"""
        self.trading_pnl = 0
        self.holding_pnl = 0
        self.total_pnl = 0

    @property
    def cum_pnl(self) -> float:
        """累计总盈亏（历史累计 + 当日）"""
        return self.history_pnl + self.total_pnl

    @property
    def cum_return(self) -> float:
        """累计收益率，未设置初始资金时返回0"""
        if not self.capital:
            return 0
        return self.cum_pnl / self.capital

    def get_data(self) -> dict:
        """获取数据字典"""
        data: dict = {
            "reference": self.reference,
            "trading_pnl": self.trading_pnl,
            "holding_pnl": self.holding_pnl,
            "total_pnl": self.total_pnl,
            "history_pnl": self.history_pnl,
            "capital": self.capital,
            "cum_pnl": self.cum_pnl,
            "cum_return": self.cum_return,
        }
        return data
