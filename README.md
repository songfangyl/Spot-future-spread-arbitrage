## Quant Trader Test Helper

This repo carries the reference solutions for the two questions in `Quant Trader Test.md`: a TWAP execution helper and a historical backtest of the BTC spot/coin-futures carry.

### Environment Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you have not already.
2. From the repo root create and sync the virtualenv:

```bash
uv sync
```

### Task 1 – Execution Algorithm

- `quant_trader/execution.py` implements a TWAP that spends a USD notional evenly across the chosen window.
- Each slice looks up the latest bid/ask, converts the remaining USD budget (or unwind quantity) into BTC/contracts, and submits the order. With the default `use_market_orders=True`, we fire a true market order so it fills right away. Flip `use_market_orders=False` to use a limit order priced just past the top of book (`ask * 1.001` when buying, `bid * 0.999` when selling) so it still fills immediately but with a small 0.1% cushion on price.
- Each slice waits for its exact timeslot, so even if one request runs a bit slow the next order still fires on schedule.
- At the end of every slice the script logs remaining exposure in both USD and BTC/contracts; any dust left below the exchange minimum is surfaced as a warning.

#### Run It

```bash
uv run python -m quant_trader.execution
```

The default `ExecutionConfig` simulates a 24-hour schedule in dry-run mode. To trade live, set `ExecutionConfig.dry_run = False` (and provide real Binance clients if you do not want to use the simulator). Adjust `duration_hours`, `slice_interval_minutes`, `notional_usdt`, or `use_market_orders` to match your execution view.

### Task 2 – Backtest

- `quant_trader/backtest.py` loads daily BTC spot candles plus coin-margined delivery futures klines directly from Binance.
- Futures rolls follow the exchange convention: quarterly expiries on the last Friday of March/June/September/December with positions rolled one day before delivery.
- The simulator keeps a constant notional on both legs; futures PnL is computed in BTC using the inverse-contract formula and then marked in USDT with that day’s spot close.
- CSV output lands in `output/` by default with a name that includes the pair and date window (e.g. `output/backtest_BTCUSD_2021-01-01_2021-09-30.csv`). Set `BacktestConfig.output_path` to override the filename or to point to a directory of your choice.

#### Run It

```bash
uv run python -m quant_trader.backtest
```

Tweak `BacktestConfig` to change dates, notional, roll buffer, symbols, or output destination. The module prints a summary dict and writes the detailed daily results CSV automatically.

### Repo Structure

- `quant_trader/execution.py` – TWAP execution helper and simulator entrypoint.
- `quant_trader/backtest.py` – Historical carry backtest and CLI entrypoint.
- `quant_trader/binance_simulator.py` – Thin wrapper that fakes order placement while leaving data requests live.
- `Quant Trader Test.md` – Original problem statement.
