from __future__ import annotations

import time
import copy

from typing import TYPE_CHECKING 
if TYPE_CHECKING:
    import torch
    import pandas as pd
    import polars as pl
    from pfund.types.core import tModel
    from pfund.types.backtest import BacktestKwargs
    from pfeed.feeds.base_feed import BaseFeed
    from pfund.datas.data_base import BaseData
    from pfund.products.product_base import BaseProduct
    from pfund.strategies.strategy_base import BaseStrategy

import numpy as np

from pfund.strategies.strategy_base import BaseStrategy
from pfund.models.model_base import BaseModel
from pfund.indicators.talib_indicator import TalibIndicator
from pfund.managers.data_manager import get_resolutions_from_kwargs


# FIXME: clean up, should add to types?
_PFUND_BACKTEST_KWARGS = ['data_source', 'rollback_period', 'start_date', 'end_date']
_EVENT_DRIVEN_BACKTEST_KWARGS = ['resamples', 'shifts', 'auto_resample']


class BacktestMixin:
    # NOTE: custom __post_init__ is called in MetaStrategy/MetaModel
    # used to avoid confusing __init__ pattern in MetaStrategy/MetaModel
    # end result: only the __init__ of a normal class (real strategy/model class, not _BacktestStrategy/_BacktestModel) is called in the end
    def __post_init__(self, *args, **kwargs):
        # NOTE: signal_df = INDEX columns + signals generated by the current strategy/model
        self._signal_df = None
        self._signal_list = []
        self._signal_list_num = 0
        # case1: strategy is a dummy strategy
        # case2: model is using a dummy strategy as its only consumer
        self._is_dummy_strategy = self._check_if_dummy_strategy()
        self._is_signal_df_required = self._check_if_signal_df_required()
        self._is_append_to_df = not self._is_signal_df_required
        if isinstance(self, BaseStrategy):
            self._is_append_to_df = self._is_append_to_df and not self._is_dummy_strategy
        
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
    
    def _assert_consistent_signals(self):
        '''Asserts consistent signals from vectorized and event-driven backtesting, triggered in event-driven backtesting'''
        self.logger.debug(f"asserting {self.name}'s signals...")
        
        # since current strategy/model's signal_df is its consumer's prediction column
        # get the signal_df from the consumer
        consumer_df = self._consumers[0].df

        # load the signal_df dumped from vectorized backtesting
        self._is_signal_df_required = True
        self.load()

        if self.engine.data_tool == 'pandas':
            event_driven_signal_df = consumer_df[self.INDEX + self._signal_cols]
            # NOTE: since the loaded signal_df might have a few more rows than event_driven_signal_df
            # because the last bar is not pushed in event-driven backtesting.
            # truncate the signal_df to the same length as event_driven_signal_df
            vectorized_signal_df = self._signal_df.iloc[:len(event_driven_signal_df)]
        elif self.engine.data_tool == 'polars':
            event_driven_signal_df = consumer_df.select(self.INDEX + self._signal_cols)
            vectorized_signal_df = self._signal_df.slice(0, len(event_driven_signal_df))
        # TODO
        else:
            raise NotImplementedError
        self.dtl.assert_frame_equal(vectorized_signal_df, event_driven_signal_df)

    def _add_raw_df(self, data, df):
        if self._is_dummy_strategy and isinstance(self, BaseStrategy):
            return
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
        if self._is_dummy_strategy and isinstance(self, BaseStrategy):
            return
        return self.dtl.set_data_periods(datas, **kwargs)
    
    def _add_data_signature(self, *args, **kwargs):
        self._data_signatures.append((args, kwargs))
    
    def add_data(self, trading_venue, base_currency, quote_currency, ptype, *args, backtest: BacktestKwargs | None=None, train: dict | None=None, **kwargs) -> list[BaseData]:
        self._add_data_signature(trading_venue, base_currency, quote_currency, ptype, *args, backtest=backtest, train=train, **kwargs)
        
        backtest_kwargs, train_kwargs = backtest or {}, train or {}
        
        if backtest_kwargs:
            data_source = self._get_data_source(trading_venue, backtest_kwargs)
            feed = self.get_feed(data_source)
            kwargs = self._prepare_kwargs(feed, kwargs)
            
        datas = super().add_data(trading_venue, base_currency, quote_currency, ptype, *args, **kwargs)
        
        if train_kwargs:
            self._set_data_periods(datas, **train_kwargs)

        if backtest_kwargs:
            dfs = self.get_historical_data(feed, datas, kwargs, copy.deepcopy(backtest_kwargs))
            for data, df in zip(datas, dfs):
                self._add_raw_df(data, df)
        return datas
    
    def _add_consumer_datas(self, consumer: BaseStrategy | BaseModel, *args, use_consumer_data=False, **kwargs) -> list[BaseData]:
        consumer_datas = super()._add_consumer_datas(consumer, *args, use_consumer_data=use_consumer_data, **kwargs)
        dtl = consumer.dtl
        for data in consumer_datas:
            if dtl.has_raw_df(data):
                df = dtl.get_raw_df(data)
                self._add_raw_df(data, df)
        return consumer_datas
    
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
    
    def _get_data_source(self, trading_venue: str, backtest_kwargs: dict):
        from pfeed.const.common import SUPPORTED_DATA_FEEDS
        trading_venue = trading_venue.upper()
        # if data_source is not defined, use trading_venue as data_source
        if trading_venue in SUPPORTED_DATA_FEEDS and 'data_source' not in backtest_kwargs:
            backtest_kwargs['data_source'] = trading_venue
        assert 'data_source' in backtest_kwargs, "data_source must be defined"
        data_source = backtest_kwargs['data_source'].upper()
        assert data_source in SUPPORTED_DATA_FEEDS, f"{data_source=} not in {SUPPORTED_DATA_FEEDS}"
        return data_source
    
    def _prepare_kwargs(self, feed: BaseFeed, kwargs: dict):
        assert 'resolution' in kwargs or 'resolutions' in kwargs, f"data resolution(s) must be defined for {feed.name}"
        
        if self.engine.mode == 'vectorized':
            # clear kwargs that are only for event driven backtesting
            for k in _EVENT_DRIVEN_BACKTEST_KWARGS:
                if k == 'auto_resample':
                    kwargs[k] = {'by_official_resolution': False, 'by_highest_resolution': False}
                else:
                    kwargs[k] = {}
        elif self.engine.mode == 'event_driven':
            if 'is_skip_first_bar' not in kwargs:
                kwargs['is_skip_first_bar'] = False
        
            # add 'shifts' to kwargs:
            # HACK: since Yahoo Finance hourly data starts from 9:30 to 10:30 etc.
            # shift the start_ts (e.g. 9:00) of the bar to 30 minutes
            if feed.name == 'YAHOO_FINANCE':
                if 'shifts' not in kwargs:
                    kwargs['shifts'] = {}  # e.g. kwargs['shifts'] = {'1h': 30}
                for resolution in get_resolutions_from_kwargs(kwargs):
                    if resolution.is_hour() and repr(resolution) not in kwargs['shifts']:
                        # REVIEW: is there a better way to automatically determine the shifts? instead of hard-coding it to be 30 for yfinance here
                        kwargs['shifts'][repr(resolution)] = 30

        # override supported timeframes and periods using feed's
        # e.g. user might use IB as a broker for backtesting, but use Yahoo Finance as a data source
        # so IB's supported timeframes and periods should be overridden by Yahoo Finance's
        if hasattr(feed, 'SUPPORTED_TIMEFRAMES_AND_PERIODS'):
            kwargs['supported_timeframes_and_periods'] = feed.SUPPORTED_TIMEFRAMES_AND_PERIODS
        
        return kwargs
    
    @staticmethod
    def _remove_pfund_backtest_kwargs(kwargs: dict, backtest_kwargs: dict):
        '''backtest_kwargs include kwargs for both pfund backtesting and data feeds such as yfinance,
        clear pfund's kwargs for backtesting, only kwargs for e.g. yfinance are left
        '''
        for k in _PFUND_BACKTEST_KWARGS:
            assert k not in kwargs, f"kwarg '{k}' should be put inside 'backtest={{'{k}': '{kwargs[k]}'}}' "
            # clear PFund's kwargs for backtesting, only kwargs for e.g. yfinance are left
            if k in backtest_kwargs:
                del backtest_kwargs[k]
        return backtest_kwargs

    @staticmethod
    def get_feed(data_source: str) -> BaseFeed:
        from pfeed.feeds import YahooFinanceFeed, BybitFeed
        data_source = data_source.upper()
        if data_source == 'YAHOO_FINANCE':
            feed = YahooFinanceFeed()
        elif data_source == 'BYBIT':
            feed = BybitFeed()
        # TODO: other feeds
        else:
            raise NotImplementedError
        return feed

    def get_historical_data(
        self, 
        feed: BaseFeed, 
        datas: list[BaseData], 
        kwargs: dict,
        backtest_kwargs: dict
    ) -> list[pd.DataFrame | pl.LazyFrame]:
        rollback_period = backtest_kwargs.get('rollback_period', '1w')
        start_date = backtest_kwargs.get('start_date', '')
        end_date = backtest_kwargs.get('end_date', '')
        backtest_kwargs = self._remove_pfund_backtest_kwargs(kwargs, backtest_kwargs)
        
        dfs = []
        rate_limit = 3  # in seconds, 1 request every x seconds
        for n, data in enumerate(datas):
            if data.is_time_based():
                if data.is_resamplee():
                    continue
            product = data.product
            resolution = data.resolution
            pdt_or_symbol = product.symbol if feed.name == 'YAHOO_FINANCE' else product.pdt
            pfeed_df = feed.get_historical_data(
                pdt_or_symbol, 
                rollback_period=rollback_period, 
                start_date=start_date, 
                end_date=end_date, 
                resolution=repr(resolution), 
                data_tool=self.engine.data_tool,
                **backtest_kwargs
            )
            # NOTE: pfeed's df is a bit different: 
            # e.g. it has 'symbol' instead of 'product' for YahooFinanceFeed
            # and its 'product' doesn't have info for 'bkr' and 'exch' for BybitFeed
            df = self._convert_pfeed_df_to_pfund_df(pfeed_df, product)
            dfs.append(df)
            
            # don't sleep on the last one loop, waste of time
            if feed.name == 'YAHOO_FINANCE' and n != len(datas) - 1:
                time.sleep(rate_limit)
        return dfs
    
    def _convert_pfeed_df_to_pfund_df(self, df: pd.DataFrame | pl.DataFrame | pl.LazyFrame, product: BaseProduct) -> pd.DataFrame | pl.LazyFrame:
        try:
            import pandas as pd
        except ImportError:
            pd = None

        try:
            import polars as pl
        except ImportError:
            pl = None
        if isinstance(df, pd.DataFrame):
            if 'symbol' in df.columns:
                df = df.drop(columns=['symbol'])
            if 'product' in df.columns:
                df = df.drop(columns=['product'])
            df['product'] = repr(product)
        elif pl is not None and isinstance(df, (pl.DataFrame, pl.LazyFrame)):
            if isinstance(df, pl.DataFrame):
                df = df.lazy()
            df = df.drop(columns=['symbol', 'product'])
            df = df.with_columns(
                pl.lit(repr(product)).alias('product'),
            )
        # EXTEND
        else:
            raise NotImplementedError(f"{type(df)=} not supported")
        return df