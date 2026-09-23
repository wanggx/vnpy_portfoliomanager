# VeighNa框架的交易组合管理模块

<p align="center">
  <img src ="https://vnpy.oss-cn-shanghai.aliyuncs.com/vnpy-logo.png"/>
</p>

<p align="center">
    <img src ="https://img.shields.io/badge/version-1.3.0-blueviolet.svg"/>
    <img src ="https://img.shields.io/badge/platform-windows|linux|macos-yellow.svg"/>
    <img src ="https://img.shields.io/badge/python-3.10|3.11|3.12|3.13-blue.svg" />
    <img src ="https://img.shields.io/github/license/vnpy/vnpy.svg?color=orange"/>
</p>

## 说明

PortfolioManager是用于交易组合跟踪管理的功能模块，以独立的策略交易组合（子账户）为基础，提供委托成交记录管理、交易仓位自动跟踪以及每日盈亏实时统计功能。

### 界面说明

模块界面采用页签式布局，顶部工具条保留组合汇总信息与刷新频率设置：

- **收益曲线**：多组合累计盈亏/累计收益率曲线对比，风格与市场情绪模块保持一致（纯黑背景、
  右侧数值轴）；鼠标移动显示十字光标与各组合数值，滚动滚轮缩放、拖拽平移。下方汇总表可设置
  每个组合的初始资金（**默认 50 万**，可直接改；改成 0 则不显示该组合的收益率），并列的“显示”列
  可勾选需要画在曲线上的组合（默认勾选前三个，组合较多时只画关注的那几条）。
- **持仓明细**：按组合、合约展示开盘仓位、当前仓位、交易盈亏、持仓盈亏与总盈亏。
- **成交记录**：按日期范围查询成交（默认当日，可查历史），支持按组合/合约筛选及导出CSV。
  查询走后台线程，不阻塞界面。

### 成交记录数据库

成交记录默认落库（需要 [SqlApp](https://github.com/vnpy/vnpy_sqlapp)，即 `vnpy_sqlapp`，
在 Trader 里要**先于本模块加载**），表名 `vnpy_portfolio_trade`，由 SqlApp 的配置决定
落到哪个库（`sqlapp.type` / `sqlapp.*`，留空回退到 vn.py 全局 `database.*`）：

- 表在模块启动时自动创建（`CREATE TABLE IF NOT EXISTS`），不建时会把
  `sql` 里的 `auto_create` 置为 `false`，改用手工执行 `script/create_vnpy_portfolio_trade.sql`；
- 只记录**带组合标记**的成交（即本模块认领的成交），外部下单仍靠持仓对账发现；
- 唯一键是 `(trade_date, vt_tradeid)`：CTP 的 `TradeID` 只在当个交易日内唯一、跨日会重复，
  所以不能只用 `vt_tradeid` 做唯一键；
- 写入是幂等的：重启、CTP 登录重放当日成交、当日成交回放都不会重复计数；
- 未加载 SqlApp 时自动降级：成交只留在内存（只显示当日，重启即失），状态栏会提示。

可在 `portfolio_manager_setting.json` 里配置（默认值不落盘）：

```json
{
    "sql": {
        "enabled": true,
        "table": "vnpy_portfolio_trade",
        "auto_create": true,
        "max_rows": 5000
    }
}
```

`max_rows` 是单次查询的行数上限，命中时状态栏会提示缩小日期范围。

### 数据文件

除仓位存档外，模块会把每个交易日的盈亏快照写入 `portfolio_manager_history.json`（位于
`%USERPROFILE%\\.vntrader`），跨日重启后曲线仍可延续。初始资金配置保存在
`portfolio_manager_setting.json` 的 `capitals` 字段中（与 `sql` 配置同文件，模块写入时会
合并保留其它字段）。

### 关于交易日

模块按**自然日**计算交易日，遇周六日顺延到周一（没有节假日日历，长假只能近似）。

同一个交易日内重启程序时，模块会从接口取回当日成交并重放一遍，重建当前仓位与成交成本；
但若重启时程序已错过当日开盘（存档中的开盘仓位不可信），仓位只能以接口实际回报为准。

## 安装

安装环境推荐基于4.0.0版本以上的【[**VeighNa Studio**](https://www.vnpy.com)】。

成交记录落库需要额外安装 `vnpy_sqlapp`（可选，不装则自动降级为内存模式）：

```
pip install vnpy_sqlapp
```

直接使用pip命令：

```
pip install vnpy_portfoliomanager
```


或者下载源代码后，解压后在cmd中运行：

```
pip install .
```
