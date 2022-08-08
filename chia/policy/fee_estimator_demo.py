from typing import Dict, Any, List

from chia.policy.fee_estimation import FeeBlockInfo, FeeMempoolInfo
from chia.types.mempool_item import MempoolItem

from chia.full_node.fee_estimate import FeeEstimate
from chia.util.ints import uint64

MIN_MOJO_PER_COST = 5


def demo_fee_rate_function(cost: int, time_in_seconds: int) -> uint64:
    return uint64(cost * MIN_MOJO_PER_COST * max((3600 - time_in_seconds), 1))


class FeeEstimatorDemo:  # FeeEstimatorInterface Protocol
    def __init__(self, config: Dict[str, Any] = {}) -> None:
        self.config = config

    def new_block(self, block_info: FeeBlockInfo) -> None:
        pass

    def add_mempool_item(self, mempool_info: FeeMempoolInfo, mempool_item: MempoolItem) -> None:
        pass

    def remove_mempool_item(self, mempool_info: FeeMempoolInfo, mempool_item: MempoolItem) -> None:
        pass

    def estimate_fee(self, *, cost: int, time: int) -> uint64:
        return demo_fee_rate_function(cost, time)

    def mempool_size(self) -> uint64:
        """Report last seen mempool size"""
        return uint64(0)

    def mempool_max_size(self) -> uint64:
        """Report current mempool max size (cost)"""
        return uint64(0)

    def request_fee_estimates(self, request_times: List[uint64]) -> List[FeeEstimate]:
        estimates = [self.estimate_fee(cost=1, time=t) for t in request_times]
        fee_estimates = [FeeEstimate(None, t, e) for (t, e) in zip(request_times, estimates)]
        return fee_estimates

