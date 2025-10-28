"""Light-weight simulator that reuses Binance clients for read-only data.

The class mirrors the helper shown in the instructions so that the
execution script can operate without sending real orders.  API keys
bundle read-only permissions on Binance's test account.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Dict, Optional

from binance.cm_futures import CMFutures
from binance.spot import Spot

DEFAULT_API_KEY = "0Lj7lMcerkFtSnCyaIYs6CJmxbqwrdWoPjhJLqBLhyuDkCtvztgxbluNQxOCKn7X"
DEFAULT_SECRET_KEY = "jNd2ld4ONKDmeuse9TPLDBdB8ZCnlUMuMPpKknMMwfxZb8QcmpStkSRLHSvZDCk1"


@dataclass
class BinanceSimulator:
    """Wrap spot / coin-margin futures clients and fake order placement."""

    api_key: str = DEFAULT_API_KEY
    secret_key: str = DEFAULT_SECRET_KEY
    order_fill_prob: float = 0.9
    spot_client: Optional[Spot] = None
    future_client: Optional[CMFutures] = None

    def __post_init__(self) -> None:
        self.spot = self.spot_client or Spot(api_key=self.api_key, api_secret=self.secret_key)
        self.cm_future = self.future_client or CMFutures(key=self.api_key, secret=self.secret_key)

    def place_order(self, order_params: Dict[str, Any]) -> Dict[str, Any]:
        """Mimic order placement with a simple fill probability."""

        response = order_params.copy()
        fill = random.random() <= self.order_fill_prob
        response["status"] = "FILLED" if fill else "CANCELED"
        return response
