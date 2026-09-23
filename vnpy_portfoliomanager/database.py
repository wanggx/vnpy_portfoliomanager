"""成交记录入库：表结构、幂等写入与日期范围查询。

只依赖 SqlApp 公开的 ``SqlEngine`` 接口（``query`` / ``execute``），不自己建连接、
不碰 SQLApp 内部实现——与 ``vnpy_patternsearch`` 的 sqlapp 数据源做法一致。

方言固定为 **MySQL**（当前 ``sqlapp.type = mysql``）：反引号标识符、``%s`` 占位符、
``ON DUPLICATE KEY UPDATE`` 幂等写入。SqlApp 明确不做 SQL 方言转换，换成
sqlite/postgresql 需要改本文件的 DDL 与写入语句。

时间口径：vnpy 的 ``trade.datetime`` 是带时区的 UTC，入库前统一转成**本地时区**的
naive datetime（与 vnpy ``TimeCell`` 的显示口径一致，即系统时区）。表中同时保留
``trade_date`` 独立列，范围查询可直接走索引、不必在 SQL 里做时区换算。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import TYPE_CHECKING, Any

from tzlocal import get_localzone_name
from vnpy.trader.object import TradeData
from vnpy.trader.utility import ZoneInfo

from .settings import DEFAULT_TABLE, SqlSettings, validate_identifier

if TYPE_CHECKING:
    # 可选依赖：只用于类型标注，运行时由引擎从 MainEngine 取 SqlEngine 实例
    from vnpy_sqlapp import SqlEngine


class TradeDatabaseError(Exception):
    """成交记录读写失败"""


# 列定义：(列名, 类型定义, 注释)。列表顺序即 INSERT 与 SELECT 的列顺序。
COLUMN_DEFINITIONS: list[tuple[str, str, str]] = [
    ("trade_date", "DATE NOT NULL", "交易日（本地时区，成交时间的日期部分）"),
    ("trade_time", "DATETIME(3) NOT NULL", "成交时间，本地时区，毫秒精度"),
    ("reference", "VARCHAR(64) NOT NULL DEFAULT ''", "组合/策略名（vnpy reference）"),
    ("vt_symbol", "VARCHAR(64) NOT NULL DEFAULT ''", "本地代码，如 600000.SSE"),
    ("symbol", "VARCHAR(32) NOT NULL DEFAULT ''", "代码，如 600000"),
    ("exchange", "VARCHAR(16) NOT NULL DEFAULT ''", "交易所枚举名，如 SSE"),
    ("name", "VARCHAR(64) NOT NULL DEFAULT ''", "合约名称快照"),
    ("direction", "VARCHAR(8) NOT NULL DEFAULT ''", "方向枚举名 LONG / SHORT"),
    ("offset", "VARCHAR(16) NOT NULL DEFAULT ''", "开平枚举名 OPEN / CLOSE 等"),
    ("price", "DECIMAL(20,6) NOT NULL DEFAULT 0", "成交价"),
    ("volume", "DECIMAL(20,4) NOT NULL DEFAULT 0", "成交量（股/手）"),
    ("tradeid", "VARCHAR(32) NOT NULL DEFAULT ''", "柜台成交号"),
    ("orderid", "VARCHAR(32) NOT NULL DEFAULT ''", "柜台委托号"),
    ("vt_tradeid", "VARCHAR(64) NOT NULL DEFAULT ''", "gateway_name.tradeid"),
    ("vt_orderid", "VARCHAR(64) NOT NULL DEFAULT ''", "gateway_name.orderid"),
    ("gateway_name", "VARCHAR(32) NOT NULL DEFAULT ''", "接口名，如 CTP"),
    ("mark", "VARCHAR(64) NOT NULL DEFAULT ''", "委托标记快照"),
]

TRADE_COLUMNS: list[str] = [name for name, _, _ in COLUMN_DEFINITIONS]

_LOCAL_TZ: ZoneInfo | None = None


def get_local_tz() -> ZoneInfo:
    """本地时区（与 vnpy TimeCell 一致：取系统时区）"""
    global _LOCAL_TZ
    if _LOCAL_TZ is None:
        _LOCAL_TZ = ZoneInfo(get_localzone_name())
    return _LOCAL_TZ


def to_local_naive(value: datetime | None) -> datetime:
    """把带时区的成交时间转成本地时区的 naive datetime

    vnpy 的 ``datetime`` 带时区（UTC）；数据库列存本地时间，便于直接用 SQL 按日期
    范围过滤、也便于人工查表。时间缺失时用当前本地时间兜底（列是 NOT NULL）。
    """
    if value is None:
        return datetime.now(get_local_tz()).replace(tzinfo=None)
    if value.tzinfo is None:
        return value
    return value.astimezone(get_local_tz()).replace(tzinfo=None)


def enum_name(value: Enum | None) -> str:
    """枚举统一存 **name**（LONG/SSE/OPEN）

    vnpy 的 ``Direction`` / ``Offset`` 的 ``value`` 是中文（多/空/开/平）且随语言
    版本变化，存 name 更稳定；读回来用 ``Direction[...]`` 还原。
    """
    if value is None:
        return ""
    return getattr(value, "name", "") or str(value)


def to_decimal(value: Any) -> Decimal:
    """转成写库用的 Decimal，转不了就返回 0

    价格/数量用定点存（DECIMAL）而不是 DOUBLE；但网关给的脏数据（非数值）不能把
    入库链路抛崩——这条链路跑在 EventEngine 的派发线程里，异常会连带影响整个 vnpy
    的事件处理，所以这里退化成 0 并留待日志排查。
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def build_trade_row(trade: TradeData, name: str = "", mark: str = "") -> dict[str, Any]:
    """把成交转成一行表数据（键与 ``TRADE_COLUMNS`` 一致）

    ``name``（合约名称）与 ``mark``（委托标记）由调用方在**写入时**解析后传入：历史
    查询不能依赖主引擎的内存缓存（合约会退市/改名，``TradeData`` 也没有 mark 字段）。
    """
    trade_time: datetime = to_local_naive(trade.datetime)

    return {
        "trade_date": trade_time.date(),
        "trade_time": trade_time,
        "reference": getattr(trade, "reference", "") or "",
        "vt_symbol": trade.vt_symbol,
        "symbol": trade.symbol,
        "exchange": enum_name(trade.exchange),
        "name": name or "",
        "direction": enum_name(trade.direction),
        "offset": enum_name(trade.offset),
        "price": to_decimal(trade.price),
        "volume": to_decimal(trade.volume),
        "tradeid": trade.tradeid,
        "orderid": trade.orderid,
        "vt_tradeid": trade.vt_tradeid,
        "vt_orderid": trade.vt_orderid,
        "gateway_name": trade.gateway_name,
        "mark": mark or "",
    }


