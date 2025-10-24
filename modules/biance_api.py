from binance.um_futures import UMFutures
from binance.error import ClientError
import time
from datetime import datetime, timedelta
from requests.exceptions import ConnectionError, ReadTimeout
from urllib3.exceptions import ProtocolError


def calculate_body_ratio(open_price, close_price, high, low):
    """计算实体占比"""
    total_length = float(high) - float(low)
    body_length = abs(float(close_price) - float(open_price))
    if total_length == 0:
        return 0
    return body_length / total_length

def is_red_candle(open_price, close_price):
    """判断是否为红柱"""
    return float(close_price) < float(open_price)

def is_green_candle(open_price, close_price):
    """判断是否为绿柱"""
    return float(close_price) > float(open_price)

def calculate_price_change(open_price, close_price):
    """计算价格变化百分比"""
    return (float(close_price) - float(open_price)) / float(open_price) * 100

def format_result_message(trigger_price, next_available_time):
    """格式化结果信息"""
    return (
        f"\n=== 检测到BTCUSDT事件合约模式目标 ===\n"
        f"当前价格: ${trigger_price:,.2f}\n"
        f"时间单位: 10分钟\n"
        f"执行操作: 上涨\n"
        f"数量: 5.00 USDT\n"
        f"下次可用时间: {next_available_time}\n"
        + "="*35
    )

def format_check_result(trigger_time, trigger_price, current_price, price_change, is_success):
    """格式化检查结果信息"""
    result = (
        f"\n=== BTCUSDT事件合约模式结果检查 ===\n"
        f"触发时间: ${trigger_time}\n"
        f"触发时价格: ${trigger_price:,.2f}\n"
        f"当前价格: ${current_price:,.2f}\n"
        f"价格变化: {price_change:.2f}%\n"
    )
    if is_success:
        result += "结果: 胜利 ✅\n数量变化: +9.00 USDT\n"
    else:
        result += "结果: 失败 ❌\n数量变化: -5.00 USDT\n"
    result += "="*35
    return result

def check_result(trigger_time, trigger_price):
    """不再等待，仅计算结果"""
    try:
        client = UMFutures()
        current_price = float(client.mark_price("BTCUSDT")['markPrice'])
        price_change = ((current_price - trigger_price) / trigger_price * 100)
        return format_check_result(
            trigger_time,
            trigger_price, 
            current_price,
            price_change,
            current_price > trigger_price
        )
    except Exception as e:
        return f"检查结果时发生错误: {e}"

def monitor_btc_pattern(body_ratio_threshold=0.7, green_vs_red_ratio=1.0):
    """只检测当前是否符合模式，不阻塞等待结果"""
    try:
        client = UMFutures(timeout=30)
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                klines = client.mark_price_klines("BTCUSDT", '5m', limit=3)
                break
            except (ConnectionError, ProtocolError, ReadTimeout) as e:
                if attempt == max_retries - 1:
                    return [f"连接错误 (尝试 {max_retries} 次): {e}"], None
                time.sleep(retry_delay)
        
        if len(klines) < 3:
            return ["获取数据不足"], None
            
        candles = []
        for k in klines:
            candles.append({
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'time': datetime.fromtimestamp(k[0]/1000)
            })
        
        # 连续两根红柱+第三根绿柱+涨幅计算
        if (is_red_candle(candles[0]['open'], candles[0]['close']) and
            is_red_candle(candles[1]['open'], candles[1]['close']) and
            is_green_candle(candles[2]['open'], candles[2]['close'])):
            
            ratio1 = calculate_body_ratio(candles[0]['open'], candles[0]['close'], 
                                          candles[0]['high'], candles[0]['low'])
            green_change = calculate_price_change(candles[2]['open'], candles[2]['close'])
            red_change = abs(calculate_price_change(candles[1]['open'], candles[1]['close']))
            
            if (ratio1 >= body_ratio_threshold and 
                green_change >= red_change * green_vs_red_ratio):
                
                trigger_price = candles[2]['close']
                trigger_dt = candles[2]['time'] + timedelta(minutes=5)  # 得到datetime
                trigger_time = trigger_dt.timestamp()                  # 转为浮点秒数
                
                message = format_result_message(
                    trigger_price,
                    datetime.now() + timedelta(minutes=10)
                )
                return [message], (trigger_price, trigger_time)
        
        return [], None
            
    except ClientError as e:
        return [f"API错误: {e.error_message}"], None
    except Exception as e:
        return [f"发生未知错误: {e}"], None