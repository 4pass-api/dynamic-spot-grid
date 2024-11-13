import configparser
import logging
import time
from typing import Literal

import ccxt
from ccxt import InsufficientFunds
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.StreamHandler(),
                        logging.FileHandler('app.log', encoding='utf-8')
                    ])
logger = logging.getLogger(__name__)


class GridOrder(BaseModel):
    id: str
    symbol: str
    side: Literal['buy', 'sell']
    amount: float
    price: float
    error_tries: int = 0

    @staticmethod
    def from_order_info(order_info: dict):
        return GridOrder(
            id=order_info['id'],
            symbol=order_info['symbol'],
            side=order_info['side'],
            price=order_info['price'],
            amount=order_info['amount']
        )

    def strategy_run(self, exchange: ccxt.binance, price_diff: float, p_max: float, p_min: float):

        try:
            order_info = exchange.fetch_order(self.id, self.symbol)
        except ccxt.OrderNotFound as e:
            logger.error(f"訂單 {self.id} 不存在: {e}")
            self.error_tries += 1
            if self.error_tries > 3:
                return None
            return self

        if order_info['status'] == 'closed':
            logger.info(f"訂單 {self.id} 完成: {self.side}@{self.price} x {self.amount}")
            next_side = 'buy' if self.side == 'sell' else 'sell'
            next_price = self.price + price_diff if next_side == 'sell' else self.price - price_diff
            if p_min < next_price < p_max:
                new_order_info = place_order(exchange, self.symbol, next_side, self.amount, next_price)
                return GridOrder.from_order_info(new_order_info)
            else:
                logging.info(f"價格 {next_price} 超出範圍，不下單")
                return None
        else:
            return self

    def cancel_order(self, exchange: ccxt.binance):
        logger.info(f"取消訂單: {self.id}, {self.side}@{self.price} x {self.amount}")
        exchange.cancel_order(self.id, self.symbol)


def exit_after_timeout(timeout):
    for i in range(timeout):
        logger.info(f"{timeout - i} 秒後退出...")
        time.sleep(1)
    logger.info("程式退出")
    exit(0)


def place_order(
        exchange: ccxt.binance,
        symbol: str,
        side: Literal['buy', 'sell'],
        amount: float,
        price: float | None = None,
        type: Literal['limit', 'market'] = 'limit',
        max_tries: int = 3
):
    for i in range(max_tries):
        try:
            order_info = exchange.create_order(symbol, type, side, amount, price)
            logger.info(f" {symbol}下單成功: {side}@{price if price else '市價'} x {amount}")
            return order_info
        except InsufficientFunds as e:
            logger.error(f"餘額不足: {e}")
        except ccxt.BaseError as e:
            logger.error(f"下單失敗: {e}")
        except Exception as e:
            logger.error(f"其他錯誤: {e}")
        time.sleep(1)
        logger.info("等待 1 秒後重試")

    exit_after_timeout(3)


def get_balance(exchange: ccxt.binance, _symbol):
    try:
        balance = exchange.fetch_balance()
    except ccxt.AuthenticationError as e:
        logger.error(f"API 錯誤: {e}")
        exit_after_timeout(5)
    except Exception as e:
        logger.error(f"其他錯誤: {e}")
        exit_after_timeout(5)

    fdusd_balance = balance['free']['FDUSD']
    logger.info(f"FDUSD 餘額: {fdusd_balance}")
    tgt_balance = balance['free'][_symbol]
    logger.info(f"{_symbol} 餘額: {tgt_balance}")
    tgt_fdusd_value = last_price * tgt_balance
    logger.info(f"{_symbol} 總價值: {tgt_fdusd_value} FDUSD")

    return fdusd_balance, tgt_balance, tgt_fdusd_value


