import numpy as np

try:
    import pandas as pd
    import polars as pl
except ImportError:
    pass

from pfund.models.model_base import BaseModel


class SklearnModel(BaseModel):
    def fit(
        self, 
        X: np.ndarray | pd.DataFrame | pl.LazyFrame, 
        y: np.ndarray
    ):
        if type(X) is pd.DataFrame:
            X = X.to_numpy()
        elif type(X) is pl.LazyFrame:
            X = X.collect().to_numpy()
        
        return self.ml_model.fit(X, y)
    
    def predict(
        self, 
        X: np.ndarray | pd.DataFrame | pl.LazyFrame,
        *args, 
        **kwargs
    ) -> np.ndarray:
        if type(X) is pd.DataFrame:
            X = X.to_numpy()
        elif type(X) is pl.LazyFrame:
            X = X.collect().to_numpy()
        else:
            raise ValueError(f"Unsupported data type: {type(X)}")
        pred_y = self.ml_model.predict(X, *args, **kwargs)

        if not self._signal_cols:
            num_cols = pred_y.shape[-1]
            signal_cols = self.get_default_signal_cols(num_cols)
            self.set_signal_cols(signal_cols)
        return pred_y