def trade_key(row: dict[str, Any]) -> tuple[str, str]:
    """成交的唯一键：``(交易日, vt_tradeid)``

    CTP 的 ``TradeID`` 只在**当个交易日内**唯一、跨交易日会重复，所以唯一键必须带
    日期，否则历史数据会被跨日重复的 tradeid 静默覆盖。
    """
    trade_date: Any = row.get("trade_date")
    if isinstance(trade_date, datetime):
        trade_date = trade_date.date()
    return (str(trade_date or ""), str(row.get("vt_tradeid") or ""))


def build_create_table_sql(table_name: str) -> str:
    """生成建表语句（与 ``script/create_vnpy_portfolio_trade.sql`` 保持一致）

    ``table_name`` 是**未加引号**的原始表名，内部会做标识符校验。
    """
    table_sql: str = validate_identifier(table_name, "表名")

    columns: str = ",\n".join(
        f"    {validate_identifier(name, '列名')} {definition} COMMENT '{comment}'"
        for name, definition, comment in COLUMN_DEFINITIONS
    )

    return (
        f"CREATE TABLE IF NOT EXISTS {table_sql} (\n"
        "    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',\n"
        f"{columns},\n"
        "    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次入库时间',\n"
        "    PRIMARY KEY (`id`),\n"
        "    UNIQUE KEY `uk_trade_date_tradeid` (`trade_date`, `vt_tradeid`),\n"
        "    KEY `idx_date_reference` (`trade_date`, `reference`),\n"
        "    KEY `idx_symbol_date` (`vt_symbol`, `trade_date`)\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='投资组合成交记录'"
    )


