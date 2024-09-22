from __future__ import annotations
from typing import TYPE_CHECKING, overload
if TYPE_CHECKING:
    import torch
    import pandas as pd
    import polars as pl
    from pfeed.feeds.base_feed import BaseFeed
    from pfeed.types.common_literals import tSUPPORTED_DATA_FEEDS
    from pfund.types.core import tModel
    from pfund.datas.data_base import BaseData
    from pfund.products.product_base import BaseProduct
    from pfund.strategies.strategy_base import BaseStrategy
    from pfund.types.common_literals import tSUPPORTED_TRADING_VENUES

import time

import numpy as np

from pfund.types.backtest import BacktestKwargs
from pfund.validations.backtest import validate_backtest_kwargs
from pfund.strategies.strategy_base import BaseStrategy
from pfund.models.model_base import BaseModel
from pfund.indicators.talib_indicator import TalibIndicator


# FIXME: clean up, should add to types?
_EVENT_DRIVEN_BACKTEST_KWARGS = ['resamples', 'shifts', 'auto_resample']


def vectorized(func):
    def wrapper(*args, **kwargs):
        if args[0].engine.mode == 'vectorized':
            return func(*args, **kwargs)
        else:
            raise Exception(f"{func.__name__}() is only available in vectorized backtesting.")
    return wrapper


def event_driven(func):
    def wrapper(*args, **kwargs):
        if args[0].engine.mode == 'event_driven':
            return func(*args, **kwargs)
        else:
            raise Exception(f"{func.__name__}() is only available in event driven backtesting.")
    return wrapper


