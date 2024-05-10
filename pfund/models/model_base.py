from __future__ import annotations

import os
import sys
import importlib
from pathlib import Path
from abc import ABC, abstractmethod
from collections import defaultdict

from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    import pandas as pd
    import polars as pl
    import torch
    import torch.nn as nn
    from sklearn.base import BaseEstimator, ClassifierMixin, RegressorMixin
    from sklearn.pipeline import Pipeline
    from pfund.strategies.strategy_base import BaseStrategy
    from pfund.models import PytorchModel, SklearnModel
    from pfund.indicators.indicator_base import TaFunction, TalibFunction
    from pfund.datas.data_base import BaseData
    from pfund.datas.data_quote import QuoteData
    from pfund.datas.data_tick import TickData
    from pfund.datas.data_bar import BarData
    from pfund.types.core import tModel, tFeature, tIndicator
    MachineLearningModel = Union[
        nn.Module,
        BaseEstimator,
        ClassifierMixin,
        RegressorMixin, 
        Pipeline,
        TaFunction,  # ta.utils.IndicatorMixin
        TalibFunction,
        Any,
    ]

import joblib
import numpy as np
from rich.console import Console

try:
    import torch.nn as nn
except ImportError:
    nn = None

try:
    from sklearn.base import BaseEstimator, ClassifierMixin, RegressorMixin
    from sklearn.pipeline import Pipeline
except ImportError:
    sklearn = None

from pfund.datas.resolution import Resolution
from pfund.models.model_meta import MetaModel
from pfund.products.product_base import BaseProduct
from pfund.utils.utils import short_path, get_engine_class, load_yaml_file, convert_ts_to_dt
from pfund.plogging import create_dynamic_logger


