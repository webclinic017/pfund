import pytest

import pfund as pf
from talib import abstract as talib


@pytest.mark.smoke
class TestCore:
    def test_vectorized_backtesting_flow(self, mocker):
        FakeVectorizedStrategy = type('FakeVectorizedStrategy', (pf.Strategy,), {})
        engine = pf.BacktestEngine(mode='vectorized')
        strategy = engine.add_strategy(FakeVectorizedStrategy(), name='fake_vectorized_strategy', is_parallel=False)
        
        # TODO: move to conftest.py as fixture
        mock_get_historical_data = mocker.patch.object(strategy, 'get_historical_data')
        mock_data_tool = mocker.patch.object(strategy, 'data_tool')
        mock_engine_run = mocker.patch.object(engine, 'run')
        
        yf_datas = strategy.add_data(
            'IB', 'AAPL', 'USD', 'STK', resolutions=['1d'],
            backtest={
                'data_source': 'YAHOO_FINANCE',
                'start_date': '2024-01-01',
                'end_date': '2024-02-01',
            }
        )
        bybit_datas = strategy.add_data(
            'BYBIT', 'BTC', 'USDT', 'PERP', resolution='1m',
            backtest={
                'start_date': '2024-03-01',
                'end_date': '2024-03-01',
            }
        )
        
        # add models and indicators:
        indicator = strategy.add_indicator(pf.TALibIndicator(talib.SMA, timeperiod=3, price='close'), name='SMA', indicator_path='')
        
        
        engine.run()
        
        # check if datas are added to strategy correctly
        strategy_datas = [data for data_per_resolution in strategy.datas.values() for data in data_per_resolution.values()]
        assert yf_datas + bybit_datas == strategy_datas