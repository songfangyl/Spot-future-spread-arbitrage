"""Backtest for the long-spot / short-coin future carry strategy."""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from binance.cm_futures import CMFutures
from binance.spot import Spot

from .binance_simulator import BinanceSimulator, DEFAULT_API_KEY, DEFAULT_SECRET_KEY


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _date_to_millis(day: date) -> int:
    dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _klines_to_daily_close(klines: List[List]) -> Dict[date, float]:
    prices: Dict[date, float] = {}
    for kline in klines:
        day = datetime.fromtimestamp(kline[0] / 1000, tz=timezone.utc).date()
        prices[day] = float(kline[4])
    return prices


def _last_friday(year: int, month: int) -> date:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    candidate = next_month - timedelta(days=1)
    while candidate.weekday() != 4:
        candidate -= timedelta(days=1)
    return candidate


def _format_delivery_symbol(pair: str, delivery: date) -> str:
    return f"{pair}_{delivery.strftime('%y%m%d')}"


@dataclass
class BacktestConfig:
    start_date: date | str = "2021-01-01"
    end_date: date | str = "2021-09-30"
    notional_usdt: float = 1_000_000
    roll_buffer_days: int = 1
    spot_symbol: str = "BTCUSDT"
    future_pair: str = "BTCUSD"
    output_path: Optional[Path | str] = Path("output")

    def __post_init__(self) -> None:
        self.start_date = _parse_date(self.start_date)
        self.end_date = _parse_date(self.end_date)
        if self.start_date > self.end_date:
            raise ValueError("Start date must be earlier than end date.")
        if isinstance(self.output_path, str):
            self.output_path = Path(self.output_path)