class BaseModel(ABC, metaclass=MetaModel):
    
    _file_path: Path | None = None  # Get the file path where the model was defined
    config = {}
    
    @classmethod
    def load_config(cls, config: dict | None=None):
        if config:
            cls.config = config
        elif cls._file_path:
            for file_name in ['config.yml', 'config.yaml']:
                if config := load_yaml_file(cls._file_path.parent / file_name):
                    cls.config = config
                    break
    
    def load_params(self, params: dict | None=None):
        if params:
            self.params = params
        elif self._file_path:
            for file_name in ['params.yml', 'params.yaml']:
                if params := load_yaml_file(self._file_path.parent / file_name):
                    self.params = params
                    break
    
    def __new__(cls, *args, **kwargs):
        if not cls._file_path:
            module = sys.modules[cls.__module__]
            if strategy_file_path := getattr(module, '__file__', None):
                cls._file_path = Path(strategy_file_path)
                cls.load_config()
        return super().__new__(cls)
    
    def __init__(self, ml_model: MachineLearningModel, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs
        self.ml_model = ml_model  # user-defined machine learning model
        self.name = self.mdl = self.get_default_name()
        self.Engine = get_engine_class()
        self.engine = self.Engine()
        data_tool: str = self.Engine.data_tool
        DataTool = getattr(importlib.import_module(f'pfund.data_tools.data_tool_{data_tool}'), f'{data_tool.capitalize()}DataTool')
        self._data_tool = DataTool()
        self.logger = None
        self._is_running = False
        self._is_ready = defaultdict(bool)  # {data: bool}
        self.products = defaultdict(dict)  # {trading_venue: {pdt1: product1, pdt2: product2} }
        self.datas = defaultdict(dict)  # {product: {repr(resolution): data}}
        self._listeners = defaultdict(list)  # {data: model}
        self._consumers = []  # strategies/models that consume this model
        self._min_data = {}  # {data: int}
        self._max_data = {}  # {data: int}}
        self._num_data = defaultdict(int)  # {data: int}
        self._group_data = True
        self.type = 'model'
        
        self.models = {}
        # NOTE: current model's signal is consumer's prediction
        self.predictions = {}  # {model_name: pred_y}
        self.signals = {}  # {data: signal}, signal = output of predict()
        self._signal_cols = []
        self._num_signal_cols = 0
        
        self.params = {}
        self.load_params()

    @abstractmethod
    def predict(self, X: pd.DataFrame | pl.LazyFrame, *args, **kwargs) -> torch.Tensor | np.ndarray:
        pass
    
    def featurize(self) -> pd.DataFrame | pl.LazyFrame:
        Console().print(
            f"WARNING: '{self.name}' is using the default featurize(), "
            "which assumes X = df, it could be a wrong input for predict(X).\n"
            f"It is highly recommended to override featurize() in your '{self.name}'.",
            style='bold magenta'
        )
        return self.get_df()
    
    # NOTE: df = INDEX columns + predictions generated by other models
    @property
    def df(self):
        return self.get_df(copy=False)
   
    @property
    def dtl(self):
        return self._data_tool
    data_tool = dtl
    
    @property
    def features(self):
        return {mdl: model for mdl, model in self.models.items() if model.is_feature()}
    
    @property
    def extractions(self):
        return {pred_name: prediction for pred_name, prediction in self.predictions.items() if self.models[pred_name.split('-')[0]].is_feature()}
    
    @property
    def indicators(self):
        return {mdl: model for mdl, model in self.models.items() if model.is_indicator()}
    
    @property
    def indications(self):
        return {pred_name: prediction for pred_name, prediction in self.predictions.items() if self.models[pred_name.split('-')[0]].is_indicator()}
    
    @property
    def INDEX(self):
        return self._data_tool.INDEX
    
    @property
    def GROUP(self):
        return self._data_tool.GROUP
    
    def get_df(
        self, 
        start_idx: int=0, 
        end_idx: int | None=None, 
        product: str | None=None, 
        resolution: str | None=None, 
        copy: bool=True
    ):
        return self._data_tool.get_df(
            start_idx=start_idx, 
            end_idx=end_idx, 
            product=product,
            resolution=resolution,
            copy=copy
        )
    
    @staticmethod
    def dt(ts: float):
        return convert_ts_to_dt(ts)
    
    def is_running(self):
        return self._is_running
    
    def is_ready(self, data: BaseData) -> bool:
        if not self._is_ready[data]:
            self._num_data[data] += 1
            if self._num_data[data] >= self._min_data[data]:
                self._is_ready[data] = True
        return self._is_ready[data]
    
    def to_dict(self):
        return {
            'class': self.__class__.__name__,
            'name': self.name,
            'config': self.config,
            'params': self.params,
            'ml_model': self.ml_model,
            'datas': [repr(data) for product in self.datas for data in self.datas[product].values()],
            'models': [model.to_dict() for model in self.models.values()],
        }
    
    def get_default_name(self):
        return self.__class__.__name__
    
    def get_default_signal_cols(self, num_cols: int):
        if num_cols == 1:
            columns = [self.name]
        else:
            columns = [f'{self.name}-{i}' for i in range(num_cols)]
        return columns
   
    def get_signal_cols(self):
        return self._signal_cols
    
    def set_signal_cols(self, columns: list[str]):
        self._signal_cols = [f'{self.name}-{col}' if not col.startswith(self.name) else col for col in columns]
        self._num_signal_cols = len(columns)
    
    def _next(self, data: BaseData) -> torch.Tensor | np.ndarray:
        '''Returns the next prediction in event-driven manner.'''
        if data in self.signals:
            return self.signals[data]
        
        if not self.is_ready(data):
            return [np.nan] * self._num_signal_cols
        
        # if max_data = -1 (include all data), then start_idx = 0
        # if max_data = +x, then start_idx = -x
        start_idx = min(-self._max_data[data], 0)

        if self._group_data:
            product_filter = repr(data.product)
            resolution_filter = data.resol
        else:
            product_filter = resolution_filter = None

        # if group_data, X is per product and resolution -> X[-start_idx:];
        # if not, X is the whole data -> X[-start_idx:]
        X = self.get_df(
            start_idx=start_idx,
            product=product_filter,
            resolution=resolution_filter,
            copy=False
        )
        
        pred_y = self.predict(X)
        new_pred = pred_y[-1]
        self.signals[data] = new_pred
        
        return new_pred
            
    def _convert_min_max_data_to_dict(self):
        '''Converts min_data and max_data from int to dict[product, dict[resolution, int]]'''
        is_min_data_int = is_max_data_int = False
        if isinstance(self._min_data, int):
            is_min_data_int = True
            min_data = self._min_data
            self._min_data = {}
        if isinstance(self._max_data, int):
            is_max_data_int = True
            max_data = self._max_data
            self._max_data = {}
        
        for data in self.get_datas():
            # if not int = min_data is already set up by user calling set_min_data() explicitly, i.e. check if set up correctly
            if not is_min_data_int:
                assert data in self._min_data, f"{data} not found in {self._min_data=}, make sure set_min_data() is called correctly"
            else:
                self._min_data[data] = min_data
                
            # if not int = max_data is already set up by user calling set_max_data() explicitly, i.e. check if set up correctly
            if not is_max_data_int:
                assert data in self._max_data, f"{data} not found in {self._max_data=}, make sure set_max_data() is called correctly"
            else:
                self._max_data[data] = max_data
    
            max_data = self._max_data[data]
            min_data = self._min_data[data]
            
            # NOTE: -1 means include all data
            if max_data == -1:
                max_data = sys.float_info.max
                
            assert min_data >= 1, f'{min_data=} for {data} must be >= 1'
            assert max_data >= min_data, f'{max_data=} for {data} must be >= {min_data=}'
    
    def get_model_type_of_ml_model(self) -> PytorchModel | SklearnModel | BaseModel:
        if nn is not None and isinstance(self.ml_model, nn.Module):
            from pfund.models import PytorchModel
            Model = PytorchModel
        elif sklearn is not None and isinstance(self.ml_model, (BaseEstimator, ClassifierMixin, RegressorMixin, Pipeline)):
            from pfund.models import SklearnModel
            Model = SklearnModel
        else:
            Model = BaseModel
        return Model
    
    def add_consumer(self, consumer: BaseStrategy | BaseModel):
        '''
        when a model is added to a strategy, consumer is a strategy
        when a model is added to a model, consumer is a model
        '''
        if consumer not in self._consumers:
            self._consumers.append(consumer)

    def create_logger(self):
        if self.is_indicator():
            type_ = 'indicator'
        elif self.is_feature():
            type_ = 'feature'
        else:
            type_ = 'model'
        self.logger = create_dynamic_logger(self.name, type_)
        
    def set_name(self, name: str):
        self.name = self.mdl = name
    
    def set_min_data(self, min_data: int | dict[BaseData, int]):
        self._min_data = min_data

    def set_max_data(self, max_data: None | int | dict[BaseData, int]):
        self._max_data = max_data if max_data else self._min_data
    
    def set_group_data(self, group_data: bool):
        self._group_data = group_data
    
    def _get_file_path(self, extension='.joblib'):
        path = f'{self.engine.config.artifact_path}/{self.name}'
        file_name = f'{self.name}{extension}'
        if not os.path.exists(path):
            os.makedirs(path)
        return f"{path}/{file_name}"
    
    # REVIEW
    def _assert_no_missing_datas(self, obj):
        loaded_datas = {data for product in obj['datas'] for data in obj['datas'][product].values()}
        added_datas = {data for product in self.datas for data in self.datas[product].values()}
        if loaded_datas != added_datas:
            missing_datas = loaded_datas - added_datas
            raise Exception(f"missing data {missing_datas} in model '{self.name}', please use add_data() to add them back")
    
    def load(self) -> dict:
        file_path = self._get_file_path()
        if os.path.exists(file_path):
            obj: dict = joblib.load(file_path)
            self.ml_model = obj['ml_model']
            self._assert_no_missing_datas(obj)
            self.logger.debug(f"loaded '{self.name}' from {short_path(file_path)}")
            return obj
        return {}
    
    def dump(self, obj: dict[str, Any] | None=None):
        if obj is None:
            obj = {}
        obj.update({
            'ml_model': self.ml_model,
            'datas': self.datas,
            # TODO: dump dates as well
        })
        file_path = self._get_file_path()
        joblib.dump(obj, file_path, compress=True)
        self.logger.debug(f"dumped '{self.name}' to {short_path(file_path)}")
        
    
    def add_listener(self, listener: BaseModel, listener_key: BaseData):
        if listener not in self._listeners[listener_key]:
            self._listeners[listener_key].append(listener)
    
    def remove_listener(self, listener: BaseModel, listener_key: BaseData):
        if listener in self._listeners[listener_key]:
            self._listeners[listener_key].remove(listener)
    
    def is_model(self) -> bool:
        return not self.is_feature() and not self.is_indicator()
    
    def is_indicator(self) -> bool:
        from pfund.indicators.indicator_base import BaseIndicator
        return isinstance(self, BaseIndicator)
    
    def is_feature(self) -> bool:
        return isinstance(self, BaseFeature)
    
    def get_datas(self) -> list[BaseData]:
        datas = []
        for product in self.datas:
            datas.extend(list(self.datas[product].values()))
        return datas
    
    def set_data(self, product: BaseProduct, resolution: str | Resolution, data: BaseData):
        if isinstance(resolution, Resolution):
            resolution = repr(resolution)
        self.datas[product][resolution] = data

    def get_data(self, product: BaseProduct, resolution: str | None=None):
        return self.datas[product] if not resolution else self.datas[product][resolution]
    
    def add_data(self, trading_venue, base_currency, quote_currency, ptype, *args, **kwargs) -> list[BaseData]:
        datas = []
        for consumer in self._consumers:
            consumer_datas = self._add_consumer_datas(consumer, trading_venue, base_currency, quote_currency, ptype, *args, **kwargs)
            datas += consumer_datas
        return datas
    
    def _add_consumer_datas(self, consumer: BaseStrategy | BaseModel, *args, use_consumer_data=False, **kwargs) -> list[BaseData]:
        if not use_consumer_data:
            consumer_datas = consumer.add_data(*args, **kwargs)
        else:
            consumer_datas = consumer.get_datas()
        for data in consumer_datas:
            self.set_data(data.product, data.resolution, data)
            consumer.add_listener(listener=self, listener_key=data)
        return consumer_datas
    
    def _add_consumers_datas_if_no_data(self):
        if self.datas:
            return
        self.logger.info(f"No data for {self.name}, adding datas from consumers {[consumer.name for consumer in self._consumers]}")
        for consumer in self._consumers:
            self._add_consumer_datas(consumer, use_consumer_data=True)
    
    def get_model(self, name: str) -> BaseModel:
        return self.models[name]
    
    def add_model(
        self, 
        model: tModel, 
        name: str='', 
        min_data: int=1,
        max_data: None | int=None,
        group_data: bool=True,
        signal_cols: list[str] | None=None,
    ) -> tModel:
        '''Adds a model to the current model.
        Args:
            min_data (int): Minimum number of data points required for the model to make a prediction.
            max_data (int | None): Maximum number of data points required for the model to make a prediction.
            - If None: max_data is set to min_data.
            - If value=-1: include all data
            
            group_data (bool): Determines how `min_data` and `max_data` are applied to the whole df:
            - If True: `min_data` and `max_data` apply to each group=(product, resolution).
            e.g. if `min_data=2`, at least two data points are required for each group=(product, resolution).
            - If False: `min_data` and `max_data` apply to the entire dataset, not segregated by product or resolution.
            e.g. if `min_data=2`, at least two data points are required for the whole dataset.
            
            signal_cols: signal columns, if not provided, it will be derived in predict()
        '''
        Model = model.get_model_type_of_ml_model()
        assert isinstance(model, Model), \
            f"{model.type} '{model.__class__.__name__}' is not an instance of {Model.__name__}. Please create your {model.type} using 'class {model.__class__.__name__}({Model.__name__})'"
        if name:
            model.set_name(name)
        model.set_min_data(min_data)
        model.set_max_data(max_data)
        model.set_group_data(group_data)
        if signal_cols:
            model.set_signal_cols(signal_cols)
        model.create_logger()
        mdl = model.name
        if mdl in self.models:
            raise Exception(f"{model.name} already exists in {self.name}")
        model.add_consumer(self)
        self.models[mdl] = model
        self.logger.debug(f"added {model.name}")
        return model
    
    def add_feature(
        self, 
        feature: tFeature, 
        name: str='',
        min_data: int=1,
        max_data: None | int=None,
        group_data: bool=True,
        signal_cols: list[str] | None=None,
    ) -> tFeature:
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
        min_data: int=1,
        max_data: None | int=None,
        group_data: bool=True,
        signal_cols: list[str] | None=None,
    ) -> tIndicator:
        return self.add_model(
            indicator, 
            name=name, 
            min_data=min_data, 
            max_data=max_data, 
            group_data=group_data,
            signal_cols=signal_cols,
        )
    
    def update_quote(self, data: QuoteData, **kwargs):
        product, bids, asks, ts = data.product, data.bids, data.asks, data.ts
        for listener in self._listeners[data]:
            listener.update_quote(data, **kwargs)
            self.update_predictions(data, listener)
        self._append_to_df(data, **kwargs)
        self.on_quote(product, bids, asks, ts, **kwargs)
        
    def update_tick(self, data: TickData, **kwargs):
        product, px, qty, ts = data.product, data.px, data.qty, data.ts
        for listener in self._listeners[data]:
            listener.update_tick(data, **kwargs)
            self.update_predictions(data, listener)
        self._append_to_df(data, **kwargs)
        self.on_tick(product, px, qty, ts, **kwargs)
    
    def update_bar(self, data: BarData, **kwargs):
        product, bar, ts = data.product, data.bar, data.bar.end_ts
        for listener in self._listeners[data]:
            listener.update_bar(data, **kwargs)
            self.update_predictions(data, listener)
        self._append_to_df(data, **kwargs)
        self.on_bar(product, bar, ts, **kwargs)
    
    def update_predictions(self, data: BaseData, listener: BaseModel):
        pred_y: torch.Tensor | np.ndarray = listener._next(data)
        signal_cols = listener.get_signal_cols()
        for i, col in enumerate(signal_cols):
            self.predictions[col] = pred_y[i]
    
    def _start_models(self):
        for model in self.models.values():
            model.start()

    def _prepare_df(self):
        return self._data_tool.prepare_df(ts_col_type='timestamp')
        
    def _append_to_df(self, data: BaseData, **kwargs):
        return self._data_tool.append_to_df(data, self.predictions, **kwargs)
    
    def start(self):
        if not self.is_running():
            self.add_datas()
            self._add_consumers_datas_if_no_data()
            self._convert_min_max_data_to_dict()
            self.add_models()
            self._start_models()
            self._prepare_df()
            self.load()
            self.on_start()
            self._is_running = True
        else:
            self.logger.warning(f'model {self.name} has already started')
        
    def stop(self):
        if self.is_running():
            self._is_running = False
            self.on_stop()
            for model in self.models.values():
                model.stop()
        else:
            self.logger.warning(f'strategy {self.name} has already stopped')
        
    '''
    ************************************************
    Model Functions
    Users can customize these functions in their models.
    ************************************************
    '''
    def add_datas(self):
        pass
    
    def add_models(self):
        pass
    
    def on_start(self):
        pass
    
    def on_stop(self):
        pass
    
    def on_quote(self, product, bids, asks, ts, **kwargs):
        pass
    
    def on_tick(self, product, px, qty, ts, **kwargs):
        pass

    def on_bar(self, product, bar, ts, **kwargs):
        pass
    
    
class BaseFeature(BaseModel):
    '''Feature is a model with ml_model=None'''
    def __init__(self, *args, **kwargs):
        ml_model = None
        super().__init__(ml_model, *args, **kwargs)
        self.type = 'feature'
        self.set_signal_cols([self.name])
    
    def predict(self, X: pd.DataFrame | pl.LazyFrame, *args, **kwargs) -> np.ndarray:
        raise NotImplementedError        
    extract = predict
    