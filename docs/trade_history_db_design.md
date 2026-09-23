# 成交记录入库 + 历史查询 设计稿

目标：

1. 成交记录落库，历史全量可查；
2. 成交记录页支持**日期范围**查询（不再是"只有当日"）。

依赖：`SqlApp`（`vnpy_sqlapp`）的**公开** `SqlEngine` 接口，不自己建连接、不碰
`SqlDatabase` 内部实现 —— 与 `vnpy_patternsearch` 里 `sqlapp_stock_daily.py` 的做法一致。

---

## 1. 现状与问题

```
EVENT_TRADE → PortfolioEngine.process_trade_event()
                 ├─ order_reference_map 命中才有 reference，否则丢弃
                 ├─ ContractResult.update_trade()
                 └─ EVENT_PM_TRADE → UI 追加一行

UI 历史查询：PortfolioEngine.get_all_reference_trades()
                 └─ MainEngine.get_all_trades()   ← 纯内存，不落盘，重启即失
```

两条"只有当日"的硬限制：

- `load_order()` 只在 `date == today` 时载入 `order_reference_map`，历史委托号查不到
  reference → 被过滤掉；
- 网关侧 CTP 只在登录时重放**当日**成交，历史日期没有任何来源。

四个存档 json（setting / history / data / order）都不含成交明细，所以"历史成交"目前
根本不存在数据源。

## 2. 数据流（改造后）

```
                        ┌──────────────── 实时 ────────────────┐
EVENT_TRADE ─→ process_trade_event ─→ TradeRepository.save()  ─→ DB
                        └─→ EVENT_PM_TRADE ─→ UI（落在当前查询区间才插入）

                        ┌──────────────── 查询 ────────────────┐
UI(日期范围/组合/合约) ─→ TradeRepository.query_range() ─→ DB ─→ 表格刷新

                        ┌──────────────── 启动 ────────────────┐
启动 init ─→ TradeRepository.save_many(main_engine.get_all_trades())
            把主引擎已有的当日成交补写一遍（幂等 upsert），
            覆盖"程序中途重启/断线期间到达但当时不在线"的成交。
```

原则：**落库失败只记日志，不阻断交易记账**（仓位/盈亏仍以内存为准，DB 是流水账）。

## 3. 表结构

表名（配置项，默认）：`vnpy_portfolio_trade`

MySQL 版 DDL（`sqlapp.type = mysql`，当前环境）。等价脚本见
`script/create_vnpy_portfolio_trade.sql`，代码侧由 `database.build_create_table_sql()` 生成：