class TradeRepository:
    """成交记录表的数据访问对象（只管 SQL，不关心 vnpy 事件与界面）"""

    def __init__(
        self,
        sql_engine: SqlEngine,
        settings: SqlSettings,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.sql_engine: SqlEngine = sql_engine
        self.settings: SqlSettings = settings
        self.log: Callable[[str], None] = log or (lambda msg: None)

        # 表名非法时回退默认表名，并提示一次；同时保留原始表名与反引号版本
        if settings.is_valid_table():
            self.table_name: str = settings.table
        else:
            self.table_name = DEFAULT_TABLE
            self.log(
                f"配置的表名 {settings.table!r} 不合法（只允许字母数字下划线），"
                f"已回退到 {DEFAULT_TABLE}"
            )

        self.table: str = validate_identifier(self.table_name, "表名")

        # 拼好的列名列表，避免每次写入/查询重复拼接
        self.column_sql: str = ", ".join(
            validate_identifier(name, "列名") for name in TRADE_COLUMNS
        )
        self.placeholder_sql: str = ", ".join("%s" for _ in TRADE_COLUMNS)

    # -- 建表 -------------------------------------------------------------

    def ensure_table(self) -> bool:
        """建表（已存在则跳过）；失败返回 False，由调用方决定是否降级"""
        try:
            self.sql_engine.execute(build_create_table_sql(self.table_name))
        except Exception as exc:  # noqa: BLE001 - 驱动异常类型由 SqlApp 包装
            self.log(f"创建成交记录表 {self.table} 失败：{exc}")
            return False
        return True

    # -- 写入 -------------------------------------------------------------

    def save_row(self, row: dict[str, Any]) -> bool:
        """写入一笔成交，返回是否**新增**了一行（重复键为 False）

        重启、CTP 登录重放当日成交、``replay_trades()`` 都会重复见到同一笔成交，
        所以对重复键直接忽略（``ON DUPLICATE KEY UPDATE id = id`` 不产生实际修改，
        影响行数为 0，据此就能区分新增与重复）。
        用 ``INSERT IGNORE`` 也能去重，但它会把数据截断等错误一起吞掉，不好排查。
        """
        sql: str = (
            f"INSERT INTO {self.table} ({self.column_sql}) "
            f"VALUES ({self.placeholder_sql}) "
            "ON DUPLICATE KEY UPDATE id = id"
        )
        parameters: tuple[Any, ...] = tuple(row[name] for name in TRADE_COLUMNS)

        try:
            rowcount: int = self.sql_engine.execute(sql, parameters)
        except Exception as exc:  # noqa: BLE001
            self.log(f"成交记录入库失败（{row.get('vt_tradeid', '')}）：{exc}")
            return False

        return rowcount > 0

    def save_rows(self, rows: list[dict[str, Any]]) -> int:
        """批量写入，返回**新增**的条数（重复的成交不计数）

        逐条执行而不是 ``executemany``：驱动侧的多行 INSERT 改写遇到
        ``ON DUPLICATE KEY UPDATE`` 会退化成逐条执行甚至报错，收益不稳定；
        单批数量很小（当日成交），逐条更可控，且一条失败不影响其余。
        """
        count: int = 0
        for row in rows:
            if self.save_row(row):
                count += 1
        return count

    def update_names(self, pairs: list[tuple[str, str]]) -> int:
        """补齐空白的合约名称（成交入库时合约可能还没加载）

        ``pairs`` 为 ``(vt_symbol, name)`` 列表。名字按 ``vt_symbol`` 回填，只更新
        名称为空的旧行，不会覆盖快照。
        """
        count: int = 0
        for vt_symbol, name in pairs:
            if not vt_symbol or not name:
                continue

            sql: str = (
                f"UPDATE {self.table} SET `name` = %s "
                "WHERE `vt_symbol` = %s AND `name` = ''"
            )
            try:
                self.sql_engine.execute(sql, (name, vt_symbol))
            except Exception as exc:  # noqa: BLE001
                self.log(f"补齐合约名称失败（{vt_symbol}）：{exc}")
                continue
            count += 1
        return count

    # -- 查询 -------------------------------------------------------------

    def query_range(
        self,
        start_date: date,
        end_date: date,
        reference: str = "",
        symbol: str = "",
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """按日期范围（含边界）查询成交，返回 (行, 是否被截断)

        按时间**倒序**返回，与界面上"最新的在最上面"的现有习惯一致。
        多取一行用于判断是否被 LIMIT 截断，避免额外来一次 COUNT(*)。
        """
        max_rows: int = self.settings.max_rows if limit is None else max(0, int(limit))

        conditions: list[str] = ["`trade_date` >= %s", "`trade_date` <= %s"]
        parameters: list[Any] = [start_date, end_date]

        if reference:
            conditions.append("`reference` = %s")
            parameters.append(reference)

        if symbol:
            conditions.append("`symbol` = %s")
            parameters.append(symbol)

        sql: str = (
            f"SELECT {self.column_sql} FROM {self.table} "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY `trade_time` DESC, `id` DESC "
            f"LIMIT {int(max_rows) + 1}"
        )

        result: Any = self.sql_engine.query(sql, tuple(parameters), max_rows=None)
        if result.error:
            raise TradeDatabaseError(str(result.error))

        # SELECT 的列就是 TRADE_COLUMNS，长度必须一致（不一致说明 DDL 与代码跑偏了）
        rows: list[dict[str, Any]] = [
            dict(zip(TRADE_COLUMNS, values, strict=True)) for values in result.rows
        ]
        truncated: bool = len(rows) > max_rows
        return rows[:max_rows], truncated

    def list_references(self) -> list[str]:
        """历史出现过的组合（筛选下拉框的可选项）"""
        return self._list_distinct("reference")

    def list_symbols(self) -> list[str]:
        """历史出现过的代码（筛选下拉框的可选项）"""
        return self._list_distinct("symbol")

    def release_thread_connection(self) -> None:
        """释放当前线程的数据库连接

        peewee 的连接是 thread-local 的：界面每次查询都新起一个后台线程，线程结束前
        必须释放，否则每查一次就多留一个 MySQL 连接。SqlApp 自己的异步查询路径
        （``query_async``）同样在收尾时调它，这里保持一致。
        """
        db: Any = getattr(self.sql_engine, "database", None)
        if db is None:
            return

        try:
            db.release_connection()
        except Exception:  # noqa: BLE001 - 释放失败不影响查询结果
            pass

    def _list_distinct(self, column: str) -> list[str]:
        column_sql: str = validate_identifier(column, "列名")
        sql: str = (
            f"SELECT DISTINCT {column_sql} FROM {self.table} "
            f"WHERE {column_sql} <> '' ORDER BY {column_sql}"
        )

        result: Any = self.sql_engine.query(sql, max_rows=None)
        if result.error:
            raise TradeDatabaseError(str(result.error))

        return [str(values[0]) for values in result.rows if values and values[0] is not None]
