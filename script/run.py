from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_ctp import CtpGateway
from vnpy_portfoliomanager import PortfolioManagerApp
from vnpy_sqlapp import SqlApp


def main() -> None:
    """Start Trader"""
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(CtpGateway)
    # SqlApp 必须先于本模块加载：成交记录落库与历史查询都依赖它（不加载则降级为内存模式）
    main_engine.add_app(SqlApp)
    main_engine.add_app(PortfolioManagerApp)

    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
