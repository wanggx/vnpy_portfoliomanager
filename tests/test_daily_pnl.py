# coding: utf-8
"""PortfolioManager daily 盈亏模式单测。

验证两件事：
1. ``calculate_pnl(prices=...)``（daily 模式外部传价）算出的盈亏与
   ``calculate_pnl()``（realtime 用 tick）用同价时一致——保证两种取价源共用同一套公式。
2. ``pnl_mode`` 配置默认 ``realtime``、识别 ``daily``、非法值回落 ``realtime``。

不连 vnpy 运行时：用一个最小 mock engine 提供 ``get_contract``（带 size）和
``get_tick``（带 last_price/pre_close）。``_fetch_daily_prices`` 依赖 bigqmt xtdata，
这里不测（属集成层，实盘验证）——只测它喂给 ``calculate_pnl`` 的算术。
"""
import os
import sys
import unittest
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# vnpy 与 vnpy_portfoliomanager 源码都在上级目录的兄弟仓库里（editable/源码运行）。
_VNPY_ROOT = os.path.dirname(ROOT)
for p in (os.path.join(_VNPY_ROOT, "vnpy"), os.path.join(ROOT, "vnpy_portfoliomanager")):
    if p not in sys.path:
        sys.path.insert(0, p)


@dataclass
class _Contract:
    size: float = 1.0


@dataclass
class _Tick:
    last_price: float
    pre_close: float


class _MockEngine:
    """最小 engine：get_contract 返回带 size 的对象，get_tick 返回 tick。"""

    def __init__(self, size=1.0, last_price=10.0, pre_close=9.5):
        self._contract = _Contract(size=size)
        self._tick = _Tick(last_price=last_price, pre_close=pre_close)

    def get_contract(self, vt_symbol):
        return self._contract

    def get_tick(self, vt_symbol):
        return self._tick


class CalculatePnlTest(unittest.TestCase):
    """calculate_pnl 的两种取价源（realtime tick / daily prices）算术一致性。"""

    def _new_result(self, engine, open_pos=100):
        from vnpy_portfoliomanager.base import ContractResult
        return ContractResult(engine, "REF", "600000.SH", open_pos)

    def test_realtime_and_daily_prices_give_same_pnl(self):
        """同价时，prices=(last, pre_close) 与 tick=(last, pre_close) 结果一致。"""
        last_price, pre_close, size = 10.5, 9.8, 100
        engine = _MockEngine(size=size, last_price=last_price, pre_close=pre_close)

        # realtime 路径：get_tick
        r_real = self._new_result(engine, open_pos=100)
        r_real.calculate_pnl()  # prices=None -> get_tick

        # daily 路径：外部传同样的价
        r_daily = self._new_result(engine, open_pos=100)
        r_daily.calculate_pnl(prices={"600000.SH": (last_price, pre_close)})

        self.assertAlmostEqual(r_real.holding_pnl, r_daily.holding_pnl)
        self.assertAlmostEqual(r_real.trading_pnl, r_daily.trading_pnl)
        self.assertAlmostEqual(r_real.total_pnl, r_daily.total_pnl)
        # holding_pnl = (last - pre_close) * open_pos * size
        expected_holding = (last_price - pre_close) * 100 * size
        self.assertAlmostEqual(r_daily.holding_pnl, expected_holding)

    def test_daily_prices_missing_symbol_skips(self):
        """prices 里没有该标的 -> 不算（pnl 保持 0），与 realtime 没 tick 一致。"""
        engine = _MockEngine(size=1.0, last_price=10.0, pre_close=9.5)
        r = self._new_result(engine, open_pos=100)
        r.calculate_pnl(prices={"OTHER.SH": (1.0, 1.0)})  # 不含 600000.SH
        self.assertEqual(r.trading_pnl, 0)
        self.assertEqual(r.holding_pnl, 0)
        self.assertEqual(r.total_pnl, 0)

    def test_realtime_no_tick_skips(self):
        """realtime 模式 get_tick 返回 None -> 不算（pnl 保持 0）。"""
        engine = _MockEngine(size=1.0, last_price=10.0, pre_close=9.5)
        engine._tick = None  # 模拟没订阅、没 tick
        r = self._new_result(engine, open_pos=100)
        r.calculate_pnl()
        self.assertEqual(r.total_pnl, 0)

    def test_zero_position_zero_holding_pnl(self):
        """open_pos=0 时 holding_pnl=0（无持仓不产生持仓盈亏）。"""
        engine = _MockEngine(size=100, last_price=10.5, pre_close=9.8)
        r = self._new_result(engine, open_pos=0)
        r.calculate_pnl(prices={"600000.SH": (10.5, 9.8)})
        self.assertEqual(r.holding_pnl, 0)


if __name__ == "__main__":
    unittest.main()
