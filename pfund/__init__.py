import sys
from importlib.metadata import version

from pfund.config_handler import configure
from pfund.const.aliases import ALIASES
from pfund.const.paths import PROJ_PATH
# add python path so that for files like "ibapi" (official python code from IB) can find their modules
sys.path.append(f'{PROJ_PATH}/externals')
from pfund.engines import BacktestEngine, TradeEngine, TrainEngine, SandboxEngine
from pfund.strategies import Strategy
from pfund.models import Model, Feature, PytorchModel, SklearnModel


__version__ = version('pfund')
__all__ = (
    '__version__',
    'configure', 
    'ALIASES',
    'BacktestEngine', 
    'TradeEngine',
    'TrainEngine', 
    'SandboxEngine',
    'Strategy', 
    'Model',
    'Feature',
    'PytorchModel',
    'SklearnModel',
)