class SpreadBacktester:
    """Download Binance data, roll contracts, and compute daily PnL."""

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        simulator: Optional[BinanceSimulator] = None,
        spot_client: Optional[Spot] = None,
        future_client: Optional[CMFutures] = None,
    ) -> None:
        self.config = config or BacktestConfig()
        if simulator:
            self.spot_client = simulator.spot
            self.future_client = simulator.cm_future
        else:
            api_key = DEFAULT_API_KEY
            api_secret = DEFAULT_SECRET_KEY
            self.spot_client = spot_client or Spot(api_key=api_key, api_secret=api_secret)
            self.future_client = future_client or CMFutures(key=api_key, secret=api_secret)

    # ------------------------------ public ---------------------------------
    def run(self) -> Tuple[List[Dict[str, float]], Dict[str, float]]:
        spot_prices = self._fetch_spot_prices()
        segments = self._build_contract_segments()
        future_prices = self._fetch_future_prices(segments)
        records = self._simulate(spot_prices, future_prices, segments)
        summary = self._summarize(records)
        target_path = self._resolve_output_path()
        self._write_csv(records, target_path)
        return records, summary

    # --------------------------- data fetch --------------------------------
    def _fetch_spot_prices(self) -> Dict[date, float]:
        params = {
            "symbol": self.config.spot_symbol,
            "interval": "1d",
            "startTime": _date_to_millis(self.config.start_date),
            "endTime": _date_to_millis(self.config.end_date + timedelta(days=1)),
        }
        klines = self.spot_client.klines(**params)
        return _klines_to_daily_close(klines)

    def _fetch_future_prices(self, segments: Iterable[Dict[str, Any]]) -> Dict[str, Dict[date, float]]:
        prices: Dict[str, Dict[date, float]] = {}
        for segment in segments:
            symbol = segment["symbol"]
            start_ms = _date_to_millis(segment["start"] - timedelta(days=1))
            end_ms = _date_to_millis(segment["end"] + timedelta(days=1))
            klines = self.future_client.klines(
                symbol=symbol,
                interval="1d",
                startTime=start_ms,
                endTime=end_ms,
            )
            prices[symbol] = _klines_to_daily_close(klines)
        return prices

    def _build_contract_segments(self) -> List[Dict[str, Any]]:
        contract_size = self._detect_contract_size()
        segments: List[Dict[str, Any]] = []
        cursor = self.config.start_date
        end_date = self.config.end_date

        earliest = self.config.start_date - timedelta(days=365)
        latest = self.config.end_date + timedelta(days=365)
        deliveries: List[date] = []
        for year in range(earliest.year, latest.year + 1):
            for month in (3, 6, 9, 12):
                delivery = _last_friday(year, month)
                if delivery < earliest or delivery > latest:
                    continue
                deliveries.append(delivery)
        deliveries = sorted(set(deliveries))

        for delivery in deliveries:
            roll_date = delivery - timedelta(days=self.config.roll_buffer_days)
            start = cursor
            end = min(roll_date, end_date)
            if start > end:
                continue
            symbol = _format_delivery_symbol(self.config.future_pair, delivery)
            segments.append({"symbol": symbol, "start": start, "end": end, "contractSize": contract_size})
            cursor = end + timedelta(days=1)
            if cursor > end_date:
                break

        if cursor <= end_date:
            raise RuntimeError("Not enough delivery contracts to cover the requested window.")
        return segments

    def _detect_contract_size(self) -> float:
        try:
            info = self.future_client.exchange_info()
        except Exception:
            info = None
        if isinstance(info, dict):
            for symbol in info.get("symbols", []):
                if symbol.get("pair") != self.config.future_pair:
                    continue
                if symbol.get("contractType") == "PERPETUAL":
                    continue
                size = symbol.get("contractSize")
                if size:
                    try:
                        return float(size)
                    except (TypeError, ValueError):
                        continue
        return 100.0

    def _default_output_name(self) -> str:
        pair = self.config.future_pair.replace("/", "").upper()
        start = self.config.start_date.isoformat()
        end = self.config.end_date.isoformat()
        return f"backtest_{pair}_{start}_{end}.csv"

    def _resolve_output_path(self) -> Path:
        base = self.config.output_path or Path("output")
        if not isinstance(base, Path):
            base = Path(base)
        if base.suffix:
            return base
        return base / self._default_output_name()

    # --------------------------- simulation --------------------------------
    def _simulate(
        self,
        spot_prices: Dict[date, float],
        future_prices: Dict[str, Dict[date, float]],
        segments: List[Dict[str, Any]],
    ) -> List[Dict[str, float]]:
        day_to_symbol: Dict[date, str] = {}
        segment_meta: Dict[str, Dict[str, Any]] = {}
        for segment in segments:
            day = segment["start"]
            while day <= segment["end"]:
                day_to_symbol[day] = segment["symbol"]
                day += timedelta(days=1)
            segment_meta[segment["symbol"]] = segment

        prev_spot_price: Optional[float] = None
        prev_future_price: Optional[float] = None
        prev_symbol: Optional[str] = None
        spot_qty: Optional[float] = None
        contracts: Optional[int] = None
        cum_pnl = 0.0
        records: List[Dict[str, float]] = []
        for day in _date_range(self.config.start_date, self.config.end_date):
            spot_price = spot_prices.get(day)
            symbol = day_to_symbol.get(day)
            if spot_price is None or symbol is None:
                raise RuntimeError(f"Missing price data for {day}.")
            future_price = future_prices[symbol].get(day)
            if future_price is None:
                raise RuntimeError(f"Missing future price for {symbol} on {day}.")

            roll = symbol != prev_symbol or prev_spot_price is None
            if roll:
                spot_qty = self.config.notional_usdt / spot_price
                contract_size = segment_meta[symbol]["contractSize"]
                contracts = max(1, round(self.config.notional_usdt / contract_size))
                spot_pnl = 0.0
                future_pnl = 0.0
            else:
                spot_pnl = (spot_qty or 0.0) * (spot_price - (prev_spot_price or spot_price))
                contract_size = segment_meta[symbol]["contractSize"]
                future_pnl_coin = -1.0 * (contracts or 0) * contract_size * (
                    1.0 / (prev_future_price or future_price) - 1.0 / future_price
                )
                future_pnl = future_pnl_coin * spot_price

            total = spot_pnl + future_pnl
            cum_pnl += total
            records.append(
                {
                    "date": day.isoformat(),
                    "spot_price": spot_price,
                    "future_symbol": symbol,
                    "future_price": future_price,
                    "spot_position": spot_qty or 0.0,
                    "future_contracts": float(contracts or 0),
                    "spot_pnl": spot_pnl,
                    "future_pnl": future_pnl,
                    "total_pnl": total,
                    "cum_pnl": cum_pnl,
                    "roll": roll,
                }
            )
            prev_spot_price = spot_price
            prev_future_price = future_price
            prev_symbol = symbol
        return records

    # --------------------------- reporting ---------------------------------
    def _summarize(self, records: List[Dict[str, float]]) -> Dict[str, float]:
        if not records:
            return {}
        total_days = len(records)
        final_pnl = records[-1]["cum_pnl"]
        total_return = final_pnl / self.config.notional_usdt
        annualized_return = (1 + total_return) ** (365 / total_days) - 1 if total_days else 0.0
        daily_returns = [rec["total_pnl"] / self.config.notional_usdt for rec in records[1:]]
        daily_vol = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
        annualized_vol = daily_vol * math.sqrt(365)
        sharpe = annualized_return / annualized_vol if annualized_vol else float("nan")
        return {
            "days": total_days,
            "final_pnl": final_pnl,
            "total_return": total_return,
            "annualized_return": annualized_return,
            "annualized_vol": annualized_vol,
            "sharpe": sharpe,
        }

    def _write_csv(self, records: List[Dict[str, float]], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)


def run_example() -> None:
    """Execute the backtest for Jan-Sep 2021 and print the summary."""

    simulator = BinanceSimulator()
    backtester = SpreadBacktester(config=BacktestConfig(), simulator=simulator)
    _, summary = backtester.run()
    print("Backtest summary:", summary)


if __name__ == "__main__":
    run_example()
