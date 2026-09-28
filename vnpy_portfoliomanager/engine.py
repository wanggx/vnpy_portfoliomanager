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
        # vt_orderid -> 委托标记（mark）快照。与 order_reference_map 同理：不少网关
        # （XT/QMT 等）在后续的委托状态回报里会重建 OrderData 并把 mark 丢掉，而成交
        # 记录的"标记"列取自委托，必须在这里补回；同时落盘，进程重启后网关回放的委托
        # 同样没有 mark，靠存档还原。
        self.order_mark_map: dict[str, str] = {}
        # 已经告警过的"无组合标记"成交，避免重复推送时刷屏
        self.unmatched_tradeids: set[str] = set()
        self.contract_results: dict[tuple[str, str], ContractResult] = {}
        self.portfolio_results: dict[str, PortfolioResult] = {}

        # 成交记录入库配置与仓储；未加载 SqlApp 或建表失败时为 None（降级为内存模式）
        self.sql_settings: SqlSettings = SqlSettings()
        self.trade_repository: TradeRepository | None = None
        # 只报一次的日志：延迟初始化会被反复调用，同一句话别刷屏
        self.warned_messages: set[str] = set()

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
        # 成交库**不在这里连**：此刻 SqlApp 常常还没加载（Station 按 APP_INFO 顺序加载应用，
        # SqlApp 排在 PortfolioManager 之后），现在连只会白白降级成内存模式。连库交给
        # 使用方：界面打开时调 init_trade_repository()，无界面时由首笔成交兜底。
        self.register_event()

    def write_log(self, msg: str) -> None:
        """写日志到主引擎（BaseEngine 自身没有 write_log）"""
        self.main_engine.write_log(msg, self.engine_name)

    def write_log_once(self, msg: str) -> None:
        """同一句话只写一次（延迟初始化的降级路径会被反复走到）"""
        if msg in self.warned_messages:
            return

        self.warned_messages.add(msg)
        self.write_log(msg)

    def register_event(self) -> None:
        """"""
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        self.event_engine.register(EVENT_CONTRACT, self.process_contract_event)

    def process_order_event(self, event: Event) -> None:
        """"""
        order: OrderData = event.data

        reference: str = self.order_reference_map.get(order.vt_orderid, "")

        # 只有带回 reference 的推送才是可信来源，空值一律不入表：不少网关（如 XT/QMT）
        # 的委托状态推送、以及启动时的委托查询回报都不带 reference（XT 网关只把
        # req.reference 塞给券商端，回报里没有这个字段）。把空值记进去会把整份映射
        # 污染成空串，之后每笔成交都归不到组合上，表现就是成交记录空白。
        if order.reference:
            self.order_reference_map[order.vt_orderid] = order.reference
        elif reference:
            # 后续推送没带 reference：用本地缓存补回，别的引擎/界面也要读这个字段
            order.reference = reference

        # mark 同理：委托回报里常常没有这个字段（XT 的 on_stock_order 是重新 new 了
        # 一个 OrderData），必须用本地缓存补回，否则成交记录的"标记"列会是空的——
        # 成交是最后一步，等它到达时再补就已经晚了。
        mark: str = self.order_mark_map.get(order.vt_orderid, "")
        if order.mark:
            self.order_mark_map[order.vt_orderid] = order.mark
        elif mark:
            order.mark = mark

    def process_trade_event(self, event: Event) -> None:
        """"""
        trade: TradeData = event.data

        reference: str = self.order_reference_map.get(trade.vt_orderid, "")
        if not reference:
            # 静默丢弃会让"成交记录空白"极难排查，这里明确告警（同一笔只报一次）
            if trade.vt_tradeid not in self.unmatched_tradeids:
                self.unmatched_tradeids.add(trade.vt_tradeid)
                self.write_log(
                    f"成交{trade.vt_tradeid}（委托{trade.vt_orderid}，{trade.vt_symbol}）"
                    "缺少组合标记，已忽略：委托映射里没有这笔委托，该成交不会计入"
                    "盈亏与成交记录"
                )
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
        # 首笔成交时连库（延迟初始化）：此时所有应用都已加载完，SqlApp 一定在
        self.init_trade_repository()

        if not self.trade_repository:
            # 成交记录**只能从 SqlApp 读**：这条成交没进库，就不要推给界面，
            # 否则界面会显示一份“库里查不到”的成交
            return

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

    def init_trade_repository(self) -> bool:
        """连接 SqlApp 并准备成交记录表；连上返回 True，否则降级为纯内存模式

        **由使用方在合适的时机显式调用**（照 portfoliostrategy 的路子：界面打开时调
        ``init_engine()``），不在引擎构造时连库——Station 按 ``APP_INFO`` 顺序加载应用，
        ``SqlApp`` 排在 ``PortfolioManager`` 之后，构造时 ``get_engine("SqlApp")``
        必然拿不到，过早初始化只会把整个会话锁死在内存模式。

        当前调用点：界面 ``init_data()``（界面打开）、``process_trade_event()``（无界面
        或首笔成交先到时兜底）。

        幂等且可重试：已连上直接返回 True；失败不缓存结果，下次调用还会再试。
        """
        if self.trade_repository is not None:
            return True

        if not self.sql_settings.enabled:
            self.write_log_once("成交记录入库已关闭（设置文件中的 sql.enabled = false）")
            return False

        sql_engine: Any = self.main_engine.get_engine(SQL_APP_NAME)
        if sql_engine is None:
            self.write_log_once("未加载 SqlApp，成交记录只保留在内存中，无法查询历史成交")
            return False

        repository: TradeRepository = TradeRepository(
            sql_engine,
            self.sql_settings,
            self.write_log
        )

        if self.sql_settings.auto_create and not repository.ensure_table():
            return False

        self.trade_repository = repository
        self.backfill_trades()
        return True

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

        # mark 以本地缓存为准（见 order_mark_map）：重启后网关回放的委托没有 mark，
        # 但存档里有；缓存缺失（例如模块启动之前就下过的单）时才退回主引擎的委托对象。
        mark: str = self.order_mark_map.get(trade.vt_orderid, "")
        if not mark:
            order: OrderData | None = self.main_engine.get_order(trade.vt_orderid)
            mark = order.mark if order else ""

        return build_trade_row(trade, name=name, mark=mark)

    def get_reference_trade_rows(self) -> list[dict[str, Any]]:
        """主引擎内存里带组合标记的成交（按时间**倒序**，最新在前）

        只用于**连库成功时的回补**（`backfill_trades`）：把内存模式期间（或程序没运行、
        由网关重放）的成交补写进库。界面不再用它——成交记录**只从 SqlApp 读**。
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
        """成交记录是否已接入数据库（未加载 SqlApp 或建表失败时为 False）

        纯查询，**不带副作用**：连库由界面打开（界面里调 ``init_trade_repository()``，
        与 portfoliostrategy 的 ``widget -> engine.init_engine()`` 一致）或首笔成交触发。
        """
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

        连库由调用方先调 ``init_trade_repository()``（界面打开时已经连过），
        这里不再隐式触发。
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

        未接入数据库或查询失败时返回空列表（界面不再回退到内存里的成交）。
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

            # 上一日已平仓（收盘 0 仓）的合约直接丢掉：A 股口径下平仓当天仍要显示，
            # 清理留到下一个交易日，所以存档里还留着它们（见 save_data）。
            if date_changed and not d["last_pos"]:
                continue

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
        skipped: int = 0

        for trade in self.main_engine.get_all_trades():
            reference: str = self.order_reference_map.get(trade.vt_orderid, "")
            if not reference:
                skipped += 1
                continue

            key: tuple[str, str] = (reference, trade.vt_symbol)
            contract_result: ContractResult | None = self.contract_results.get(key, None)
            if not contract_result:
                contract_result = ContractResult(self, reference, trade.vt_symbol)
                self.contract_results[key] = contract_result

            contract_result.update_trade(trade)

        # 映射缺失（例如程序在下单之后才启动、或映射文件被跨日覆盖）时静默跳过，
        # 结果就是当天成交全丢，这里至少留一条可追溯的记录
        if skipped:
            self.write_log(
                f"当日成交回放：{skipped} 笔成交找不到组合标记（委托映射缺失），"
                "这些成交不会计入盈亏与成交记录"
            )

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

        # 当日平仓（持仓 0）的合约**照样落盘**：与 A 股持仓显示口径一致，平仓当天明细里
        # 仍要有这条 0 仓记录，同一交易日重启后也一样。清理留到下一个交易日，由
        # load_data() 跨日载入时丢弃、check_date_change() 从内存里删掉，所以存档不会
        # 随交易过的标的越积越多。
        # 注意这里不能反过来把 0 仓的 contract_result 删掉：清仓当日那笔已实现盈亏还在
        # 它身上，删了会让组合当日盈亏变小。
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

        history: dict = data.get("history", {})

        # 丢弃晚于当前交易日的快照：只可能是旧的"20:00 之后算下一交易日"口径写下的
        # 脏点（A 股没有夜盘，那天其实还是同一天），留着会让曲线多一个点、
        # 累计合计偏大。
        dropped: list[str] = []
        for reference, days in history.items():
            for date_str in list(days.keys()):
                if date_str > self.current_date:
                    days.pop(date_str)
                    dropped.append(f"{reference}@{date_str}")

        self.history = history

        if dropped:
            self.write_log(
                f"已忽略晚于当前交易日的盈亏快照（旧口径脏数据）：{', '.join(dropped)}"
            )

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
        """检测交易日切换：归档前一日结果，滚动收盘仓位，并清理已平仓的合约

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

        self.clear_flat_contracts()

        self.save_data()
        self.save_history()

    def clear_flat_contracts(self) -> None:
        """清掉已平仓（持仓 0）的合约记录，并通知界面隐藏对应明细行

        平仓当天**不清理**：与 A 股持仓显示口径一致，当日平仓的标的当天仍要在明细里
        显示一条 0 仓记录，到下一个交易日才消失。所以这个动作放在交易日切换时，且必须
        在 record_daily_result() 之后——清仓当日那笔已实现盈亏这时已经写进历史快照，
        此刻删掉不会让前一日的组合盈亏变小（新的一天本来也不该再累计它）。

        界面侧只 ``setHidden(True)``（不删控件，重新建仓时原样复用），因此必须显式下发
        ``cleared`` 标记：记录删掉之后引擎不会再推这个合约的周期数据，界面无从自己判断。
        """
        for key in list(self.contract_results.keys()):
            contract_result: ContractResult = self.contract_results[key]
            if contract_result.last_pos:
                continue

            del self.contract_results[key]

            data: dict = contract_result.get_data()
            data["cleared"] = True
            self.event_engine.put(Event(EVENT_PM_CONTRACT, data))

    def load_order(self) -> None:
        """"""
        order_data: dict = load_json(self.order_filename)

        date: str = order_data.get("date", "")
        today: str = get_trading_day()
        if date == today:
            self.order_reference_map = order_data["data"]
            # 旧版存档没有 marks 字段，取不到就是空表
            self.order_mark_map = order_data.get("marks", {}) or {}

    def save_order(self) -> None:
        """"""
        today: str = get_trading_day()
        existing: dict = load_json(self.order_filename)

        # 合并写入（同一天早先实例存下的映射是成交归属/标记的唯一恢复来源）：先取回
        # 今天已落盘的有效项，再用当前内存里的非空项覆盖，谁都不丢。
        same_day: bool = existing.get("date") == today
        data: dict[str, str] = {
            key: value
            for key, value in (existing.get("data") or {}).items()
            if value
        } if same_day else {}
        marks: dict[str, str] = {
            key: value
            for key, value in (existing.get("marks") or {}).items()
            if value
        } if same_day else {}

        # 空串是"这一笔没拿到 reference / mark"的占位，写进去会把上次留下的好映射覆盖掉
        data.update(
            (key, value) for key, value in self.order_reference_map.items() if value
        )
        marks.update(
            (key, value) for key, value in self.order_mark_map.items() if value
        )

        if not data and not marks:
            return

        order_data: dict[str, Any] = {
            "date": today,
            "data": data,
            "marks": marks,
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

