from datetime import date
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
from .database import TradeRepository, build_trade_row
from .settings import SqlSettings, parse_sql_settings


APP_NAME = "PortfolioManager"

# SqlApp 的引擎名（vnpy_sqlapp.APP_NAME）。不硬依赖该包，取不到引擎时整体降级。
try:
    from vnpy_sqlapp import APP_NAME as SQL_APP_NAME
except ImportError:
    SQL_APP_NAME = "SqlApp"

EVENT_PM_CONTRACT = "ePmContract"
EVENT_PM_PORTFOLIO = "ePmPortfolio"
# 负载是成交行数据 dict（键见 database.TRADE_COLUMNS）
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

        # 成交记录入库配置与仓储；未加载 SqlApp 或建表失败时为 None（降级为内存模式）
        self.sql_settings: SqlSettings = SqlSettings()
        self.trade_repository: TradeRepository | None = None

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
        # 必须在注册事件之前建好仓储：注册之后到达的成交会直接入库
        self.init_trade_repository()
        self.register_event()

    def write_log(self, msg: str) -> None:
        """写日志到主引擎（BaseEngine 自身没有 write_log）"""
        self.main_engine.write_log(msg, self.engine_name)

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

        # 落库：失败只记日志，不影响内存记账（仓位与盈亏仍以内存为准）
        trade.reference = reference
        row: dict[str, Any] = self.make_trade_row(trade, reference)
        if self.trade_repository:
            self.trade_repository.save_row(row)

        # 推送成交数据（行数据，界面与入库共用同一份字段口径）
        self.event_engine.put(Event(EVENT_PM_TRADE, row))

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

    # -- 成交记录入库 -----------------------------------------------------

    def init_trade_repository(self) -> None:
        """连接 SqlApp 并准备成交记录表；任何一步失败都降级为纯内存模式"""
        if not self.sql_settings.enabled:
            self.write_log("成交记录入库已关闭（设置文件中的 sql.enabled = false）")
            return

        sql_engine: Any = self.main_engine.get_engine(SQL_APP_NAME)
        if sql_engine is None:
            self.write_log("未加载 SqlApp，成交记录只保留在内存中，无法查询历史成交")
            return

        repository: TradeRepository = TradeRepository(
            sql_engine,
            self.sql_settings,
            self.write_log
        )

        if self.sql_settings.auto_create and not repository.ensure_table():
            return

        self.trade_repository = repository
        self.backfill_trades()

    def backfill_trades(self) -> None:
        """把主引擎已有的成交补写进库（幂等）

        程序当天中途重启时，CTP 登录会把当日成交重推一遍，主引擎里因此已有当日全量
        成交，这里补写一遍可以覆盖"程序没运行时到达"的那些成交。
        """
        if not self.trade_repository:
            return

        rows: list[dict[str, Any]] = self.get_reference_trade_rows()
        if not rows:
            return

        count: int = self.trade_repository.save_rows(rows)
        self.write_log(f"成交记录入库：新增 {count} 条（当日成交共 {len(rows)} 条，重复的不再写入）")

    def make_trade_row(self, trade: TradeData, reference: str = "") -> dict[str, Any]:
        """把成交转成行数据（补上合约名称与委托标记的快照）"""
        if not reference:
            reference = getattr(trade, "reference", "") or ""

        contract: ContractData | None = self.main_engine.get_contract(trade.vt_symbol)
        name: str = contract.name if contract else ""

        order: OrderData | None = self.main_engine.get_order(trade.vt_orderid)
        mark: str = order.mark if order else ""

        return build_trade_row(trade, name=name, mark=mark)

    def get_reference_trade_rows(self) -> list[dict[str, Any]]:
        """主引擎内存里带组合标记的成交（按时间**倒序**，最新在前）

        只在未接入数据库时作为降级数据源使用：主引擎的成交是纯内存的，重启即失，
        且网关只会重放当日成交。
        """
        rows: list[dict[str, Any]] = []

        for trade in self.main_engine.get_all_trades():
            reference: str = self.order_reference_map.get(trade.vt_orderid, "")
            if not reference:
                continue

            rows.append(self.make_trade_row(trade, reference))

        rows.sort(key=lambda row: row["trade_time"], reverse=True)
        return rows

    def update_trade_names(self) -> None:
        """把已加载的合约名称回填到名称为空的历史成交行

        成交入库时合约信息可能还没加载（名称会是空），这里按周期补一次；只更新名称
        为空的旧行，不覆盖已有的名称快照。
        """
        if not self.trade_repository:
            return

        pairs: list[tuple[str, str]] = []
        for vt_symbol in {result.vt_symbol for result in self.contract_results.values()}:
            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if contract and contract.name:
                pairs.append((vt_symbol, contract.name))

        if pairs:
            self.trade_repository.update_names(pairs)

    def is_trade_db_ready(self) -> bool:
        """成交记录是否已接入数据库（未加载 SqlApp 或建表失败时为 False）"""
        return self.trade_repository is not None

    def query_trades(
        self,
        start_date: date,
        end_date: date,
        reference: str = "",
        symbol: str = ""
    ) -> dict[str, Any]:
        """按日期范围查询历史成交（供界面后台线程调用，不抛异常）

        返回 ``{"rows": [...], "truncated": bool, "error": str, "db_ready": bool}``；
        行按时间倒序。Query 失败时 ``error`` 里带原因，界面只展示错误不炸窗口。
        """
        if not self.trade_repository:
            return {"rows": [], "truncated": False, "error": "", "db_ready": False}

        # 取到局部变量：finally 里即使 self.trade_repository 被改也不影响释放
        repository: TradeRepository = self.trade_repository

        try:
            rows, truncated = repository.query_range(
                start_date,
                end_date,
                reference,
                symbol
            )
        except Exception as exc:  # noqa: BLE001 - 驱动异常类型由 SqlApp 包装
            self.write_log(f"查询成交记录失败：{exc}")
            return {
                "rows": [],
                "truncated": False,
                "error": str(exc),
                "db_ready": True
            }
        finally:
            # 查询跑在界面的临时线程里，peewee 连接是 thread-local 的，必须在这里释放
            repository.release_thread_connection()

        return {"rows": rows, "truncated": truncated, "error": "", "db_ready": True}

    def get_trade_filter_options(self) -> tuple[list[str], list[str]]:
        """数据库里出现过的组合与代码（历史组合也能筛到）

        未接入数据库或查询失败时返回空列表，由界面回退到内存里的成交。
        """
        if not self.trade_repository:
            return [], []

        try:
            references: list[str] = self.trade_repository.list_references()
            symbols: list[str] = self.trade_repository.list_symbols()
        except Exception as exc:  # noqa: BLE001
            self.write_log(f"读取成交记录筛选项失败：{exc}")
            return [], []

        return references, symbols

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

        self.sql_settings = parse_sql_settings(setting)

    def save_setting(self) -> None:
        """写回设置文件

        在原文件基础上合并，而不是整份覆盖：用户可能手写了 ``sql`` 配置（或其他未知
        字段），直接覆盖会把它们抹掉。
        """
        setting: dict[str, Any] = load_json(self.setting_filename)
        setting["timer_interval"] = self.timer_interval
        setting["capitals"] = self.capitals
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
        """按时间间隔落盘历史快照、仓位与委托映射，避免崩溃丢当日数据

        委托映射（order_reference_map）必须一起落盘：成交记录与当日成交回放都靠它把
        成交归到组合上，而 load_order 只在文件日期等于当前交易日时才加载。只在 close()
        里存的话，进程非正常退出就整份丢失，表现就是成交记录为空、回放失效。
        """
        self.history_save_seconds += self.timer_interval
        if self.history_save_seconds < self.history_save_interval:
            return

        self.history_save_seconds = 0
        self.save_history()
        self.save_data()
        self.save_order()
        # 成交入库时合约可能还没加载（名称为空），随周期补一次
        self.update_trade_names()

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

        交易日按自然日（遇周末顺延），本地以 A 股为主、没有夜盘，所以不再把 20:00
        之后算作下一个交易日。
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

