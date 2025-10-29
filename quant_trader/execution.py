"""Execution helper for the long-spot/short-future spread strategy."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from binance.cm_futures import CMFutures
from binance.spot import Spot

from .binance_simulator import BinanceSimulator

logger = logging.getLogger(__name__)


def _round_to_step(value: float, step: float) -> float:
    if step == 0:
        return value
    precision = max(0, int(round(-math.log10(step))))
    rounded = math.floor(value / step) * step
    return round(rounded, precision)


@dataclass
class ExecutionConfig:
    """Configuration for the TWAP style execution."""

    notional_usdt: float = 1_000_000
    duration_hours: int = 24
    slice_interval_minutes: int = 5
    price_offset_bps: float = 5.0
    min_spot_qty: float = 0.0001
    dry_run: bool = True
    use_market_orders: bool = True

    @property
    def num_slices(self) -> int:
        slices = max(1, int(self.duration_hours * 60 // self.slice_interval_minutes))
        return slices

    @property
    def slice_interval_seconds(self) -> int:
        return self.slice_interval_minutes * 60


class SimpleOrderExecutor:
    """Send live orders via Binance clients (used when simulator is absent)."""

    def __init__(self, spot_client: Spot, future_client: CMFutures):
        self.spot_client = spot_client
        self.future_client = future_client

    def place_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        payload = order.copy()
        account = payload.pop("account")
        if account == "SPOT":
            return self.spot_client.new_order(**payload)
        if account == "FUTURE":
            return self.future_client.new_order(**payload)
        raise ValueError(f"Unknown account {account}")


class SpreadExecutor:
    """Coordinate long spot + short coin-margin futures execution."""

    def __init__(
        self,
        spot_symbol: str,
        future_pair: str = "BTCUSD",
        future_symbol: Optional[str] = None,
        config: Optional[ExecutionConfig] = None,
        simulator: Optional[BinanceSimulator] = None,
        spot_client: Optional[Spot] = None,
        future_client: Optional[CMFutures] = None,
    ) -> None:
        self.config = config or ExecutionConfig()
        self.spot_symbol = spot_symbol
        self.future_pair = future_pair
        if simulator:
            self.spot_client = simulator.spot
            self.future_client = simulator.cm_future
            self.order_executor = simulator
        else:
            if not spot_client or not future_client:
                raise ValueError("Live execution requires both spot_client and future_client.")
            self.spot_client = spot_client
            self.future_client = future_client
            self.order_executor = SimpleOrderExecutor(spot_client, future_client)

        self.future_symbol = future_symbol or self._select_front_contract()
        self._spot_filter = self._load_spot_filters()
        self._future_filter = self._load_future_filters()
        self.contract_size = float(self._future_filter["contractSize"])

    # -------------------------- public API ---------------------------------
    def open_position(self) -> None:
        """Open the spread by buying spot and selling futures via TWAP."""

        _, spot_ask = self._spot_best_prices()
        approx_spot_qty = self.config.notional_usdt / spot_ask if spot_ask else 0.0
        approx_contracts = self.config.notional_usdt / self.contract_size
        logger.info(
            "Opening spread for %.2f USDT (~%.6f %s, %.2f %s) via %.0f slices (contractSize=%s).",
            self.config.notional_usdt,
            approx_spot_qty,
            self.spot_symbol,
            approx_contracts,
            self.future_symbol,
            self.config.num_slices,
            self.contract_size,
        )
        self._run_twap_open("BUY", "SELL")

    def close_position(self, spot_qty: float, futures_contracts: int) -> None:
        """Close an existing spread by unwinding both legs."""

        logger.info(
            "Closing spread: %.4f %s, %s contracts %s via %.0f slices.",
            spot_qty,
            self.spot_symbol,
            futures_contracts,
            self.future_symbol,
            self.config.num_slices,
        )
        self._run_twap_close("SELL", "BUY", spot_qty, futures_contracts)

    # -------------------------- helpers ------------------------------------
    def _run_twap_open(self, spot_side: str, future_side: str) -> None:
        spot_notional_remaining = float(self.config.notional_usdt)
        future_notional_remaining = float(self.config.notional_usdt)
        slices = self.config.num_slices

        start_time = time.time()
        next_run = start_time

        for idx in range(slices):
            slices_left = max(1, slices - idx)
            spot_bid, spot_ask = self._spot_best_prices()
            spot_price = spot_ask if spot_side == "BUY" else spot_bid
            if spot_price <= 0:
                logger.warning("Spot price unavailable; skipping slice %d.", idx + 1)
                continue

            planned_spot_notional = spot_notional_remaining / slices_left
            planned_spot_qty = planned_spot_notional / spot_price

            future_bid, future_ask = self._future_best_prices()
            planned_future_notional = future_notional_remaining / slices_left
            planned_future_contracts = planned_future_notional / self.contract_size

            spot_size = self._place_spot_slice(spot_side, planned_spot_qty, spot_bid, spot_ask)
            future_size = self._place_future_slice(future_side, planned_future_contracts, future_bid, future_ask)

            executed_spot_notional = spot_size * spot_price
            executed_future_notional = future_size * self.contract_size

            spot_notional_remaining = max(0.0, spot_notional_remaining - executed_spot_notional)
            future_notional_remaining = max(0.0, future_notional_remaining - executed_future_notional)

            logger.info(
                "Slice %03d/%03d complete. Spot remaining: %.2f USDT, Futures remaining: %.2f USDT.",
                idx + 1,
                slices,
                spot_notional_remaining,
                future_notional_remaining,
            )
            if not self.config.dry_run and idx + 1 < slices:
                next_run += self.config.slice_interval_seconds
                sleep_for = max(0.0, next_run - time.time())
                time.sleep(sleep_for)

        if spot_notional_remaining > 1.0 or future_notional_remaining > 1.0:
            logger.warning(
                "Execution finished with residual notional. Spot left: %.2f USDT, Futures left: %.2f USDT.",
                spot_notional_remaining,
                future_notional_remaining,
            )

    def _run_twap_close(
        self,
        spot_side: str,
        future_side: str,
        spot_target_qty: float,
        future_target_contracts: float,
    ) -> None:
        spot_qty_remaining = max(0.0, spot_target_qty)
        future_remaining = max(0.0, future_target_contracts)
        slices = self.config.num_slices

        start_time = time.time()
        next_run = start_time

        for idx in range(slices):
            slices_left = max(1, slices - idx)
            spot_bid, spot_ask = self._spot_best_prices()
            future_bid, future_ask = self._future_best_prices()
            planned_spot_qty = spot_qty_remaining / slices_left
            planned_future_contracts = future_remaining / slices_left

            spot_size = self._place_spot_slice(spot_side, planned_spot_qty, spot_bid, spot_ask)
            future_size = self._place_future_slice(future_side, planned_future_contracts, future_bid, future_ask)

            spot_qty_remaining = max(0.0, spot_qty_remaining - spot_size)
            future_remaining = max(0.0, future_remaining - future_size)

            logger.info(
                "Slice %03d/%03d complete. Spot remaining: %.6f BTC, Futures remaining: %.2f contracts.",
                idx + 1,
                slices,
                spot_qty_remaining,
                future_remaining,
            )
            if not self.config.dry_run and idx + 1 < slices:
                next_run += self.config.slice_interval_seconds
                sleep_for = max(0.0, next_run - time.time())
                time.sleep(sleep_for)

        if spot_qty_remaining > 1e-6 or future_remaining > 1e-6:
            logger.warning(
                "Execution finished with residual quantity. Spot left: %.6f BTC, Futures left: %.2f contracts.",
                spot_qty_remaining,
                future_remaining,
            )

    def _spot_best_prices(self) -> Tuple[float, float]:
        book = self.spot_client.book_ticker(self.spot_symbol)
        bid = float(book["bidPrice"])
        ask = float(book["askPrice"])
        return bid, ask

    def _future_best_prices(self) -> Tuple[float, float]:
        book = self._future_book_ticker()
        bid = float(book["bidPrice"])
        ask = float(book["askPrice"])
        return bid, ask

    def _place_spot_slice(self, side: str, target_qty: float, best_bid: float, best_ask: float) -> float:
        if target_qty <= 0:
            return 0.0
        min_qty = max(self.config.min_spot_qty, self._spot_filter["minQty"])
        clip = _round_to_step(target_qty, self._spot_filter["stepSize"])
        if clip < min_qty:
            return 0.0

        if self.config.use_market_orders:
            order = {
                "symbol": self.spot_symbol,
                "account": "SPOT",
                "side": side,
                "type": "MARKET",
                "quantity": clip,
            }
        else:
            price = self._limit_price(side, best_bid, best_ask, self._spot_filter["tickSize"])
            order = {
                "symbol": self.spot_symbol,
                "account": "SPOT",
                "side": side,
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": clip,
                "price": price,
            }
        self._submit_order(order)
        return clip

    def _place_future_slice(self, side: str, target_contracts: float, best_bid: float, best_ask: float) -> float:
        if target_contracts <= 0:
            return 0.0
        step = self._future_filter["stepSize"]
        min_contract = max(step, 1.0 if step >= 1.0 else step)
        clip = _round_to_step(target_contracts, step)
        if clip < min_contract:
            return 0.0

        quantity = int(clip) if step >= 1.0 else clip

        if self.config.use_market_orders:
            order = {
                "symbol": self.future_symbol,
                "account": "FUTURE",
                "side": side,
                "type": "MARKET",
                "quantity": quantity,
            }
        else:
            price = self._limit_price(side, best_bid, best_ask, self._future_filter["tickSize"])
            order = {
                "symbol": self.future_symbol,
                "account": "FUTURE",
                "side": side,
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": quantity,
                "price": price,
            }
        self._submit_order(order)
        return clip

    def _submit_order(self, order: Dict[str, Any]) -> None:
        if self.config.dry_run:
            logger.info("Dry-run order: %s", order)
            return
        response = self.order_executor.place_order(order)
        logger.info("Order response: %s", response)

    # ----------------------- instrument metadata ---------------------------
    def _load_spot_filters(self) -> Dict[str, float]:
        info = self.spot_client.exchange_info()
        for symbol in info["symbols"]:
            if symbol["symbol"] == self.spot_symbol:
                filters = {f["filterType"]: f for f in symbol["filters"]}
                lot = float(filters["LOT_SIZE"]["stepSize"])
                tick = float(filters["PRICE_FILTER"]["tickSize"])
                min_qty = float(filters["LOT_SIZE"]["minQty"])
                return {"stepSize": lot, "tickSize": tick, "minQty": min_qty}
        raise ValueError(f"Spot symbol {self.spot_symbol} missing in exchange info.")

    def _load_future_filters(self) -> Dict[str, float]:
        info = self.future_client.exchange_info()
        for symbol in info["symbols"]:
            if symbol["symbol"] == self.future_symbol:
                filters = {f["filterType"]: f for f in symbol["filters"]}
                lot = float(filters["LOT_SIZE"]["stepSize"])
                tick = float(filters["PRICE_FILTER"]["tickSize"])
                return {
                    "stepSize": lot,
                    "tickSize": tick,
                    "contractSize": float(symbol["contractSize"]),
                    "deliveryDate": int(symbol["deliveryDate"]),
                }
        raise ValueError(f"Future symbol {self.future_symbol} missing in exchange info.")

    def _select_front_contract(self) -> str:
        info = self.future_client.exchange_info()
        now = int(time.time() * 1000)
        candidates = []
        for symbol in info["symbols"]:
            if symbol["pair"] != self.future_pair or symbol["contractType"] == "PERPETUAL":
                continue
            status = symbol.get("status") or symbol.get("contractStatus")
            if status != "TRADING":
                continue
            if symbol["deliveryDate"] <= now:
                continue
            candidates.append(symbol)
        if not candidates:
            raise ValueError(f"No active delivery futures for pair {self.future_pair}")
        candidates.sort(key=lambda item: item["deliveryDate"])
        return candidates[0]["symbol"]

    # ----------------------- price helpers ---------------------------------
    def _limit_price(self, side: str, bid: float, ask: float, tick_size: float) -> float:
        offset = self.config.price_offset_bps / 10_000.0
        if side == "BUY":
            price = min(ask * (1 + offset), ask * 1.001)
        else:
            price = max(bid * (1 - offset), bid * 0.999)
        price = _round_to_step(price, tick_size)
        return price

    def _future_book_ticker(self) -> Dict[str, Any]:
        """Return a normalized best bid/ask snapshot for the current future symbol."""

        data = self.future_client.book_ticker(symbol=self.future_symbol)
        if isinstance(data, list):
            if not data:
                raise ValueError(f"No book ticker data for {self.future_symbol}")
            if len(data) == 1:
                data = data[0]
            else:
                target = next((item for item in data if item.get("symbol") == self.future_symbol), None)
                data = target or data[0]
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected book ticker payload for {self.future_symbol}: {data}")
        return data


def run_example() -> None:
    """Small helper to demonstrate how to open and close the spread."""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    simulator = BinanceSimulator()
    executor = SpreadExecutor("BTCUSDT", future_pair="BTCUSD", simulator=simulator)
    executor.config.dry_run = True
    executor.open_position()
    # In practice pass actual filled sizes; the example uses the same notional approximation.
    _, spot_ask = executor._spot_best_prices()
    approx_spot_qty = executor.config.notional_usdt / spot_ask if spot_ask else 0.0
    approx_contracts = executor.config.notional_usdt / executor.contract_size
    executor.close_position(spot_qty=approx_spot_qty, futures_contracts=approx_contracts)


if __name__ == "__main__":
    run_example()
