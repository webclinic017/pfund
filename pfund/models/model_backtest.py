from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import torch
    from pfund.models.model_base import MachineLearningModel
    from pfund.types.core import tModel
    from pfund.models.model_base import BaseModel

import numpy as np
try:
    import pandas as pd
    import polars as pl
except ImportError:
    pass

from pfund.models.model_base import BaseFeature
from pfund.strategies.strategy_base import BaseStrategy
from pfund.mixins.backtest import BacktestMixin


def BacktestModel(Model: type[tModel], ml_model: MachineLearningModel, *args, **kwargs) -> BacktestMixin | tModel:
    class _BacktestModel(BacktestMixin, Model):
        def __getattr__(self, name):
            if hasattr(super(), name):
                return getattr(super(), name)
            else:
                class_name = Model.__name__
                raise AttributeError(f"'{class_name}' object has no attribute '{name}'")
            
        def to_dict(self):
            model_dict = super().to_dict()
            model_dict['class'] = Model.__name__
            model_dict['model_signature'] = self._model_signature
            model_dict['data_signatures'] = self._data_signatures
            return model_dict

        def add_consumer(self, consumer: BaseStrategy | BaseModel):
            is_dummy_strategy = isinstance(consumer, BaseStrategy) and consumer.name == '_dummy'
            if is_dummy_strategy:
                assert not self._consumers, f"{self.name} must have _dummy strategy as its only consumer"
            return super().add_consumer(consumer)
        
        def _check_if_dummy_strategy(self):
            if self._consumers:
                # NOTE: dummy strategy will always be the only consumer if it's added
                consumer = self._consumers[0]
                return isinstance(consumer, BaseStrategy) and consumer.name == '_dummy'
            else:
                return False
        
        def on_start(self):
            if self.engine.mode == 'vectorized':
                self.set_group_data(False)
            if self._is_signal_df_required and self._signal_df is None:
                print(
                    f"creating signal_df for '{self.name}' on the fly:\n"
                    "featurize() -> predict(X) -> signalize(X, pred_y)"
                )
                signal_df: pd.DataFrame | pl.LazyFrame = self.flow()
                self.set_signal_df(signal_df)
                # TODO: check if the signal_df is consistent with the current datas
            super().on_start()
            
        def flow(self) -> pd.DataFrame | pl.LazyFrame:
            X: pd.DataFrame | pl.LazyFrame = self.featurize()
            pred_y: torch.Tensor | np.ndarray = self.predict(X)
            signal_df: pd.DataFrame | pl.LazyFrame = self.signalize(X, pred_y)
            return signal_df
        
        def load(self) -> dict:
            obj: dict = super().load()
            signal_df = obj.get('signal_df', None)
            self.set_signal_df(signal_df)
            if self.is_model():
                assert self.ml_model, \
                f"Please make sure '{self.name}' was dumped "
                f"using '{self.type}.dump(signal_df)' correctly.\n"
                # FIXME: correct the link
                "Please refer to the doc for more details: https://pfund.ai"  
            return obj

        def dump(self, signal_df: pd.DataFrame | pl.LazyFrame):
            assert signal_df is not None, "signal_df cannot be None"
            obj = {'signal_df': signal_df}
            super().dump(obj)
        
        def clear_dfs(self):
            assert self.engine.mode == 'event_driven'
            if not self._is_signal_df_required:
                self._data_tool.clear_df()
            for model in self.models.values():
                model.clear_dfs()
            
        # FIXME: pandas specific
        def assert_consistent_signals(self):
            '''Asserts consistent model signals from vectorized and event-driven backtesting, triggered in event-driven backtesting'''
            import pandas.testing as pdt
            event_driven_signal = self.signal_df
            # set signal_df to None and load the vectorized_signal
            self.set_signal_df(None)
            self.load()
            assert self.signal_df is not None, f"Please dump your model '{self.name}' by calling model.dump() before running event-driven backtesting"
            vectorized_signal = self.signal_df
            # filter out the last date since event_driven_signal doesn't have it 
            vectorized_signal_ts_index = vectorized_signal.index.get_level_values('ts')
            last_date = vectorized_signal_ts_index.max()
            vectorized_signal = vectorized_signal[vectorized_signal_ts_index != last_date]

            for col in vectorized_signal.columns:
                pdt.assert_series_equal(vectorized_signal[col], event_driven_signal[col], check_exact=False, rtol=1e-5)
            
    try:       
        if not issubclass(Model, BaseFeature):
            return _BacktestModel(ml_model, *args, **kwargs)
        else:
            return _BacktestModel(*args, **kwargs)
    except TypeError as e:
        raise TypeError(
            f'if super().__init__() is called in {Model.__name__ }.__init__() (which is unnecssary), '
            'make sure it is called with args and kwargs, i.e. super().__init__(*args, **kwargs)'
        ) from e