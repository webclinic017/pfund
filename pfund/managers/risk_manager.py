from pfund.managers.base_manager import BaseManager


# TODO
class RiskManager(BaseManager):
    def __init__(self, broker):
        super().__init__('risk_manager', broker)
        