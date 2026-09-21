# VeighNa框架的交易组合管理模块

<p align="center">
  <img src ="https://vnpy.oss-cn-shanghai.aliyuncs.com/vnpy-logo.png"/>
</p>

<p align="center">
    <img src ="https://img.shields.io/badge/version-1.1.0-blueviolet.svg"/>
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
- **成交记录**：全宽展示当日成交，支持按组合/合约筛选，并可导出CSV。

### 数据文件

除仓位存档外，模块会把每个交易日的盈亏快照写入 `portfolio_manager_history.json`（位于
`%USERPROFILE%\.vntrader`），跨日重启后曲线仍可延续。初始资金配置保存在
`portfolio_manager_setting.json` 的 `capitals` 字段中。

### 关于交易日

模块按国内期货惯例计算交易日：**20:00 之后的夜盘归属下一个交易日**，周末顺延到周一（没有节假日
日历，长假只能近似）。因此夜盘 21:00～次日凌晨的行情与其后的日盘算作同一天，不会把一夜拆成两个点。

同一个交易日内重启程序时，模块会从接口取回当日成交并重放一遍，重建当前仓位与成交成本；
但若重启时程序已错过当日开盘（存档中的开盘仓位不可信），仓位只能以接口实际回报为准。

## 安装

安装环境推荐基于4.0.0版本以上的【[**VeighNa Studio**](https://www.vnpy.com)】。

直接使用pip命令：

```
pip install vnpy_portfoliomanager
```


或者下载源代码后，解压后在cmd中运行：

```
pip install .
```
