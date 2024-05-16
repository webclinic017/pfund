from collections import defaultdict

from pfund.universes.base_universe import BaseUniverse
from pfund.const.common import SUPPORTED_PRODUCT_TYPES


class TradfiUniverse(BaseUniverse):
    def __init__(self):
        super().__init__()
        self.stocks = defaultdict(dict)  # {exch: {pdt: product}}
        self.futures = defaultdict(dict)
        self.options = defaultdict(dict)
        self.cashes = defaultdict(dict)
        self.cryptos = self.spots = defaultdict(dict)
        self.bonds = defaultdict(dict)
        self.funds = defaultdict(dict)
        self.cmdties = defaultdict(dict)
        self._all_assets = {
            # ptype: assets
            'STK': self.stocks,
            'FUT': self.futures,
            'OPT': self.options,
            'CASH': self.cashes,
            'CRYPTO': self.cryptos,
            'BOND': self.bonds,
            'FUND': self.funds,
            'CMDTY': self.cmdties,
        }
    
    def _get_assets(self, ptype: str):
        try:
            return super()._get_assets(ptype)
        except KeyError:
            raise KeyError(f'Invalid {ptype=}, {SUPPORTED_PRODUCT_TYPES=}')