```sql
CREATE TABLE IF NOT EXISTS `vnpy_portfolio_trade` (
    `id`            BIGINT        NOT NULL AUTO_INCREMENT,
    `trade_date`    DATE          NOT NULL COMMENT '交易日（本地时区）',
    `trade_time`    DATETIME(3)   NOT NULL COMMENT '成交时间，本地时区，毫秒精度',
    `reference`     VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '组合/策略名',
    `vt_symbol`     VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '本地代码 600000.SSE',
    `symbol`        VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '代码 600000',
    `exchange`      VARCHAR(16)   NOT NULL DEFAULT '' COMMENT '交易所枚举名 SSE',
    `name`          VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '合约名称快照',
    `direction`     VARCHAR(8)    NOT NULL DEFAULT '' COMMENT '方向枚举名 LONG / SHORT',
    `offset`        VARCHAR(16)   NOT NULL DEFAULT '' COMMENT '开平枚举名 OPEN / CLOSE 等',
    `price`         DECIMAL(20,6) NOT NULL DEFAULT 0  COMMENT '成交价',
    `volume`        DECIMAL(20,4) NOT NULL DEFAULT 0  COMMENT '成交量（股/手）',
    `tradeid`       VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '柜台成交号',
    `orderid`       VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '柜台委托号',
    `vt_tradeid`    VARCHAR(64)   NOT NULL DEFAULT '' COMMENT 'gateway_name.tradeid',
    `vt_orderid`    VARCHAR(64)   NOT NULL DEFAULT '' COMMENT 'gateway_name.orderid',
    `gateway_name`  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '接口名 CTP',
    `mark`          VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '委托标记快照',
    `created_at`    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次入库时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_trade_date_tradeid` (`trade_date`, `vt_tradeid`),
    KEY `idx_date_reference` (`trade_date`, `reference`),
    KEY `idx_symbol_date` (`vt_symbol`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='投资组合成交记录';
```

sqlite / postgresql 的差异（**当前不支持**，见第 7 节）：

| 项 | sqlite | postgresql |
|---|---|---|
| 自增主键 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` |
| 毫秒时间 | `DATETIME`（文本） | `TIMESTAMP(3)` |
| 幂等写入 | `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` |
| 占位符 | `?` / `:name` | `%s` / `%(name)s` |

### 3.1 字段与 vnpy `TradeData` 的映射

| 表字段 | 来源 | 备注 |
|---|---|---|
| `trade_time` | `trade.datetime` → 本地时区 | 与 UI `TradeTimeCell` 显示口径一致（`database.timezone`，默认 Asia/Shanghai） |
| `trade_date` | `trade_time` 取日期部分 | 独立成列，范围查询可直接走索引，避免 `DATE(trade_time)` 导致索引失效 + SQL 里做时区换算 |
| `reference` | `getattr(trade, "reference", "")` | vnpy 4.4 的 `TradeData` 是 dataclass，`reference` 由本模块动态挂上 |
| `name` | `main_engine.get_contract(vt_symbol).name` | **写入时快照**：历史查询不能依赖主引擎（合约可能退市/改名/当时未加载） |
| `mark` | `main_engine.get_order(vt_orderid).mark` | 同上，`TradeData` 没有 mark，隔日重启后取不到，必须写入时快照 |
| `price` / `volume` | `trade.price` / `trade.volume` | 用 `DECIMAL` 而非 `DOUBLE`：金额/数量定点存储，避免浮点误差 |
| 其余 | `trade.*` / `trade.vt_*` | 直接落库 |

### 3.2 为什么唯一键是 `(trade_date, vt_tradeid)` 而不是 `vt_tradeid`

CTP 的 `TradeID` **只在当个交易日内唯一，跨交易日会重复**（`MainEngine.trades` 用
`vt_tradeid` 单键是内存字典、只服务当日，所以没问题；但持久表跨日就会撞键）。
必须带上日期，否则历史数据会被跨日重复的 tradeid 静默覆盖/丢弃。

### 3.3 幂等写入（upsert）

- 引擎重启、CTP 登录重放当日成交、`replay_trades()` 都会重复见到同一笔成交；
- 所以写库一律用"存在即忽略"，靠唯一键兜底：

```sql
INSERT INTO `vnpy_portfolio_trade` (...) VALUES (...)
ON DUPLICATE KEY UPDATE `id` = `id`;
```

重复键时影响行数为 0，据此区分"新增/已存在"（日志里的"新增 N 条"就是这个口径）。
没用 `INSERT IGNORE` 是因为它会把数据截断等错误一起吞掉，不好排查；
也没用 `executemany`（驱动侧的多行 INSERT 改写遇到 `ON DUPLICATE KEY UPDATE`
会退化成逐条执行甚至报错），改为逐条 `execute`，一条失败不影响其余。

### 3.4 典型查询

```sql
-- 日期范围（含边界）+ 可选条件，按时间倒序（最新的在最上面，与界面习惯一致）
SELECT `trade_date`, `trade_time`, `reference`, ... , `mark`
FROM `vnpy_portfolio_trade`
WHERE `trade_date` >= %s AND `trade_date` <= %s
  [AND `reference` = %s]        -- 选了组合才加
  [AND `symbol` = %s]           -- 选了合约才加
ORDER BY `trade_time` DESC, `id` DESC
LIMIT 5001;                     -- 上限 +1，多取一行用于判断是否被截断

-- 筛选下拉框的可选项（历史组合/合约也能选到）
SELECT DISTINCT `reference` FROM `vnpy_portfolio_trade` WHERE `reference` <> '' ORDER BY `reference`;
SELECT DISTINCT `symbol`    FROM `vnpy_portfolio_trade` WHERE `symbol`    <> '' ORDER BY `symbol`;
```

`LIMIT` 默认 5000（配置项）：多取一行就能判断是否被截断，不必额外查一次 `COUNT(*)`；
命中上限时状态栏提示"已显示最新的 5000 条，可能有更多记录，请缩小日期范围"。

## 4. 引擎侧改动

新增 `vnpy_portfoliomanager/database.py`，只依赖 `SqlEngine` 公开接口：

```python
class TradeRepository:
    def __init__(self, sql_engine: SqlEngine | None, table: str, auto_create: bool) -> None: ...

    def ensure_table(self) -> bool: ...                       # CREATE TABLE IF NOT EXISTS
    def save_row(self, row: dict[str, Any]) -> bool: ...      # 幂等 upsert，返回是否新增
    def save_rows(self, rows: list[dict[str, Any]]) -> int: ...  # 逐条幂等写入，返回新增数
    def update_names(self, pairs: list[tuple[str, str]]) -> int: ...  # 补齐空的合约名称
    def query_range(self, start: date, end: date, reference: str = "",
                    symbol: str = "", limit: int | None = None
                    ) -> tuple[list[dict[str, Any]], bool]: ...
    def list_references(self) -> list[str]: ...
    def list_symbols(self) -> list[str]: ...
```

`engine.py`：

- `__init__` 里 `sql_engine = main_engine.get_engine("SqlApp")`，拿不到就整体降级
  （`self.trade_repository = None`），模块照常工作，只是没有历史查询；
- 在 `load_data()` 之后、`register_event()` 之前调 `init_trade_repository()`（建表 +
  回补当日成交），保证之后到达的实时成交一律入库；
- `process_trade_event()` 里在 `update_trade()` 之后落库，**再**推 `EVENT_PM_TRADE`；
  事件负载因此改成行数据 dict（键见 `TRADE_COLUMNS`），界面与入库共用同一套字段口径；
- `save_history_periodically()` 里额外调 `update_trade_names()`，把入库时还没加载到的
  合约名称回填到名称为空的行（只更新 `name = ''` 的行，不覆盖已有快照）。

`APP_NAME` 从 `vnpy_sqlapp` 导入（`"SqlApp"`），但**不做硬依赖**：
`vnpy_sqlapp` 未安装时 `import` 失败不应让本模块崩，所以走 `try/except ImportError`
+ 运行时探测。

## 5. UI 侧改动（成交记录页）

顶部工具条改为：

```
[起始日期 ▾][结束日期 ▾] [今天][近一周][近一月][全部] [查询]
[组合 ▾][合约 ▾][清空筛选]                          [导出CSV]
状态：共 N 条  或  已显示前 5000 条，请缩小日期范围
```

- 日期控件用 `QDateEdit` + `setCalendarPopup(True)`，默认"今天 ~ 今天"；
- 「查询」在**后台线程**里跑 `TradeRepository.query_range()`，结果通过 Qt Signal 回到
  主线程刷新表格（沿用本模块现有的 `signal_*` 事件模式，不复用 sqlapp 的单槽
  `query_async`，避免和 SqlApp 查询页抢锁）；
- 表格由"只追加"改为"整体刷新"，同时在收到 `EVENT_PM_TRADE` 时判断该成交是否落在
  当前查询区间，落在就插一行（保持日内实时感）；
- 组合/合约下拉选项改为从 DB 的 `DISTINCT` 取（历史组合也能筛），保留本地过滤逻辑兜底；
- 「导出CSV」导出当前表格内容（即当前查询结果，仍是只导可见行）；
- SqlApp 未加载时：表格退回内存里的当日成交，状态栏提示"未加载 SqlApp，仅显示当日内存成交，
  无法查询历史"（日期控件保持可点，只是不生效）。

实现要点：

- 日期控件用 `QDateEdit` + `setCalendarPopup(True)`，默认"今天 ~ 今天"；
- 快捷区间（今天 / 近一周 / 近一月 / 全部）用 `set_trade_date_range()` 一次性设置两个日期，
  期间 `blockSignals`，避免触发两次查询；"全部"起始日用 `2000-01-01` 占位；
- 起始晚于结束时，把**被改的那个**同步到另一端（用 `self.sender()` 判断），避免查出空结果；
- 查询在后台线程里跑 `PortfolioEngine.query_trades()`，结果通过 `signal_trades_loaded` 回到
  主线程，**不复用 sqlapp 的单槽 `query_async`**（那是它的查询页专用，会和界面抢那个锁）；
- 每次查询带一个自增 `trade_query_token`，`process_trades_loaded` 只采纳序号最新的结果，
  条件改得比线程快时不会出现"旧结果盖新结果"；
- 表格由"只追加"改为"整体刷新 + 实时插入"：`EVENT_PM_TRADE` 到达时若该成交落在当前区间
  且未出现过（按 `(trade_date, vt_tradeid)` 去重）就插入到最上面；
- 组合/合约下拉选项来自 DB 的 `DISTINCT` + 实时成交 + 查询结果里出现过的值（历史组合也能筛），
  选中后作为**服务端**查询条件；`monitor.set_filter()` 仍保留，用于实时插入行的可见性判断；

## 6. 配置项（`portfolio_manager_setting.json` 的 `sql` 子字典）

| 配置 | 默认 | 说明 |
|---|---|---|
| `sql.enabled` | `true` | 关掉则完全退回内存模式 |
| `sql.table` | `vnpy_portfolio_trade` | 表名（白名单校验后拼接） |
| `sql.auto_create` | `true` | 启动自动 `CREATE TABLE IF NOT EXISTS` |
| `sql.max_rows` | `5000` | 单次查询行数上限 |

默认值不落盘；`save_setting()` 改为在原文件上合并写入，所以用户手写的 `sql` 配置不会被
模块的存档动作抹掉。表名与列名一律走"只允许 `[A-Za-z_][A-Za-z0-9_]*` + 反引号包裹"的
校验（沿用 `vnpy_patternsearch` 的 `validate_identifier` 做法），防注入。

## 7. 已确认的设计决策

1. **驱动方言：只支持 MySQL**（当前 `sqlapp.type = mysql`）。SQL 用反引号标识符 +
   `%s` 占位符 + `ON DUPLICATE KEY UPDATE`；换 sqlite/postgresql 需要改
   `database.py` 的 DDL 与写入语句。
2. **建表：启动自动建表 + 仓库留 `.sql` 脚本**（`sql.auto_create` 可关掉）。
3. **表名：`vnpy_portfolio_trade`**（可在设置文件里改）。
4. **入库口径：只写带 `reference` 的组合成交**，与现有"成交记录"页一致；外部下单仍靠
   POSITION 事件对账发现。
5. **历史回补：只回补当日**。CTP 只重放当日成交，启用本功能之前的成交无法回补，
   从启用之后开始积累。

## 8. 实现情况

| 文件 | 改动 |
|---|---|
| `vnpy_portfoliomanager/database.py` | 新增：`TRADE_COLUMNS`、`build_trade_row()`、DDL 生成、`TradeRepository`（建表/幂等写入/范围查询/DISTINCT/补名称） |
| `vnpy_portfoliomanager/settings.py` | 新增：`SqlSettings` 与标识符白名单校验 |
| `vnpy_portfoliomanager/engine.py` | 接 SqlApp（可缺失降级）、`init_trade_repository()`、`backfill_trades()`、`query_trades()`、`get_trade_filter_options()`、`update_trade_names()`；`save_setting()` 改为合并写入 |
| `vnpy_portfoliomanager/base.py` | 未改动（记账口径不变） |
| `vnpy_portfoliomanager/ui/widget.py` | 日期范围控件与快捷区间、后台查询线程、表格改为整体刷新 + 实时插入、CSV 导出、降级提示 |
| `script/create_vnpy_portfolio_trade.sql` | 新增：建表脚本（与代码生成的 DDL 一致） |
| `docs/trade_history_db_design.md` | 本文档 |
| `README.md` / `CHANGELOG.md` / `pyproject.toml` | 依赖、用法、版本说明 |

已做的验证：真 MySQL 建表/幂等写入/范围查询冒烟测试；Qt 端到端测试（实时插入、
日期范围切换、按组合筛选、筛选项刷新、未加载 SqlApp 的降级路径）。

## 9. 风险与已知取舍

- **不落库就不阻断交易**：DB 挂了/表结构不对，只写日志，仓位与盈亏照常算。
- **成交明细与盈亏对账口径**：DB 里只有成交流水，不含当日盈亏；盈亏仍来自
  `portfolio_manager_history.json`。两者按 `reference` + `trade_date` 可人工对账。
- **`trade_date` 口径**：取成交时间的日期部分。A 股无夜盘，等于自然日；将来跑期货
  夜盘时，CTP 的 `TradeDate` 本身就是"下一交易日"，此口径仍然成立。
- **`DECIMAL` 取回是 `Decimal`**：UI 显示时统一 `float()`/格式化，注意别直接参与
  和其他 float 的运算。
