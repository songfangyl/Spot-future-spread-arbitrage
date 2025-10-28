### Quant Trader Test

Spot-future spread arbitrage is a common used strategy in tradfi, and similar opportunities exist in crypto as well. Assume we want to invest in this strategy, and trade with an account in Binance.

**Tasks**

Now we use a very simple buy-and-hold strategy, which builds a long spot + short coin-margin future portfolio, and will close all positions 1 day before future expires. Our target is to maximize annualized return of portfolio.
1. If we want to invest 1 million USDT in this strategy at this moment, to avoid high slippage we open/close positions gradually in 24 hours, write an execution algorithm to achieve this goal.
2. If we did such trade on 2021-01-01, backtest daily pnl we got until 2021-09-30. Note that you will need to roll over futures.

**Hints**

1. For task 1, you will need to interact with Binance API. We provide an API key which can be used to query market/account data, while for order placement you could just use that order simulation function.
2. For task 2, you might need to read exchange trading rules carefully to know about how to select instruments / how to calculate pnl, etc.
3. Codes you provided for both tasks should be runnable, we suggest that total lines should better not exceed 1000.

**Useful links**

Spot API documentation: [Introduction – Binance API Documentation (binance-docs.github.io)](https://binance-docs.github.io/apidocs/spot/en/#introduction)

Spot Python package: https://github.com/binance/binance-connector-python

Coin-margin Future API documentation: [General Info – Binance API Documentation (binance-docs.github.io)](https://binance-docs.github.io/apidocs/delivery/en/#general-info)

Coin-margin Future Python package: https://github.com/binance/binance-futures-connector-python

Guide for Binance trading rules: [Crypto Derivatives | Binance Support](https://www.binance.com/en/support/faq/crypto-derivatives?c=4&navId=4#18-64)https://www.binance.com/en/support/faq/crypto-derivatives?c=4&navId=4#18-64)

**Code Demo**

```shell
pip install binance-connector
pip install binance-futures-connector
```

```python
import random

from binance.spot import Spot
from binance.cm_futures import CMFutures

class BinanceSimulator(object):
     
    def __init__(self,
                 api_key='0Lj7lMcerkFtSnCyaIYs6CJmxbqwrdWoPjhJLqBLhyuDkCtvztgxbluNQxOCKn7X',
                 secret_key='jNd2ld4ONKDmeuse9TPLDBdB8ZCnlUMuMPpKknMMwfxZb8QcmpStkSRLHSvZDCk1'):
        
        self.spot = Spot(api_key=api_key,api_secret=secret_key)
        self.cm_future = CMFutures(key=api_key,secret=secret_key)
        self.order_fill_prob = 0.9
        
    def place_order(self,order_params):
        response = order_params.copy()
        x = random.randint(1,100)
        if x <= self.order_fill_prob*100:
            response['status'] = 'filled'
        else:
            response['status'] = 'canceled'
        return response
        

if __name__ == '__main__':
    
    client = BinanceSimulator()
    
    #check account status
    spot_info = client.spot.account()
    future_info = client.cm_future.account()
    
    #place order
    params = {
        'symbol': 'BTCUSDT',
        'account': 'SPOT',
        'side': 'SELL',
        'type': 'LIMIT',
        'timeInForce': 'GTC',
        'quantity': 0.002,
        'price': 59808
    }
    response = client.place_order(params)

```

