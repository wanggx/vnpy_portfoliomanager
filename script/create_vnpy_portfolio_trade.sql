-- 投资组合成交记录表
--
-- 正常情况不需要手工执行：模块启动时会用同样的语句自动建表
-- （portfolio_manager_setting.json 里 sql.auto_create = false 可关掉）。
-- 本文件用于人工建表、DBA 评审或迁移到其它库。
--
-- 方言：MySQL（SqlApp 不做 SQL 方言转换，换 sqlite/postgresql 需要改
-- vnpy_portfoliomanager/database.py 的 DDL 与写入语句）。
-- 语句由 database.build_create_table_sql() 生成，两边必须保持一致。

CREATE TABLE IF NOT EXISTS `vnpy_portfolio_trade` (
    `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `trade_date` DATE NOT NULL COMMENT '交易日（本地时区，成交时间的日期部分）',
    `trade_time` DATETIME(3) NOT NULL COMMENT '成交时间，本地时区，毫秒精度',
    `reference` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '组合/策略名（vnpy reference）',
    `vt_symbol` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '本地代码，如 600000.SSE',
    `symbol` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '代码，如 600000',
    `exchange` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '交易所枚举名，如 SSE',
    `name` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '合约名称快照',
    `direction` VARCHAR(8) NOT NULL DEFAULT '' COMMENT '方向枚举名 LONG / SHORT',
    `offset` VARCHAR(16) NOT NULL DEFAULT '' COMMENT '开平枚举名 OPEN / CLOSE 等',
    `price` DECIMAL(20,6) NOT NULL DEFAULT 0 COMMENT '成交价',
    `volume` DECIMAL(20,4) NOT NULL DEFAULT 0 COMMENT '成交量（股/手）',
    `tradeid` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '柜台成交号',
    `orderid` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '柜台委托号',
    `vt_tradeid` VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'gateway_name.tradeid',
    `vt_orderid` VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'gateway_name.orderid',
    `gateway_name` VARCHAR(32) NOT NULL DEFAULT '' COMMENT '接口名，如 CTP',
    `mark` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '委托标记快照',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '首次入库时间',
    PRIMARY KEY (`id`),
    -- CTP 的 TradeID 只在当个交易日内唯一、跨日会重复，所以唯一键必须带日期
    UNIQUE KEY `uk_trade_date_tradeid` (`trade_date`, `vt_tradeid`),
    KEY `idx_date_reference` (`trade_date`, `reference`),
    KEY `idx_symbol_date` (`vt_symbol`, `trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='投资组合成交记录';

-- 常用查询
--
-- 某天的成交：
-- SELECT * FROM vnpy_portfolio_trade WHERE trade_date = '2026-09-23' ORDER BY trade_time;
--
-- 某组合某段时间的成交：
-- SELECT trade_date, trade_time, vt_symbol, direction, price, volume FROM vnpy_portfolio_trade
-- WHERE reference = '组合A' AND trade_date BETWEEN '2026-09-01' AND '2026-09-30'
-- ORDER BY trade_time;
--
-- 每日成交笔数：
-- SELECT trade_date, COUNT(*) AS trades, SUM(volume) AS volume
-- FROM vnpy_portfolio_trade GROUP BY trade_date ORDER BY trade_date DESC;
