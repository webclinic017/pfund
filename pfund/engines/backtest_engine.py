from __future__ import annotations
from typing import TYPE_CHECKING, Literal
if TYPE_CHECKING:
    try:
        import pandas as pd
    except ImportError:
        pd = None
    from pfeed.typing.literals import tDATA_TOOL
    from pfund.typing.core import tStrategy, tModel, tFeature, tIndicator
    from pfund.models.model_base import BaseModel
    from pfund.mixins.backtest_mixin import BacktestMixin

import os
import inspect
import time
import logging
from logging.handlers import QueueHandler, QueueListener

from tqdm import tqdm
from rich.console import Console
try:
    import polars as pl
except ImportError:
    pl = None

from pfund.backtest_history import BacktestHistory
from pfund.engines.base_engine import BaseEngine
from pfund.strategies.strategy_base import BaseStrategy
from pfund.brokers.broker_backtest import BacktestBroker
from pfund.const.enums import BacktestMode


class BacktestEngine(BaseEngine):
    def __new__(
        cls,
        *,
        env: Literal['BACKTEST', 'TRAIN']='BACKTEST',
        data_tool: tDATA_TOOL='pandas',
        mode: Literal['vectorized' | 'event_driven']='vectorized',
        use_signal_df: bool=False,
        assert_signals: bool=True,
        commit_to_git: bool=False,
        save_backtests: bool=True,
        retention_period: str='7d',
        num_chunks: int=1,
        use_ray: bool=False,
        num_cpus: int=8,
        **settings
    ):
        '''
        Args:
            use_signal_df:
                if True, uses signals from dumped signal_df in _next() instead of recalculating the signals. 
                This will make event-driven backtesting a LOT faster but inconsistent with live trading.
            assert_signals:
                if True, asserts signals generated by event-driven backtesting and vectorized backtesting are the same,
                by collecting results during event-driven backtesting.      
        '''
        if not hasattr(cls, 'mode'):
            cls.mode = BacktestMode[mode.upper()]
        if not hasattr(cls, 'use_signal_df'):
            cls.use_signal_df = use_signal_df
            if use_signal_df:
                Console().print(
                    f'Warning: {use_signal_df=} makes event-driven backtesting inconsistent with live trading',
                    style='bold'
                )
        if not hasattr(cls, 'assert_signals'):
            cls.assert_signals = assert_signals
            if cls.mode == BacktestMode.EVENT_DRIVEN and cls.use_signal_df and cls.assert_signals:
                raise ValueError('use_signal_df must be False when assert_signals=True in event-driven backtesting')
        if not hasattr(cls, 'commit_to_git'):
            cls.commit_to_git = commit_to_git
        if not hasattr(cls, 'save_backtests'):
            cls.save_backtests = save_backtests
        if not hasattr(cls, 'retention_period'):
            retention_period = retention_period.lower()
            assert retention_period[-1] in ['d', 'w', 'm', 'y'], 'retention_period must end with one of [d, w, m, y]'
            cls.retention_period = retention_period
        if not hasattr(cls, 'num_chunks'):
            cls.num_chunks = num_chunks
        if not hasattr(cls, 'use_ray'):
            cls.use_ray = use_ray
            if use_ray:
                logical_cpus = os.cpu_count()
                cls.num_cpus = min(num_cpus, logical_cpus)
                if cls.num_cpus > cls.num_chunks:
                    cls.num_chunks = cls.num_cpus
                    print(f'num_chunks is adjusted to {num_cpus} because {num_cpus=}')
        return super().__new__(
            cls,
            env,
            data_tool=data_tool,
            **settings
        )

    def __init__(
        self,
        *,
        env: Literal['BACKTEST', 'TRAIN']='BACKTEST',
        data_tool: tDATA_TOOL='pandas',
        mode: Literal['vectorized' | 'event_driven']='vectorized',
        use_signal_df: bool=False,
        assert_signals: bool=True,
        commit_to_git: bool=False,
        save_backtests: bool=True,
        retention_period: str='7d',
        num_chunks: int=1,
        use_ray: bool=False,
        num_cpus: int=8,
        **settings
    ):
        # avoid re-initialization to implement singleton class correctly
        if not hasattr(self, '_initialized'):
            super().__init__(
                env,
                data_tool=data_tool,
                **settings
            )
            self.history = BacktestHistory(self)
    
    # HACK: since python doesn't support dynamic typing, true return type should be subclass of BacktestMixin and tStrategy
    # write -> BacktestMixin | tStrategy for better intellisense in IDEs
    def add_strategy(
        self, 
        strategy: tStrategy, name: str='', 
        is_parallel=False
    ) -> BacktestMixin | tStrategy:
        from pfund.strategies.strategy_backtest import BacktestStrategy
        is_dummy_strategy_exist = '_dummy' in self.strategy_manager.strategies
        assert not is_dummy_strategy_exist, 'dummy strategy is being used for model backtesting, adding another strategy is not allowed'
        if is_parallel:
            is_parallel = False
            self.logger.warning(f'Parallel strategy is not supported in backtesting, {strategy.__class__.__name__} will be run in sequential mode')
        if type(strategy) is not BaseStrategy:
            assert name != '_dummy', 'dummy strategy is reserved for model backtesting, please use another name'
        name = name or strategy.__class__.__name__
        strategy = BacktestStrategy(type(strategy), *strategy._args, **strategy._kwargs)
        return super().add_strategy(strategy, name=name, is_parallel=is_parallel)

    def add_model(
        self, 
        model: tModel, 
        name: str='',
        min_data: None | int=None,
        max_data: None | int=None,
        group_data: bool=True,
        signal_cols: list[str] | None=None,
    ) -> BacktestMixin | tModel:
        '''Add model without creating a strategy (using dummy strategy)'''
        is_non_dummy_strategy_exist = bool([strat for strat in self.strategy_manager.strategies if strat != '_dummy'])
        assert not is_non_dummy_strategy_exist, 'Please use strategy.add_model(...) instead of engine.add_model(...) when a strategy is already created'
        if not (strategy := self.strategy_manager.get_strategy('_dummy')):
            strategy = self.add_strategy(BaseStrategy(), name='_dummy')
            strategy.set_flags(True)
            # add event driven functions to dummy strategy to avoid NotImplementedError in backtesting
            empty_function = lambda *args, **kwargs: None
            for func in strategy.REQUIRED_FUNCTIONS:
                setattr(strategy, func, empty_function)
        assert not strategy.models, 'Adding more than 1 model to dummy strategy in backtesting is not supported, you should train and dump your models one by one'
        model = strategy.add_model(
            model,
            name=name,
            min_data=min_data,
            max_data=max_data,
            group_data=group_data,
            signal_cols=signal_cols,
        )
        model.set_flags(True)
        return model
    
    def add_feature(
        self, 
        feature: tFeature, 
        name: str='',
        min_data: None | int=None,
        max_data: None | int=None,
        group_data: bool=True,
        signal_cols: list[str] | None=None,
    ) -> BacktestMixin | tFeature:
        return self.add_model(
            feature, 
            name=name, 
            min_data=min_data, 
            max_data=max_data, 
            group_data=group_data,
            signal_cols=signal_cols,
        )
    
    def add_indicator(
        self, 
        indicator: tIndicator, 
        name: str='',
        min_data: None | int=None,
        max_data: None | int=None,
        group_data: bool=True,
        signal_cols: list[str] | None=None,
    ) -> BacktestMixin | tIndicator:
        return self.add_model(
            indicator, 
            name=name, 
            min_data=min_data, 
            max_data=max_data, 
            group_data=group_data,
            signal_cols=signal_cols,
        )
    
    def add_broker(self, bkr: str):
        bkr = bkr.upper()
        if bkr in self.brokers:
            return self.get_broker(bkr)
        Broker = self.get_Broker(bkr)
        broker = BacktestBroker(Broker)
        bkr = broker.name
        self.brokers[bkr] = broker
        self.logger.debug(f'added {bkr=}')
        return broker
    
    def _assert_backtest_function(self, backtestee: BaseStrategy | BaseModel):
        assert self.mode == BacktestMode.VECTORIZED, 'assert_backtest_function() is only for vectorized backtesting'
        if not hasattr(backtestee, 'backtest'):
            raise Exception(f'class "{backtestee.name}" does not have backtest() method, cannot run vectorized backtesting')
        sig = inspect.signature(backtestee.backtest)
        params = list(sig.parameters.values())
        if not params or params[0].name != 'df':
            raise Exception(f'{backtestee.name} backtest() must have "df" as its first arg, i.e. backtest(self, df)')
        
    def run(self):
        for broker in self.brokers.values():
            broker.start()
        self.strategy_manager.start()
        backtest_results = {}
        error = ''
        try:
            for strat, strategy in self.strategy_manager.strategies.items():
                backtestee = strategy
                if strat == '_dummy':
                    if self.mode == BacktestMode.VECTORIZED:
                        continue
                    elif self.mode == BacktestMode.EVENT_DRIVEN:
                        # dummy strategy has exactly one model
                        model = list(strategy.models.values())[0]
                        backtestee = model
                backtest_result: dict = self._backtest(backtestee)
                backtest_results.update(backtest_result)
            # if only one backtest is run, return the backtest result without backtestee's name
            if len(backtest_results) == 1:
                backtest_results = backtest_results[backtestee.name]
        except Exception as err:
            error = str(err)
            self.logger.exception('Error in backtesting:')
        finally:
            self.end(reason=error)
        
        return backtest_results

    def _backtest(self, backtestee: BaseStrategy | BaseModel) -> dict:
        backtest_result = {}
        dtl = backtestee.dtl
        df = backtestee.get_df(copy=True)
        
        # Pre-Backtesting
        if self.mode == BacktestMode.VECTORIZED:
            self._assert_backtest_function(backtestee)
            df_chunks = []
        elif self.mode == BacktestMode.EVENT_DRIVEN:
            # NOTE: clear dfs so that strategies/models don't know anything about the incoming data
            backtestee.clear_dfs()
        else:
            raise NotImplementedError(f'Backtesting mode {self.mode} is not supported')
        
        
        # Backtesting
        if not self.use_ray:
            tqdm_desc = f'Backtesting {backtestee.name} (per chunk)'
            tqdm_bar = tqdm(total=self.num_chunks, desc=tqdm_desc, colour='green')
        else:
            ray_tasks = []
        start_time = time.time()
        for chunk_num, df_chunk in enumerate(dtl.iterate_df_by_chunks(df, num_chunks=self.num_chunks)):
            if self.use_ray:
                ray_tasks.append((df_chunk, chunk_num))
            else:
                if self.mode == BacktestMode.VECTORIZED:
                    df_chunk = dtl.preprocess_vectorized_df(df_chunk)
                    backtestee.backtest(df_chunk)
                    df_chunks.append(df_chunk)
                elif self.mode == BacktestMode.EVENT_DRIVEN:
                    df_chunk = dtl.preprocess_event_driven_df(df_chunk)
                    self._event_driven_backtest(df_chunk, chunk_num=chunk_num)
                tqdm_bar.update(1)
            
        if self.use_ray:
            import atexit
            import ray
            from ray.util.queue import Queue
            
            atexit.register(lambda: ray.shutdown())
            
            @ray.remote
            def _run_task(log_queue: Queue,  _df_chunk: pd.DataFrame | pl.LazyFrame, _chunk_num: int, _batch_num: int):
                try:
                    logger = backtestee.logger
                    if not logger.handlers:
                        logger.addHandler(QueueHandler(log_queue))
                        logger.setLevel(logging.DEBUG)
                    if self.mode == BacktestMode.VECTORIZED:
                        _df_chunk = dtl.preprocess_vectorized_df(_df_chunk, backtestee)
                        backtestee.backtest(_df_chunk)
                    elif self.mode == BacktestMode.EVENT_DRIVEN:
                        _df_chunk = dtl.preprocess_event_driven_df(_df_chunk)
                        self._event_driven_backtest(_df_chunk, chunk_num=_chunk_num, batch_num=_batch_num)
                except Exception:
                    logger.exception(f'Error in backtest-chunk{_chunk_num}-batch{_batch_num}:')
                    return False
                return True

            try:
                log_listener = None
                logger = backtestee.logger
                ray.init(num_cpus=self.num_cpus)
                print(f"Ray's num_cpus is set to {self.num_cpus}")
                batch_size = self.num_cpus
                log_queue = Queue()
                log_listener = QueueListener(log_queue, *logger.handlers, respect_handler_level=True)
                log_listener.start()
                batches = [ray_tasks[i: i + batch_size] for i in range(0, len(ray_tasks), batch_size)]
                with tqdm(
                    total=len(batches),
                    desc=f'Backtesting {backtestee.name} ({batch_size} chunks per batch)', 
                    colour='green'
                ) as tqdm_bar:
                    for batch_num, batch in enumerate(batches):
                        futures = [_run_task.remote(log_queue, *task, batch_num) for task in batch]
                        results = ray.get(futures)
                        if not all(results):
                            logger.warning(f'Some backtesting tasks in batch{batch_num} failed, check {logger.name}.log for details')
                        tqdm_bar.update(1)
            except Exception:
                logger.exception('Error in backtesting:')
            finally:
                if log_listener:
                    log_listener.stop()
                ray.shutdown()
        end_time = time.time()
        Console().print(f'Backtest elapsed time: {end_time - start_time:.3f}(s)', style='bold')
        
        
        # Post-Backtesting
        if backtestee.type == 'strategy':
            if self.mode == BacktestMode.VECTORIZED:
                df = dtl.postprocess_vectorized_df(df_chunks)
            # TODO
            elif self.mode == BacktestMode.EVENT_DRIVEN:
                pass
            backtest_history: dict = self.history.create(backtestee, df, start_time, end_time)
            backtest_result[backtestee.name] = backtest_history
        return backtest_result

    def _event_driven_backtest(self, df_chunk, chunk_num=0, batch_num=0):
        COMMON_COLS = ['ts', 'product', 'resolution', 'broker', 'is_quote', 'is_tick']
        if pl is not None and isinstance(df_chunk, pl.LazyFrame):
            df_chunk = df_chunk.collect().to_pandas()
        
        # OPTIMIZE: critical loop
        for row in tqdm(
            df_chunk.itertuples(index=False), 
            total=df_chunk.shape[0], 
            desc=f'Backtest-Chunk{chunk_num}-Batch{batch_num} (per row)', 
            colour='yellow'
        ):
            ts, product, resolution = row.ts, row.product, row.resolution
            broker = self.brokers[row.broker]
            data_manager = broker.dm
            if row.is_quote:
                # TODO
                raise NotImplementedError('Quote data is not supported in event-driven backtesting yet')
                quote = {}
                data_manager.update_quote(product, quote)
            elif row.is_tick:
                # TODO
                raise NotImplementedError('Tick data is not supported in event-driven backtesting yet')
                tick = {}
                data_manager.update_tick(product, tick)
            else:
                bar_cols = ['open', 'high', 'low', 'close', 'volume']
                bar = {
                    'resolution': resolution,
                    'data': {
                        'ts': ts,
                        'open': row.open,
                        'high': row.high,
                        'low': row.low,
                        'close': row.close,
                        'volume': row.volume,
                    },
                    'other_info': {
                        col: getattr(row, col) for col in row._fields
                        if col not in COMMON_COLS + bar_cols
                    },
                }
                data_manager.update_bar(product, bar, now=ts)
    
    # NOTE: end() vs stop()
    # end() means everything is done and NO state will be kept, can't be restarted
    # stop() means the process is stopped but the state is still kept, can be restarted
    def end(self, reason: str=''):
        for strat in list(self.strategy_manager.strategies):
            self.strategy_manager.stop(strat, reason=reason or 'finished backtesting')
            self.remove_strategy(strat)
        for broker in list(self.brokers.values()):
            broker.stop()
            self.remove_broker(broker.name)
        self._remove_singleton()
