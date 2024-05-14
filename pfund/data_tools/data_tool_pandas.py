from __future__ import annotations
from collections import defaultdict

from typing import TYPE_CHECKING, Generator, Literal
if TYPE_CHECKING:
    from pfund.datas.data_base import BaseData
    from pfund.models.model_base import BaseModel

import numpy as np
import pandas as pd

from pfund.strategies.strategy_base import BaseStrategy
from pfund.data_tools.data_tool_base import BaseDataTool
from pfund.utils.envs import backtest


# NOTE: convention: all function names that endswith "_df" will directly modify self.df, e.g. "xxx_df"
class PandasDataTool(BaseDataTool):
    def get_df(
        self, 
        start_idx: int=0, 
        end_idx: int | None=None, 
        product: str | None=None, 
        resolution: str | None=None, 
        copy: bool=True
    ) -> pd.DataFrame | None:
        if self.df is None:
            return
        if self._new_rows:
            self._push_new_rows_to_df()
        df = self.df.copy(deep=True) if copy else self.df
        if product and resolution:
            df = df.loc[(df['product'] == product) & (df['resolution'] == resolution)]
        elif product and not resolution:
            df = df.loc[df['product'] == product]
        elif not product and resolution:
            df = df.loc[df['resolution'] == resolution]
        return df.iloc[start_idx:end_idx]
    
    def prepare_df(self, ts_col_type: Literal['datetime', 'timestamp']='datetime'):
        assert self._raw_dfs, "No data is found, make sure add_data(...) is called correctly"
        self.df = pd.concat(self._raw_dfs.values())
        self.df.sort_values(by=self.INDEX, ascending=True, inplace=True)
        self.df.reset_index(drop=True, inplace=True)
        # arrange columns
        self.df = self.df[self.INDEX + [col for col in self.df.columns if col not in self.INDEX]]
        if ts_col_type == 'datetime':
            # ts column already is datetime by default
            pass
        elif ts_col_type == 'timestamp':
            # converts 'ts' from datetime to unix timestamp
            # in milliseconds int -> in seconds with milliseconds precision
            self.df['ts'] = self.df['ts'].astype(int) // 10**6 / 10**3
        self._raw_dfs.clear()
    
    def merge_signal_dfs_with_df(self, signal_dfs: list[pd.DataFrame]):
        for signal_df in signal_dfs:
            self.df = pd.merge(self.df, signal_df, on=self.INDEX, how='left')
        self.df.sort_values(by='ts', ascending=True, inplace=True)
        self.df.reset_index(drop=True, inplace=True)

    def clear_df(self):
        self.df = pd.DataFrame(columns=self.df.columns).astype(self.df.dtypes)
        
    def _push_new_rows_to_df(self):
        new_rows_df = pd.DataFrame(self._new_rows)
        self.df = pd.concat([self.df, new_rows_df], ignore_index=True)
        self.df.sort_values(by=self.INDEX, ascending=True, inplace=True)
        self.df.reset_index(drop=True, inplace=True)
        self._new_rows.clear()
        if self._MAX_ROWS and self.df.shape[0] >= self._MAX_ROWS:
            self._trim_df()
        
    def append_to_df(self, data: BaseData, predictions: dict, **kwargs):
        '''Appends new data to the df
        Args:
            kwargs: other_info about the data
        '''
        row_data = {
            'ts': data.ts, 
            'product': repr(data.product), 
            'resolution': data.resol
        }
        
        for col in self.df.columns:
            if col in self.INDEX:
                continue
            # e.g. open, high, low, close, volume
            if hasattr(data, col):
                row_data[col] = getattr(data, col)
            elif col in kwargs:
                row_data[col] = kwargs[col]
                
        for mdl, pred_y in predictions.items():
            row_data[mdl] = pred_y
        
        self._new_rows.append(row_data)
        if len(self._new_rows) >= self._MAX_NEW_ROWS:
            self._push_new_rows_to_df()
    
    def _trim_df(self):
        num_rows_to_trim = self.df.shape[0] - self._MIN_ROWS
        trimmed_df = self.df.iloc[:num_rows_to_trim]
        # TODO:
        self._write_df_to_db(trimmed_df)
        self.df = self.df.iloc[-self._MIN_ROWS:]
    
    # TODO:
    def _write_df_to_db(self, trimmed_df: pd.DataFrame):
        pass
    
    @staticmethod
    def get_nan_columns(df: pd.DataFrame) -> list[str]:
        all_nan_columns: pd.Series = df.isna().all()
        nan_columns = all_nan_columns[all_nan_columns].index.tolist()
        return nan_columns
    
    @staticmethod
    def assert_frame_equal(df1: pd.DataFrame, df2: pd.DataFrame):
        '''
        Raise:
            AssertionError: if df1 and df2 are not equal
        '''
        pd.testing.assert_frame_equal(df1, df2, check_exact=False, rtol=1e-5)
    
    @backtest
    def signalize(self, X: pd.DataFrame, pred_y: np.ndarray, columns: list[str]) -> pd.DataFrame:
        pred_df = pd.DataFrame(pred_y, columns=columns)
        assert set(self.INDEX) <= set(X.columns), f"{self.INDEX} must be in X's columns"
        X = X[self.INDEX]  # only keep the index columns
        signal_df = pd.concat([X, pred_df], axis=1)
        # arrange columns
        signal_df = signal_df[self.INDEX + [col for col in signal_df.columns if col not in self.INDEX]]
        return signal_df

    @staticmethod
    @backtest
    def iterate_df_by_chunks(df: pd.DataFrame, num_chunks=1) -> Generator[pd.DataFrame, None, None]:
        total_rows = df.shape[0]
        chunk_size = total_rows // num_chunks
        for i in range(0, total_rows, chunk_size):
            df_chunk = df.iloc[i:i + chunk_size].copy(deep=True)
            yield df_chunk
    
    @backtest
    def preprocess_event_driven_df(self, df: pd.DataFrame) -> pd.DataFrame:
        def _check_resolution(res):
            from pfund.datas.resolution import Resolution
            resolution = Resolution(res)
            return resolution.is_quote(), resolution.is_tick()
        
        # add 'broker', 'is_quote', 'is_tick' columns
        df['broker'] = df['product'].str.split('-').str[0]
        df['is_quote'], df['is_tick'] = zip(*df['resolution'].apply(_check_resolution))
        
        # arrange columns
        left_cols = self.INDEX + ['broker', 'is_quote', 'is_tick']
        df = df[left_cols + [col for col in df.columns if col not in left_cols]]
        return df
   
    # TODO: 
    @backtest
    def postprocess_event_driven_df(self, df: pd.DataFrame) -> pd.DataFrame:
        # convert ts column back to datetime type
        pass
   
    # TODO
    @backtest
    def preprocess_vectorized_df(self, df: pd.DataFrame, backtestee: BaseStrategy | BaseModel) -> pd.DataFrame:
        return
    
    # TODO
    @staticmethod
    @backtest
    def postprocess_vectorized_df(df_chunks: list[pd.DataFrame]) -> pd.DataFrame:
        '''Prepares the df after vectorized backtesting, including:
        if has 'orders' column:
        - converts orders to trades
        if no 'orders' column, then 'trade_size'/'position' must be in the df
        - derives 'trade_size'/'position' from 'position'/'trade_size'
        '''
        # TEMP
        print('postprocess_vectorized_df!!!')
        return
        # df = pd.concat(df_chunks)
        # print(df)
        cols = df.columns
        if 'position' not in cols and 'trade_size' not in cols:
            raise Exception("either 'position' or 'trade_size' must be in the dataframe columns")
        # use 'position' to derive 'trade_size' and vice versa
        elif 'position' in cols and 'trade_size' not in cols:
            df['trade_size'] = df['position'].diff(1)
            # fill the nan value in the first row with the initial position
            df.iloc[0, df.columns.get_loc('trade_size')] = df.iloc[0, df.columns.get_loc('position')]
        elif 'trade_size' in cols and 'position' not in cols:
            df['position'] = df['trade_size'].cumsum()
        
        if 'trade_price' not in cols:
            df.loc[df['trade_size'] != 0, 'trade_price'] = df['close']
        return df
    
    # TODO: for train engine
    def prepare_datasets(self, datas):
        # create datasets based on train/val/test periods
        datasets = defaultdict(list)  # {'train': [df_of_product_1, df_of_product_2]}
        for product in datas:
            for type_, periods in [('train', self.train_periods), ('val', self.val_periods), ('test', self.test_periods)]:
                period = periods[product]
                if period is None:
                    raise Exception(f'{type_}_period for {product} is not specified')
                df = self.filter_df(self.df, start_date=period[0], end_date=period[1], symbol=product.symbol).reset_index()
                datasets[type_].append(df)
                
        # combine datasets from different products to create the final train/val/test set
        for type_ in ['train', 'val', 'test']:
            df = pd.concat(datasets[type_])
            df.set_index(self.INDEX, inplace=True)
            df.sort_index(level='ts', inplace=True)
            if type_ == 'train':
                self.train_set = df
            elif type_ == 'val':
                self.val_set = self.validation_set = df
            elif type_ == 'test':
                self.test_set = df

       
    '''
    ************************************************
    Helper Functions
    ************************************************
    '''
    
    @staticmethod
    def get_index_values(df: pd.DataFrame, index: str) -> list:
        assert index in df.index.names, f"index must be one of {df.index.names}"
        return df.index.get_level_values(index).unique().to_list()
    
    @staticmethod
    def set_index_values(df: pd.DataFrame, index: str, values: list) -> pd.DataFrame:
        assert index in df.index.names, f"index must be one of {df.index.names}"
        df.index = df.index.set_levels(values, level=index)
        return df
    
    @staticmethod
    def output_df_to_parquet(df: pd.DataFrame, file_path: str, compression: str='zstd'):
        df.to_parquet(file_path, compression=compression)
    
    def filter_df_with_multi_index(self, df: pd.DataFrame, start_date: str | None=None, end_date: str | None=None, product: str='', resolution: str=''):
        assert self.INDEX == df.index.names, f"index must be {self.INDEX}"
        product = product or slice(None)
        resolution = resolution or slice(None)
        return df.loc[(slice(start_date, end_date), product, resolution), :]
    
    @staticmethod
    def ffill(df: pd.DataFrame, columns: list[str]):
        return (
            df.unstack(level=columns)
            .ffill()
            .stack(level=columns)
        )
    
    @staticmethod
    def rescale(
        df: pd.DataFrame,
        window_size: int | None=None,
        min_periods: int=20,
    ) -> pd.DataFrame:
        """Scales the data to z-score using a rolling window to avoid lookahead bias
        If window_size is None, then use expanding window
        """
        if window_size:
            mu = df.rolling(window=window_size, min_periods=min_periods).mean()
            sigma = df.rolling(window=window_size, min_periods=min_periods).std()
        else:
            mu = df.expanding(min_periods=min_periods).mean()
            sigma = df.expanding(min_periods=min_periods).std()
        df_norm = (df - mu) / sigma
        return df_norm
    