class BacktestMixin:
    # NOTE: custom __post_init__ is called in MetaStrategy/MetaModel
    # used to avoid confusing __init__ pattern in MetaStrategy/MetaModel
    # end result: only the __init__ of a normal class (real strategy/model class, not _BacktestStrategy/_BacktestModel) is called in the end
    def __post_init__(self, *args, **kwargs):
        # NOTE: signal_df = INDEX columns + signals generated by the current strategy/model
        self._signal_df = None
        self._signal_list = []
        self._signal_list_num = 0
            
        self._is_dummy_strategy = False
        self._is_signal_df_required = self._check_if_signal_df_required()
        self._is_append_to_df = self._check_if_append_to_df()
            
        # stores signatures for backtest history tracking
        self._data_signatures = []
        if isinstance(self, BaseStrategy):
            self._strategy_signature = (args, kwargs)
        elif isinstance(self, BaseModel):
            # convert the talib function (can't be dumped) to its info dict
            if isinstance(self, TalibIndicator):
                talib_func = args[0]
                self._model_signature = (talib_func.info, *args[1:], kwargs)
            else:
                self._model_signature = (args, kwargs)
        else:
            raise NotImplementedError('BacktestMixin should only be used in _BacktestStrategy or _BacktestModel')
    
    def on_stop(self):
        super().on_stop()
        if self.engine.mode == 'event_driven' and self.engine.assert_signals and self._has_signal_df():
            self._assert_consistent_signals()
            
    def _next(self, data: BaseData) -> torch.Tensor | np.ndarray:
        if not self._is_signal_df_required:
            new_pred = super()._next(data)
        else:
            new_pred = self._signal_list[self._signal_list_num]
            self._signal_list_num += 1
        return new_pred
    
    def set_flags(self, is_dummy_strategy: bool):
        # case1: strategy is a dummy strategy
        # case2: model is using a dummy strategy as its only consumer
        self._is_dummy_strategy = is_dummy_strategy
        self._is_signal_df_required = self._check_if_signal_df_required()
        self._is_append_to_df = self._check_if_append_to_df()
    
    def _append_to_df(self, data: BaseData, **kwargs):
        if self._is_append_to_df:
            return self.dtl.append_to_df(data, self.predictions, **kwargs)
    
    def _check_if_signal_df_required(self) -> bool:
        if self._is_dummy_strategy:
            return False
        elif self.engine.mode == 'vectorized':
            return True
        elif self.engine.mode == 'event_driven':
            return self.engine.use_signal_df
    
    def _check_if_append_to_df(self):
        if isinstance(self, BaseStrategy):
            return not self._is_signal_df_required and not self._is_dummy_strategy
        else:
            return not self._is_signal_df_required
    
    def _has_signal_df(self):
        return ( isinstance(self, BaseStrategy) and self.is_sub_strategy() ) or isinstance(self, BaseModel)
    
    def signalize(
        self, 
        X: pd.DataFrame | pl.LazyFrame,
        pred_y: torch.Tensor | np.ndarray,
    ) -> pd.DataFrame | pl.LazyFrame:
        try:
            import torch
        except ImportError:
            torch = None
        if torch is not None and isinstance(pred_y, torch.Tensor):
            pred_y = pred_y.detach().numpy() if pred_y.requires_grad else pred_y.numpy()
        signal_cols = self.get_signal_cols()
        signal_df: pd.DataFrame | pl.LazyFrame = self.dtl.signalize(X, pred_y, columns=signal_cols)
        return signal_df
    
    def _set_signal_df(self, signal_df: pd.DataFrame | pl.LazyFrame):
        assert signal_df.shape[0] == self.df.shape[0], f"{signal_df.shape[0]=} != {self.df.shape[0]=}"
        nan_columns = self.dtl.get_nan_columns(signal_df)
        assert not nan_columns, f"{self.name} signal_df has all NaN values in columns: {nan_columns}"
        self._signal_list = signal_df.drop(columns=self.INDEX).to_numpy().tolist()
        self._signal_df = signal_df
    
    @event_driven
    def _assert_consistent_signals(self):
        '''Asserts consistent signals from vectorized and event-driven backtesting, triggered in event-driven backtesting'''
        self.logger.debug(f"asserting {self.name}'s signals...")
        
        # since current strategy/model's signal_df is its consumer's prediction column
        # get the signal_df from the consumer
        consumer_df = self._consumers[0].df

        # load the signal_df dumped from vectorized backtesting
        self._is_signal_df_required = True
        self.load()

        if self.data_tool.name == 'pandas':
            event_driven_signal_df = consumer_df[self.INDEX + self._signal_cols]
            # NOTE: since the loaded signal_df might have a few more rows than event_driven_signal_df
            # because the last bar is not pushed in event-driven backtesting.
            # truncate the signal_df to the same length as event_driven_signal_df
            vectorized_signal_df = self._signal_df.iloc[:len(event_driven_signal_df)]
        elif self.data_tool.name == 'polars':
            event_driven_signal_df = consumer_df.select(self.INDEX + self._signal_cols)
            vectorized_signal_df = self._signal_df.slice(0, len(event_driven_signal_df))
        # TODO
        else:
            raise NotImplementedError
        self.dtl.assert_frame_equal(vectorized_signal_df, event_driven_signal_df)

    def _add_raw_df(self, data, df):
        return self.dtl.add_raw_df(data, df)
    
    def _prepare_df(self):
        if self._is_dummy_strategy and isinstance(self, BaseStrategy):
            return
        ts_col_type = 'timestamp' if self.engine.mode == 'event_driven' else 'datetime'
        self.dtl.prepare_df(ts_col_type=ts_col_type)
        if self._is_signal_df_required:
            self._merge_signal_dfs_with_df()
    
    def _merge_signal_dfs_with_df(self):
        '''Merge df with signal dfs from all listeners (strategies/models)'''
        if isinstance(self, BaseStrategy):
            if signal_dfs := [strategy._signal_df for strategy in self.strategies.values()]:
                self.dtl.merge_signal_dfs_with_df(signal_dfs)
        if signal_dfs := [model._signal_df for model in self.models.values()]:
            self.dtl.merge_signal_dfs_with_df(signal_dfs)
    
    def clear_dfs(self):
        assert self.engine.mode == 'event_driven'
        if not self._is_signal_df_required:
            self._data_tool.clear_df()
        if isinstance(self, BaseStrategy):
            for strategy in self.strategies.values():
                strategy.clear_dfs()
        for model in self.models.values():
            model.clear_dfs()
    
    # TODO
    def _set_data_periods(self, datas, **kwargs):
        return self.dtl.set_data_periods(datas, **kwargs)
    
    def _add_data_signature(self, *args, **kwargs):
        self._data_signatures.append((args, kwargs))
    
    @validate_backtest_kwargs
    def add_data(
        self, 
        trading_venue: tSUPPORTED_TRADING_VENUES, 
        product: str,
        resolutions: list[str] | str,
        backtest: BacktestKwargs | None=None,
        train: dict | None=None,
        **kwargs
    ) -> list[BaseData]:
        self._add_data_signature(trading_venue, product, resolutions, backtest=backtest, train=train, **kwargs)
        feed: BaseFeed = self.get_feed(backtest['data_source'])
        kwargs = self._prepare_kwargs(feed, resolutions, kwargs)
        datas = super().add_data(trading_venue, product, resolutions, **kwargs)
        dfs = self.get_historical_data(feed, datas, backtest)
        for data, df in zip(datas, dfs):
            self._add_raw_df(data, df)
        if train:
            self._set_data_periods(datas, **train)
        return datas
    
    def _add_consumers_datas_if_no_data(self) -> list[BaseData]:
        '''Add consumers' raw dfs if no datas'''
        datas = super()._add_consumers_datas_if_no_data()
        for data in datas:
            for consumer in self._consumers:
                dtl = consumer.dtl
                if dtl.has_raw_df(data):
                    df = dtl.get_raw_df(data)
                    self._add_raw_df(data, df)
                    break
        return datas
    
    @overload
    def dump(self, signal_df: pd.DataFrame | pl.LazyFrame): ...
        
    def add_model(
        self, 
        model: tModel, 
        name: str='',
        min_data: None | int=None,
        max_data: None | int=None,
        group_data: bool=True,
        signal_cols: list[str] | None=None,
    ) -> BacktestMixin | tModel:
        from pfund.models.model_backtest import BacktestModel
        name = name or model.get_default_name()
        model = BacktestModel(type(model), model.ml_model, *model._args, **model._kwargs)
        return super().add_model(
            model, 
            name=name, 
            min_data=min_data, 
            max_data=max_data, 
            group_data=group_data,
            signal_cols=signal_cols,
        )
    
    def _prepare_kwargs(self, feed: BaseFeed, resolutions, kwargs: dict):
        if self.engine.mode == 'vectorized':
            # clear kwargs that are only for event driven backtesting
            for k in _EVENT_DRIVEN_BACKTEST_KWARGS:
                if k == 'auto_resample':
                    kwargs[k] = {'by_official_resolution': False, 'by_highest_resolution': False}
                else:
                    kwargs[k] = {}
        # FIXME
        elif self.engine.mode == 'event_driven':
            if 'is_skip_first_bar' not in kwargs:
                kwargs['is_skip_first_bar'] = False
        
            # add 'shifts' to kwargs:
            # HACK: since Yahoo Finance hourly data starts from 9:30 to 10:30 etc.
            # shift the start_ts (e.g. 9:00) of the bar to 30 minutes
            if feed.name == 'YAHOO_FINANCE':
                if 'shifts' not in kwargs:
                    kwargs['shifts'] = {}  # e.g. kwargs['shifts'] = {'1h': 30}
                for resolution in resolutions:
                    if resolution.is_hour() and repr(resolution) not in kwargs['shifts']:
                        # REVIEW: is there a better way to automatically determine the shifts? instead of hard-coding it to be 30 for yfinance here
                        kwargs['shifts'][repr(resolution)] = 30

        # override supported timeframes and periods using feed's
        # e.g. user might use IB as a broker for backtesting, but use Yahoo Finance as a data source
        # so IB's supported timeframes and periods should be overridden by Yahoo Finance's
        if hasattr(feed, 'SUPPORTED_TIMEFRAMES_AND_PERIODS'):
            kwargs['supported_timeframes_and_periods'] = feed.SUPPORTED_TIMEFRAMES_AND_PERIODS
        
        return kwargs
    
    def get_feed(self, data_source: tSUPPORTED_DATA_FEEDS) -> BaseFeed:
        from pfeed.feeds import YahooFinanceFeed, BybitFeed
        data_source = data_source.upper()
        if data_source == 'YAHOO_FINANCE':
            feed = YahooFinanceFeed(data_tool=self.data_tool.name)
        elif data_source == 'BYBIT':
            feed = BybitFeed(data_tool=self.data_tool.name)
        # TODO: other feeds
        else:
            raise NotImplementedError
        return feed

    def get_historical_data(
        self, 
        feed: BaseFeed, 
        datas: list[BaseData], 
        backtest: BacktestKwargs
    ) -> list[pd.DataFrame | pl.LazyFrame]:
        dfs = []
        rate_limit = 3  # in seconds, 1 request every x seconds
        # BacktestKwargs include kwargs for both pfund backtesting and data feeds such as yfinance,
        # clear pfund's kwargs for backtesting, only kwargs for e.g. yfinance are left 
        feed_kwargs = {k: backtest[k] for k in backtest if k not in BacktestKwargs.__annotations__}
        for n, data in enumerate(datas):
            if data.is_time_based():
                if data.is_resamplee():
                    continue
            product = data.product
            if feed.name == 'YAHOO_FINANCE':
                feed_kwargs['product'] = product.pdt
            df = feed.get_historical_data(
                product.symbol if feed.name == 'YAHOO_FINANCE' else product.pdt, 
                resolution=data.resol,
                rollback_period=backtest.get('rollback_period', ''), 
                start_date=backtest.get('start_date', ''), 
                end_date=backtest.get('end_date', ''), 
                **feed_kwargs
            )
            dfs.append(df)
            
            # don't sleep on the last one loop, waste of time
            if feed.name == 'YAHOO_FINANCE' and n != len(datas) - 1:
                time.sleep(rate_limit)
        return dfs
    