if __name__ == '__main__':
    logger.info("程式開始")

    config = configparser.ConfigParser()
    config.read('settings.ini', encoding='utf-8')

    cancel_at_start = int(config['STRATEGY_PARAMS']['CANCEL_AT_START']) == 1

    apiKey = config['SYSTEM']['API_KEY']
    secretKey = config['SYSTEM']['SECRET_KEY']

    if len(apiKey) == 0 or len(secretKey) == 0:
        logger.error("API_KEY 或 SECRET_KEY 未設定")
        exit_after_timeout(5)

    ex = ccxt.binance({
        'apiKey': config['SYSTEM']['API_KEY'],
        'secret': config['SYSTEM']['SECRET_KEY'],
        'enableRateLimit': True,
    })
    ex.options['maxRetriesOnFailure'] = 3
    ex.options['timeout'] = 1000

    _symbol = config['STRATEGY_PARAMS']['TARGET']
    symbol = f"{_symbol}/FDUSD"

    if cancel_at_start:
        # cancel all orders
        open_orders = ex.fetch_open_orders(symbol)
        for order in open_orders:
            ex.cancel_order(order['id'], symbol)
            logger.info(f"取消訂單: {order['id']}, {order['side']}@{order['price']} x {order['amount']}")
        time.sleep(2)

    # check if the symbol is available
    if symbol not in ex.load_markets():
        logger.error(f"現貨交易對 {symbol} 不可使用")
        exit_after_timeout(5)
    else:
        logger.info(f"現貨交易對 {symbol} 可用")

    market = ex.market(symbol)
    last_price = ex.fetch_ticker(symbol)['last']
    min_amount = market['limits']['amount']['min']
    max_amount = market['limits']['amount']['max']
    min_cost = market['limits']['cost']['min']
    max_cost = market['limits']['cost']['max']

    # get minimum / maximum cost
    logger.info(
        f"現貨交易對 {symbol} 最小交易成本: {min_cost}, 最大交易成本: {max_cost}")
    # get minimum / maximum amount
    logger.info(
        f"現貨交易對 {symbol} 最小交易量: {min_amount}, 最大交易量: {max_amount}")

    fdusd_balance, tgt_balance, tgt_fdusd_value = get_balance(ex, _symbol)

    if abs(tgt_fdusd_value - fdusd_balance) / max(1, min(tgt_fdusd_value, fdusd_balance)) < 0.1:
        logger.info(f"{_symbol} 和 FDUSD 餘額相近，不需要調整")
    else:
        logger.info(f"{_symbol} 和 FDUSD 餘額差異過大，將自動調整")
        if abs(tgt_fdusd_value - fdusd_balance) / 2 > min_cost:
            amount = abs(tgt_fdusd_value - fdusd_balance) / 2 / last_price
            if amount > min_amount:
                place_order(ex, symbol, 'buy' if tgt_fdusd_value < fdusd_balance else 'sell', amount, None, 'market')

        time.sleep(2)
        fdusd_balance, tgt_balance, tgt_fdusd_value = get_balance(ex, _symbol)

    # sanity check
    p = 0.0
    amount = 0.0
    max_orders = 0
    patience = 1
    p_upper = -1e16
    p_lower = 1e16

    try:
        p = float(config['STRATEGY_PARAMS']['PRICE_DIFF'])
        if p < 1e-8:
            logger.error("價格差需要大於0")
            raise ValueError
    except ValueError:
        logger.error("價格差錯誤")
        exit_after_timeout(5)

    try:
        amount = float(config['STRATEGY_PARAMS']['AMOUNT'])
        if amount < min_amount:
            logger.error(f"{_symbol}最小交易量應大於{min_amount}")
            raise ValueError
        if last_price * amount < market['limits']['cost']['min']:
            logger.error(f"{_symbol}最小交易成本應大於{market['limits']['cost']['min']}")
            raise ValueError
    except ValueError:
        logger.error("交易量錯誤")
        exit_after_timeout(5)

    try:
        max_orders = int(config['STRATEGY_PARAMS']['MAX_ORDERS'])
        if tgt_balance < amount * max_orders:
            logger.error(f"{_symbol}餘額不足，無法以最大交易次數({max_orders})進行交易")
            raise ValueError
    except ValueError:
        logger.error("最大交易次數錯誤")
        exit_after_timeout(5)

    try:
        patience = int(config['STRATEGY_PARAMS']['PATIENCE'])
        if patience < max_orders:
            logger.error(f"耐心值應該大於最大交易次數: {max_orders}")
            raise ValueError
    except ValueError:
        logger.error("耐心值錯誤")
        exit_after_timeout(5)

    try:
        p_upper = float(config['STRATEGY_PARAMS']['PRICE_UPPER'])
        if p_upper < last_price:
            logger.error(f"價格上限應該大於最後成交價: {last_price}")
            raise ValueError
    except ValueError:
        logger.error("價格上限錯誤")
        exit_after_timeout(5)

    try:
        p_lower = float(config['STRATEGY_PARAMS']['PRICE_LOWER'])
        if p_lower > last_price:
            logger.error(f"價格下限應該小於最後成交價: {last_price}")
            raise ValueError
    except ValueError:
        logger.error("價格下限錯誤")
        exit_after_timeout(5)

    if p_upper < p_lower:
        logger.error("價格上限應該大於價格下限")
        exit_after_timeout(5)

    # summarize the strategy parameters
    logger.info(
        f"價格差: {p}, 單位交易量: {amount}, 單邊掛單數: {max_orders}, 價格區間上限: {p_upper}, 價格區間下限: {p_lower}")

    last_price = ex.fetch_ticker(symbol)['last']

    # Init orders
    orders = []
    for i in range(1, max_orders + 1):
        buy_price = last_price - p * i
        order_info = place_order(ex, symbol, 'buy', amount, buy_price)
        if order_info:
            orders.append(GridOrder.from_order_info(order_info))

    for i in range(1, max_orders + 1):
        sell_price = last_price + p * i
        order_info = place_order(ex, symbol, 'sell', amount, sell_price)
        if order_info:
            orders.append(GridOrder.from_order_info(order_info))

    last_check_time = time.time()
    while True:

        last_price = ex.fetch_ticker(symbol)['last']

        orders = sorted(orders, key=lambda x: x.price)
        num_of_buy = len([order for order in orders if order.side == 'buy'])
        num_of_sell = len(orders) - num_of_buy

        new_orders = []
        for i in range(2, num_of_buy):
            # check the price difference of adjacent buy orders, if it is greater than p, place a new order
            assert orders[num_of_buy - i].side == 'buy' and orders[num_of_buy - i + 1].side == 'buy'
            if orders[num_of_buy - i].price - orders[num_of_buy - i + 1].price > 1.2 * p:
                buy_price = orders[num_of_buy - i + 1].price - p
                if p_lower < buy_price < p_upper:
                    order_info = place_order(ex, symbol, 'buy', amount, buy_price)
                    if order_info:
                        new_orders.append(GridOrder.from_order_info(order_info))

        for i in range(num_of_sell - 2):
            # check the price difference of adjacent sell orders, if it is greater than p, place a new order
            assert orders[num_of_buy + i].side == 'sell' and orders[num_of_buy + i + 1].side == 'sell'
            if orders[num_of_buy + i + 1].price - orders[num_of_buy + i].price > 1.2 * p:
                sell_price = orders[num_of_buy + i].price + p
                if p_lower < sell_price < p_upper:
                    order_info = place_order(ex, symbol, 'sell', amount, sell_price)
                    if order_info:
                        new_orders.append(GridOrder.from_order_info(order_info))

        orders.extend(new_orders)
        orders = sorted(orders, key=lambda x: x.price)
        num_of_buy = len([order for order in orders if order.side == 'buy'])
        num_of_sell = len(orders) - num_of_buy

        last_buy_price = orders[0].price
        last_sell_price = orders[-1].price

        # cancel orders if the number of orders is more than max_orders * 2
        if time.time() - last_check_time > 10:
            last_check_time = time.time()
            cancel_waitlist = []
            if num_of_buy > patience:
                cancel_waitlist.extend(orders[:num_of_buy - max_orders])
                orders = orders[num_of_buy - max_orders:]

            if num_of_sell > patience:
                cancel_waitlist.extend(orders[-(num_of_sell - max_orders):])
                orders = orders[:-(num_of_sell - max_orders)]

            if len(cancel_waitlist) > 0:
                for order in cancel_waitlist:
                    try:
                        order.cancel_order(ex)
                    except Exception as e:
                        logger.error(f"取消訂單失敗: {e}, 檢查是否已成交")
                        post_order = order.strategy_run(ex, p, p_upper, p_lower)
                        if post_order:
                            orders.append(post_order)

        if num_of_buy < max_orders:
            logging.debug(f"買單數量不足，補單")
            for i in range(1, max_orders - num_of_buy + 1):
                buy_price = last_buy_price - p * i
                if p_lower < buy_price < p_upper:
                    order_info = place_order(ex, symbol, 'buy', amount, buy_price)
                    if order_info:
                        orders.append(GridOrder.from_order_info(order_info))

        if num_of_sell < max_orders:
            logging.debug(f"賣單數量不足，補單")
            for i in range(1, max_orders - num_of_sell + 1):
                sell_price = last_sell_price + p * i
                if p_lower < sell_price < p_upper:
                    order_info = place_order(ex, symbol, 'sell', amount, sell_price)
                    if order_info:
                        orders.append(GridOrder.from_order_info(order_info))

        orders = sorted(orders, key=lambda x: abs(x.price - last_price))

        new_orders = []
        for order in orders:
            new_order = order.strategy_run(ex, p, p_upper, p_lower)
            if new_order:
                new_orders.append(new_order)

        orders = new_orders
        time.sleep(1)
