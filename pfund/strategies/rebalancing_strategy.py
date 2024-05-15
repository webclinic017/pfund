from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from pfund.portfolio import Portfolio
    from pfund.universes.base_universe import BaseUniverse

from abc import ABC, abstractmethod

from pfund.strategies.strategy_base import BaseStrategy


# TODO
class RebalancingStrategy(BaseStrategy, ABC):
    @abstractmethod
    def rebalance(self, universes: dict[str, BaseUniverse], portfolio: Portfolio, *args, **kwargs):
        pass