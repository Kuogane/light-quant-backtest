#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股量化回测引擎 v4.0 —— 组合模式 + 防未来函数 + 跨平台

===============================================
架构说明
===============================================
主程序 (stock_backtest.py) 负责：
  - 数据下载 / 加载 / 清洗（含 volume/amount 等扩展列）
  - 图表绘制（单股详情 / 多股汇总 / 组合资产曲线）
  - 技术指标计算
  - 调用外部量化插件执行回测（1只或多只，统一接口）
  - 组合回测引擎：单股轮动 / Top-N等权 / 得分加权
  - 防未来函数：订单延后一天执行

量化插件 (quant_*.py) 负责：
  - 策略定义（可使用任何外部因子）
  - 自定义交易成本（策略级 default_config）
  - 统一回测入口 run_quant_backtest
  - 组合评分接口 score()

===============================================
量化插件接口规范 (v4.0)
===============================================
【函数 1】get_strategies() -> dict
  返回策略注册表。

【函数 2】run_quant_backtest(stock_data_dict, strategy_id,
                             strategy_params, config) -> dict
  统一回测入口。stock_data_dict 中股票数量由主程序决定：
    1只  -> 单股回测，返回单股详细指标
    多只 -> 多股回测，返回汇总 + 每只股票指标

返回值：
  {
      "summary": { ... },          # 汇总指标
      "stock_results": { ... },     # 单股结果字典
      "config_used": { ... },      # 实际使用的配置
  }
"""

import os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
import sys
import glob
import math
import platform
import importlib.util
import inspect
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import logging
import datetime

# ==================== 多平台超高清中文字体配置 ====================
def _setup_chinese_font():
    system = platform.system()
    font_candidates = []
    if system == 'Windows':
        font_candidates += [
            'C:/Windows/Fonts/msyh.ttc',
            'C:/Windows/Fonts/simhei.ttf',
            'C:/Windows/Fonts/simsun.ttc',
            os.path.expanduser('~/AppData/Local/Microsoft/Windows/Fonts/NotoSansCJKsc-Regular.otf'),
        ]
    if system == 'Linux':
        font_candidates += [
            '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        ]
        for root in ['/usr/share/fonts', '/usr/local/share/fonts']:
            if os.path.isdir(root):
                for dirpath, _, filenames in os.walk(root):
                    for fn in filenames:
                        lower = fn.lower()
                        if any(k in lower for k in ['notosanscjk', 'wqy', 'zenhei', 'microhei']):
                            font_candidates.append(os.path.join(dirpath, fn))
    if system == 'Darwin':
        font_candidates += [
            '/System/Library/Fonts/PingFang.ttc',
            '/System/Library/Fonts/Hiragino Sans GB.ttc',
        ]
    font_candidates += [
        '/system/fonts/NotoSansCJK-Regular.ttc',
        '/system/fonts/DroidSansFallback.ttf',
        os.path.expanduser('~/.termux/fonts/NotoSansCJKsc-Regular.otf'),
    ]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    font_candidates += [
        os.path.join(script_dir, 'NotoSansCJKsc-Regular.otf'),
        os.path.join(script_dir, 'SimHei.ttf'),
        os.path.join(script_dir, 'font.ttf'),
    ]
    for fp in font_candidates:
        if os.path.exists(fp):
            try:
                font_manager.fontManager.addfont(fp)
                prop = font_manager.FontProperties(fname=fp)
                plt.rcParams['font.family'] = prop.get_name()
                plt.rcParams['axes.unicode_minus'] = False
                plt.rcParams['font.size'] = 14
                plt.rcParams['axes.titlesize'] = 20
                plt.rcParams['axes.labelsize'] = 14
                plt.rcParams['xtick.labelsize'] = 12
                plt.rcParams['ytick.labelsize'] = 12
                plt.rcParams['legend.fontsize'] = 12
                plt.rcParams['figure.dpi'] = 150
                plt.rcParams['savefig.dpi'] = 800
                plt.rcParams['savefig.bbox'] = 'tight'
                plt.rcParams['savefig.pad_inches'] = 0.2
                return True
            except Exception:
                continue
    try:
        fm = font_manager.FontManager()
        for font in fm.ttflist:
            name = font.name.lower()
            fname = font.fname.lower()
            if any(k in name or k in fname for k in ['noto sans cjk', 'wenquanyi', 'yahei', 'simhei',
                                                        'pingfang', 'heiti', 'source han']):
                plt.rcParams['font.family'] = font.name
                plt.rcParams['axes.unicode_minus'] = False
                plt.rcParams['font.size'] = 14
                plt.rcParams['axes.titlesize'] = 20
                plt.rcParams['savefig.dpi'] = 800
                return True
    except Exception:
        pass
    return False


# ==================== 配置 ====================
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
PHOTO_DIR = os.path.join(SCRIPT_DIR, "photo")

DEFAULT_CONFIG = {
    'initial_cash': 200000,
    'commission_rate': 0.0001,
    'min_commission': 5,
    'lot_size': 100,
    'slippage': 0.0,
    'risk_free_rate': 0.03,
    'limit_up_pct': 0.095,
    'limit_down_pct': 0.095,
    'delay_execution': False,
}

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = os.path.join(LOG_DIR, f'backtest_{ts}.log')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    logging.info(f'日志系统初始化完成: {log_path}')
    return log_path

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

def color_profit(val):
    if val > 0: return f"{RED}{val:+.2f}{RESET}"
    elif val < 0: return f"{GREEN}{val:+.2f}{RESET}"
    return f"{val:+.2f}"

def color_pct(val):
    if val > 0: return f"{RED}{val:+.2f}%{RESET}"
    elif val < 0: return f"{GREEN}{val:+.2f}%{RESET}"
    return f"{val:+.2f}%"

def color_price(val, ref):
    if ref is None or val == ref: return f"{val:.2f}"
    if val > ref: return f"{RED}{val:.2f}{RESET}"
    return f"{GREEN}{val:.2f}{RESET}"


# ==================== 账户类 ====================
class Account:
    """
    A股账户类。
    【锁定】T+1、整手交易、涨停禁买、跌停禁卖 —— 硬编码不可修改。
    【开放】佣金率、最低佣金、滑点、初始资金 —— 由外部 config 传入。
    """
    def __init__(self, cash, config=None):
        cfg = config or DEFAULT_CONFIG
        self.initial = cash
        self.cash = cash
        self.hold_total = 0
        self.hold_avail = 0
        self.frozen = 0
        self.cost_price = 0.0
        self.trades = []
        self.daily_history = []
        self._commission_rate = cfg.get('commission_rate', 0.0001)
        self._min_commission = cfg.get('min_commission', 5)
        self._lot_size = cfg.get('lot_size', 100)
        self._slippage = cfg.get('slippage', 0.0)
        self._risk_free = cfg.get('risk_free_rate', 0.03)
        self._delay_execution = cfg.get('delay_execution', False)
        self.pending_orders = []

    def daily_thaw(self):
        """T+1 解冻"""
        self.hold_avail += self.frozen
        self.frozen = 0

    def calc_commission(self, turnover):
        return max(turnover * self._commission_rate, self._min_commission)

    def _apply_slippage(self, price, action):
        if action == 'buy':
            return price * (1 + self._slippage)
        elif action == 'sell':
            return price * (1 - self._slippage)
        return price

    def buy(self, price, amount, date, idx):
        if amount % self._lot_size != 0:
            print(f"买入失败：必须是{self._lot_size}股的整数倍")
            return False
        if amount <= 0:
            print("买入失败：数量必须大于0")
            return False
        exec_price = self._apply_slippage(price, 'buy')
        turnover = exec_price * amount
        commission = self.calc_commission(turnover)
        total_pay = turnover + commission
        if self.cash < total_pay:
            print(f"买入失败：资金不足（需{total_pay:.2f}，含佣金{commission:.2f}）")
            return False
        if self.hold_total == 0:
            self.cost_price = exec_price
        else:
            old_cost = self.hold_total * self.cost_price
            new_cost = turnover
            self.cost_price = (old_cost + new_cost) / (self.hold_total + amount)
        self.cash -= total_pay
        self.hold_total += amount
        self.frozen += amount
        self.trades.append({'idx': idx, 'date': date, 'action': 'buy',
                            'price': exec_price, 'amount': amount, 'commission': commission})
        print(f"买入 {amount} 股，成交价 {exec_price:.2f}（含滑点），佣金 {commission:.2f}")
        return True

    def sell(self, price, amount, date, idx):
        if amount % self._lot_size != 0:
            print(f"卖出失败：必须是{self._lot_size}股的整数倍")
            return False
        if amount <= 0:
            print("卖出失败：数量必须大于0")
            return False
        if amount > self.hold_avail:
            print(f"卖出失败：可用持仓不足（可用{self.hold_avail}）")
            return False
        exec_price = self._apply_slippage(price, 'sell')
        turnover = exec_price * amount
        commission = self.calc_commission(turnover)
        net_income = turnover - commission
        self.cash += net_income
        self.hold_total -= amount
        self.hold_avail -= amount
        if self.hold_total == 0:
            self.cost_price = 0.0
        self.trades.append({'idx': idx, 'date': date, 'action': 'sell',
                            'price': exec_price, 'amount': amount, 'commission': commission})
        print(f"卖出 {amount} 股，成交价 {exec_price:.2f}（含滑点），佣金 {commission:.2f}")
        return True

    def record_day(self, date, close_price):
        self.daily_history.append({
            'date': date, 'cash': self.cash, 'hold_total': self.hold_total,
            'hold_avail': self.hold_avail, 'frozen': self.frozen,
            'cost_price': self.cost_price, 'close': close_price,
            'total_asset': self.cash + self.hold_total * close_price,
            'market_value': self.hold_total * close_price,
        })

    def submit_pending(self, action, amount, date, idx):
        if amount <= 0:
            return False
        self.pending_orders.append({
            'action': action,
            'amount': amount,
            'submit_date': date,
            'submit_idx': idx
        })
        return True

    def execute_pending(self, open_p, high, low, close, prev_close, date, idx, config=None):
        if not self.pending_orders:
            return []
        cfg = config or DEFAULT_CONFIG
        is_yz = is_limit_up(open_p, high, low, close, prev_close, cfg)
        is_dt = is_limit_down(open_p, high, low, close, prev_close, cfg)
        results = []
        for order in self.pending_orders:
            action = order['action']
            amount = order['amount']
            if action == 'buy':
                if is_yz:
                    print(f"{YELLOW}延后买入订单被拦截：{date.strftime('%Y-%m-%d')} 一字涨停{RESET}")
                    results.append((action, amount, False))
                else:
                    ok = self.buy(open_p, amount, date, idx)
                    results.append((action, amount, ok))
            elif action == 'sell':
                if is_dt:
                    print(f"{YELLOW}延后卖出订单被拦截：{date.strftime('%Y-%m-%d')} 一字跌停{RESET}")
                    results.append((action, amount, False))
                else:
                    ok = self.sell(open_p, amount, date, idx)
                    results.append((action, amount, ok))
        self.pending_orders = []
        return results

    def total_asset(self, price):
        return self.cash + self.hold_total * price

    def market_value(self, price):
        return self.hold_total * price

    def profit(self, price):
        return self.total_asset(price) - self.initial

    def profit_pct(self, price):
        return (self.profit(price) / self.initial) * 100




# ==================== 组合账户类 ====================
class PortfolioAccount:
    """
    组合账户类 —— 统一现金池 + 多股持仓字典。
    支持单股轮动、Top-N等权、得分加权等组合策略。
    """
    def __init__(self, cash, config=None):
        cfg = config or DEFAULT_CONFIG
        self.initial = cash
        self.cash = cash
        self.positions = {}   # code -> {'hold_total': 0, 'hold_avail': 0, 'frozen': 0, 'cost_price': 0.0}
        self.trades = []
        self.daily_history = []
        self._commission_rate = cfg.get('commission_rate', 0.0001)
        self._min_commission = cfg.get('min_commission', 5)
        self._lot_size = cfg.get('lot_size', 100)
        self._slippage = cfg.get('slippage', 0.0)
        self._risk_free = cfg.get('risk_free_rate', 0.03)
        self._delay_execution = cfg.get('delay_execution', False)
        self.pending_orders = []  # {'action': 'buy'/'sell', 'code': code, 'amount': amount, ...}

    def daily_thaw(self):
        for code, pos in self.positions.items():
            pos['hold_avail'] += pos['frozen']
            pos['frozen'] = 0

    def calc_commission(self, turnover):
        return max(turnover * self._commission_rate, self._min_commission)

    def _apply_slippage(self, price, action):
        if action == 'buy':
            return price * (1 + self._slippage)
        elif action == 'sell':
            return price * (1 - self._slippage)
        return price

    def buy(self, code, price, amount, date, idx):
        if amount % self._lot_size != 0 or amount <= 0:
            return False
        exec_price = self._apply_slippage(price, 'buy')
        turnover = exec_price * amount
        commission = self.calc_commission(turnover)
        total_pay = turnover + commission
        if self.cash < total_pay:
            return False
        if code not in self.positions:
            self.positions[code] = {'hold_total': 0, 'hold_avail': 0, 'frozen': 0, 'cost_price': 0.0}
        pos = self.positions[code]
        if pos['hold_total'] == 0:
            pos['cost_price'] = exec_price
        else:
            old_cost = pos['hold_total'] * pos['cost_price']
            new_cost = turnover
            pos['cost_price'] = (old_cost + new_cost) / (pos['hold_total'] + amount)
        self.cash -= total_pay
        pos['hold_total'] += amount
        pos['frozen'] += amount
        self.trades.append({'idx': idx, 'date': date, 'action': 'buy', 'code': code,
                            'price': exec_price, 'amount': amount, 'commission': commission})
        return True

    def sell(self, code, price, amount, date, idx):
        if code not in self.positions:
            return False
        pos = self.positions[code]
        if amount % self._lot_size != 0 or amount <= 0:
            return False
        if amount > pos['hold_avail']:
            return False
        exec_price = self._apply_slippage(price, 'sell')
        turnover = exec_price * amount
        commission = self.calc_commission(turnover)
        net_income = turnover - commission
        self.cash += net_income
        pos['hold_total'] -= amount
        pos['hold_avail'] -= amount
        if pos['hold_total'] == 0:
            pos['cost_price'] = 0.0
        self.trades.append({'idx': idx, 'date': date, 'action': 'sell', 'code': code,
                            'price': exec_price, 'amount': amount, 'commission': commission})
        return True

    def submit_pending(self, action, code, amount, date, idx):
        if amount <= 0:
            return False
        self.pending_orders.append({'action': action, 'code': code, 'amount': amount,
                                    'submit_date': date, 'submit_idx': idx})
        return True

    def execute_pending(self, open_p, high, low, close, prev_close, date, idx, config=None):
        if not self.pending_orders:
            return []
        cfg = config or DEFAULT_CONFIG
        is_yz = is_limit_up(open_p, high, low, close, prev_close, cfg)
        is_dt = is_limit_down(open_p, high, low, close, prev_close, cfg)
        results = []
        for order in self.pending_orders:
            action = order['action']
            code = order['code']
            amount = order['amount']
            if action == 'buy':
                if is_yz:
                    print(f"{YELLOW}延后买入订单被拦截：{code} {date.strftime('%Y-%m-%d')} 一字涨停{RESET}")
                    results.append((action, code, amount, False))
                else:
                    ok = self.buy(code, open_p, amount, date, idx)
                    results.append((action, code, amount, ok))
            elif action == 'sell':
                if is_dt:
                    print(f"{YELLOW}延后卖出订单被拦截：{code} {date.strftime('%Y-%m-%d')} 一字跌停{RESET}")
                    results.append((action, code, amount, False))
                else:
                    ok = self.sell(code, open_p, amount, date, idx)
                    results.append((action, code, amount, ok))
        self.pending_orders = []
        return results

    def total_asset(self, prices):
        mv = sum(pos['hold_total'] * prices.get(code, 0) for code, pos in self.positions.items())
        return self.cash + mv

    def market_value(self, prices):
        return sum(pos['hold_total'] * prices.get(code, 0) for code, pos in self.positions.items())

    def record_day(self, date, prices):
        ta = self.total_asset(prices)
        mv = self.market_value(prices)
        self.daily_history.append({
            'date': date, 'cash': self.cash,
            'positions': {k: dict(v) for k, v in self.positions.items()},
            'total_asset': ta, 'market_value': mv, 'prices': dict(prices),
        })

    def profit(self, prices):
        return self.total_asset(prices) - self.initial

    def profit_pct(self, prices):
        return (self.profit(prices) / self.initial) * 100

# ==================== 数据工具 ====================
def list_stocks():
    if not os.path.exists(DATA_DIR):
        return []
    files = glob.glob(os.path.join(DATA_DIR, "*.xlsx"))
    return sorted([os.path.splitext(os.path.basename(f))[0] for f in files])

def _normalize_df(df):
    col_map = {}
    for col in df.columns:
        c = str(col).strip()
        if c in ['日期', 'date', 'Date', '时间', 'time', 'Time']:
            col_map['date'] = col
        elif c in ['开盘', 'open', 'Open', '开盘价']:
            col_map['open'] = col
        elif c in ['最高', 'high', 'High', '最高价']:
            col_map['high'] = col
        elif c in ['最低', 'low', 'Low', '最低价']:
            col_map['low'] = col
        elif c in ['收盘', 'close', 'Close', '收盘价']:
            col_map['close'] = col
        elif c in ['成交量', 'volume', 'Volume', 'VOL', 'vol', '成交']:
            col_map['volume'] = col
        elif c in ['成交额', 'amount', 'Amount', 'AMOUNT', '成交总额', 'turnover']:
            col_map['amount'] = col
        elif c in ['涨跌幅', 'pct_change', 'change_pct', '涨跌幅(%)', 'pctChg']:
            col_map['pct_change'] = col
        elif c in ['涨跌额', 'change', 'Change', 'change_amount']:
            col_map['change'] = col
        elif c in ['昨收', 'pre_close', 'preclose', 'prev_close', 'PreClose']:
            col_map['pre_close'] = col
    for k, v in col_map.items():
        df = df.rename(columns={v: k})
    required = ['date', 'open', 'high', 'low', 'close']
    missing = [c for c in required if c not in df.columns]
    if missing:
        return None, missing
    # 保留所有列，方便下游策略使用 volume/amount 等扩展字段
    df = df.copy()
    # 对常见可选列缺失时自动补 0，避免下游插件 KeyError
    for opt_col in ['volume', 'amount', 'pct_change', 'change', 'pre_close']:
        if opt_col not in df.columns:
            df[opt_col] = 0.0
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    df = df.dropna()
    return df, None

def load_stock(code, auto_crawl=True):
    path = os.path.join(DATA_DIR, f"{code}.xlsx")
    if not os.path.exists(path):
        if auto_crawl:
            print(f"\n{YELLOW}警告：找不到 {path}{RESET}")
            s = input(f"是否调用爬虫下载 {code} 数据？(y/n): ").strip().lower()
            if s in ('y', 'yes', '是'):
                if download_stock(code):
                    return load_stock(code, auto_crawl=False)
        return None
    try:
        df = pd.read_excel(path)
        df, missing = _normalize_df(df)
        if df is None:
            print(f"错误：缺少必要列 {missing}")
            return None
        return df
    except Exception as e:
        print(f"读取文件出错：{e}")
        logging.error(f"读取股票文件 {code} 出错: {e}", exc_info=True)
        return None

def load_stock_raw(code):
    path = os.path.join(DATA_DIR, f"{code}.xlsx")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_excel(path)
        df, missing = _normalize_df(df)
        if df is None:
            return None
        return df
    except Exception:
        return None


# ==================== 爬虫接口 ====================
def download_stock(code, source='eastmoney'):
    import requests
    code = str(code).strip()
    if not code.isdigit():
        print(f"{RED}错误：{code} 不是有效的股票代码{RESET}")
        return False
    print(f"{CYAN}>>> 正在下载 {code}（源: {source}）{RESET}")
    if source == 'eastmoney':
        secid = f"1.{code}" if code.startswith('6') else f"0.{code}"
        url = ("http://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56"
               "&klt=101&fqt=0&end=20500101&lmt=1000")
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            data = resp.json()
            if data.get("data") is None or data["data"].get("klines") is None:
                return False
            rows = []
            for line in data["data"]["klines"]:
                parts = line.split(",")
                if len(parts) < 6: continue
                rows.append({"date": parts[0], "open": float(parts[1]),
                             "close": float(parts[2]), "low": float(parts[3]),
                             "high": float(parts[4])})
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            df = df[["date", "open", "high", "low", "close"]]
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, f"{code}.xlsx")
            df.to_excel(path, index=False)
            print(f"{GREEN}  ✓ 已保存 {path}（{len(df)} 条记录）{RESET}")
            return True
        except Exception as e:
            print(f"{RED}  下载失败：{e}{RESET}")
            logging.error(f"下载股票 {code} 失败 ({source}): {e}", exc_info=True)
            if source == 'eastmoney':
                return download_stock(code, source='tencent')
            return False
    elif source == 'tencent':
        symbol = f"sh{code}" if code.startswith('6') else f"sz{code}"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,1000,qfq"
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            data = resp.json()
            raw = data["data"].get(symbol, {}).get("day", [])
            rows = []
            for item in raw:
                if len(item) < 5: continue
                rows.append({"date": item[0], "open": float(item[1]),
                             "close": float(item[2]), "low": float(item[3]),
                             "high": float(item[4])})
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            df = df[["date", "open", "high", "low", "close"]]
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, f"{code}.xlsx")
            df.to_excel(path, index=False)
            print(f"{GREEN}  ✓ 已保存 {path}（{len(df)} 条记录）{RESET}")
            return True
        except Exception as e:
            print(f"{RED}  腾讯源也失败了：{e}{RESET}")
            return False
    return False


# ==================== 一字板/跌停板检测 ====================
def is_limit_up(open_p, high, low, close, prev_close, config=None):
    cfg = config or DEFAULT_CONFIG
    if prev_close is None or prev_close <= 0:
        return False
    if not (open_p == high == low == close):
        return False
    return close >= prev_close * (1 + cfg.get('limit_up_pct', 0.095))

def is_limit_down(open_p, high, low, close, prev_close, config=None):
    cfg = config or DEFAULT_CONFIG
    if prev_close is None or prev_close <= 0:
        return False
    if not (open_p == high == low == close):
        return False
    return close <= prev_close * (1 - cfg.get('limit_down_pct', 0.095))


# ==================== 技术指标计算 ====================
def calc_metrics(account, df, start_idx=0):
    if not account.daily_history:
        return {}
    assets = [r['total_asset'] for r in account.daily_history]
    n = len(assets)
    if n == 0:
        return {}
    returns = []
    for i in range(1, n):
        r = (assets[i] - assets[i-1]) / assets[i-1] if assets[i-1] > 0 else 0
        returns.append(r)
    peak = assets[0]
    max_dd = 0
    for i in range(n):
        if assets[i] > peak:
            peak = assets[i]
        dd = (peak - assets[i]) / peak
        if dd > max_dd:
            max_dd = dd
    total_return = (assets[-1] - account.initial) / account.initial
    annual_return = total_return * 252 / n if n > 0 else 0
    if len(returns) > 1:
        mean_ret = np.mean(returns)
        std_ret = np.std(returns, ddof=1)
        if std_ret > 0:
            sharpe = (mean_ret * 252 - account._risk_free) / (std_ret * np.sqrt(252))
        else:
            sharpe = 0
    else:
        sharpe = 0
    buy_queue = []
    trade_profits = []
    win_count = 0
    lose_count = 0
    win_amount = 0
    lose_amount = 0
    for t in account.trades:
        if t['action'] == 'buy':
            buy_queue.append((t['price'], t['amount']))
        elif t['action'] == 'sell':
            sell_amt = t['amount']
            sell_price = t['price']
            total_cost = 0
            total_amt = 0
            while sell_amt > 0 and buy_queue:
                b_price, b_amt = buy_queue[0]
                if b_amt <= sell_amt:
                    total_cost += b_price * b_amt
                    total_amt += b_amt
                    sell_amt -= b_amt
                    buy_queue.pop(0)
                else:
                    total_cost += b_price * sell_amt
                    total_amt += sell_amt
                    buy_queue[0] = (b_price, b_amt - sell_amt)
                    sell_amt = 0
            if total_amt > 0:
                profit = (sell_price - total_cost / total_amt) * total_amt
                trade_profits.append(profit)
    for p in trade_profits:
        if p > 0:
            win_count += 1
            win_amount += p
        elif p < 0:
            lose_count += 1
            lose_amount += abs(p)
    total_trades = win_count + lose_count
    win_rate = win_count / total_trades * 100 if total_trades > 0 else 0
    profit_loss_ratio = (win_amount / win_count) / (lose_amount / lose_count) if win_count > 0 and lose_count > 0 else 0
    max_win_streak = 0
    max_lose_streak = 0
    cur_win = 0
    cur_lose = 0
    for r in returns:
        if r > 0:
            cur_win += 1
            cur_lose = 0
            max_win_streak = max(max_win_streak, cur_win)
        elif r < 0:
            cur_lose += 1
            cur_win = 0
            max_lose_streak = max(max_lose_streak, cur_lose)
        else:
            cur_win = 0
            cur_lose = 0
    return {
        'total_return': total_return * 100,
        'annual_return': annual_return * 100,
        'max_drawdown': max_dd * 100,
        'sharpe': sharpe,
        'win_rate': win_rate,
        'profit_loss_ratio': profit_loss_ratio,
        'total_trades': total_trades,
        'win_count': win_count,
        'lose_count': lose_count,
        'max_win_streak': max_win_streak,
        'max_lose_streak': max_lose_streak,
        'final_asset': assets[-1],
        'volatility': np.std(returns, ddof=1) * np.sqrt(252) * 100 if len(returns) > 1 else 0,
    }


def print_metrics(metrics, prefix="   "):
    if not metrics:
        return
    print(f"{prefix}总收益率:     {color_pct(metrics['total_return'])}")
    print(f"{prefix}年化收益率:   {color_pct(metrics['annual_return'])}")
    print(f"{prefix}最大回撤:     {GREEN}{metrics['max_drawdown']:.2f}%{RESET}")
    print(f"{prefix}夏普比率:     {metrics['sharpe']:.2f}")
    print(f"{prefix}波动率(年化): {metrics['volatility']:.2f}%")
    print(f"{prefix}胜率:         {metrics['win_rate']:.1f}% ({metrics['win_count']}胜/{metrics['lose_count']}负)")
    print(f"{prefix}盈亏比:       {metrics['profit_loss_ratio']:.2f}")
    print(f"{prefix}交易笔数:     {metrics['total_trades']}")
    print(f"{prefix}最大连续盈利: {metrics['max_win_streak']} 天")
    print(f"{prefix}最大连续亏损: {metrics['max_lose_streak']} 天")
    print(f"{prefix}最终资产:     {metrics['final_asset']:.2f}")

def print_summary(account, price):
    total_asset = account.total_asset(price)
    market_val = account.market_value(price)
    profit = account.profit(price)
    profit_pct = account.profit_pct(price)
    print(f"\n{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}              📊 测试结束统计{RESET}")
    print(f"{CYAN}{'='*60}{RESET}")
    print(f"   初始资金: {account.initial:.2f}")
    print(f"   现金余额: {account.cash:.2f}")
    print(f"   成本价:   {account.cost_price:.2f}")
    print(f"   现价:     {price:.2f}")
    print(f"   持仓:     {account.hold_total} 股")
    print(f"   可用:     {account.hold_avail} 股")
    print(f"   总市值:   {market_val:.2f}")
    print(f"   总资产:   {total_asset:.2f}")
    print(f"   盈亏额:   {color_profit(profit)}")
    print(f"   盈亏率:   {color_pct(profit_pct)}")
    print(f"   交易笔数: {len(account.trades)}")
    print(f"{CYAN}{'='*60}{RESET}")


# ==================== 策略加载（兼容旧版 strategies.py） ====================
class _BaseQuantStrategy:
    name = "基类"
    desc = "基类"
    param_info = {}
    default_config = {}
    def __init__(self, account=None, df=None, **kwargs):
        self.account = account
        self.df = df
        self.params = kwargs
    def _max_buy(self, price, ratio=1.0):
        lots = int(self.account.cash * ratio // price // 100) if price > 0 else 0
        return lots * 100
    def _max_sell(self, ratio=1.0):
        lots = int(self.account.hold_avail * ratio // 100)
        return lots * 100
    def decide(self, i, row, open_p):
        return 'hold', 0

def load_strategies():
    try:
        from strategies import QUANT_STRATEGIES, QuantStrategy
        return QUANT_STRATEGIES, QuantStrategy
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, 'strategies.py')
    if not os.path.exists(path):
        path = './strategies.py'
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw_code = f.read()
        lines = raw_code.split('\n')
        clean_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('from strategies import') or stripped.startswith('from . import'):
                clean_lines.append('# [主程序自动注释] ' + line)
                continue
            clean_lines.append(line)
        code = '\n'.join(clean_lines)
        namespace = {'__name__': 'strategies_loaded', 'pd': pd, 'QuantStrategy': _BaseQuantStrategy}
        exec(code, namespace)
        QuantStrategy = namespace.get('QuantStrategy', _BaseQuantStrategy)
        QUANT_STRATEGIES = namespace.get('QUANT_STRATEGIES', {})
        return QUANT_STRATEGIES, QuantStrategy
    except Exception as e:
        print(f"{RED}加载策略文件失败：{e}{RESET}")
        logging.error(f"加载策略文件失败: {e}", exc_info=True)
        return {}, None

def _create_strategy(cls, account, df, kwargs):
    kwargs = kwargs or {}
    try:
        obj = cls(account, df, **kwargs)
        if getattr(obj, 'account', None) is not None and getattr(obj, 'df', None) is not None:
            return obj
    except Exception:
        pass
    try:
        obj = cls(df, account, **kwargs)
        if getattr(obj, 'account', None) is not None and getattr(obj, 'df', None) is not None:
            return obj
    except Exception:
        pass
    try:
        obj = cls(account, df)
        if getattr(obj, 'params', None) is None:
            obj.params = kwargs
        if getattr(obj, 'account', None) is not None and getattr(obj, 'df', None) is not None:
            return obj
    except Exception:
        pass
    try:
        obj = cls(df, account)
        if getattr(obj, 'params', None) is None:
            obj.params = kwargs
        if getattr(obj, 'account', None) is not None and getattr(obj, 'df', None) is not None:
            return obj
    except Exception:
        pass
    for args in [(account,), (df,), ()]:
        try:
            obj = cls(*args)
            if getattr(obj, 'account', None) is None:
                obj.account = account
            if getattr(obj, 'df', None) is None:
                obj.df = df
            if getattr(obj, 'params', None) is None:
                obj.params = kwargs
            return obj
        except Exception:
            pass
    try:
        sig = inspect.signature(cls.__init__)
        params = list(sig.parameters.keys())
        if len(params) >= 3:
            call_kwargs = {'account': account, 'df': df}
            call_kwargs.update(kwargs)
            accepted = {k: v for k, v in call_kwargs.items() if k in params}
            obj = cls(**accepted)
            if getattr(obj, 'account', None) is None:
                obj.account = account
            if getattr(obj, 'df', None) is None:
                obj.df = df
            if getattr(obj, 'params', None) is None:
                obj.params = kwargs
            return obj
    except Exception:
        pass
    raise TypeError(f"无法初始化策略 {cls.__name__}")


# ==================== 量化插件扫描与加载 ====================
QUANT_PREFIX = "quant_"

def _scan_quant_plugins():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    patterns = [
        os.path.join(script_dir, f"{QUANT_PREFIX}*.py"),
        os.path.join(os.getcwd(), f"{QUANT_PREFIX}*.py"),
    ]
    candidates = []
    seen = set()
    for pat in patterns:
        for p in glob.glob(pat):
            abs_p = os.path.abspath(p)
            if abs_p == os.path.abspath(__file__):
                continue
            if "__pycache__" in abs_p:
                continue
            if not os.path.isfile(abs_p):
                continue
            if abs_p not in seen:
                seen.add(abs_p)
                candidates.append(abs_p)
    candidates.sort(key=lambda x: os.path.basename(x).lower())
    return candidates

def load_quant_engine(path):
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("quant_engine", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['quant_engine'] = mod
        spec.loader.exec_module(mod)
        if not hasattr(mod, 'get_strategies'):
            print(f"{YELLOW}警告：{path} 未提供 get_strategies() 接口{RESET}")
        if not hasattr(mod, 'run_quant_backtest'):
            print(f"{YELLOW}警告：{path} 未提供 run_quant_backtest() 接口{RESET}")
        return mod
    except Exception as e:
        print(f"{RED}加载量化插件失败：{e}{RESET}")
        logging.error(f"加载量化插件失败: {e}", exc_info=True)
        return None

def pick_quant_engine():
    candidates = _scan_quant_plugins()
    if not candidates:
        print(f"\n{YELLOW}未找到量化插件。{RESET}")
        print(f"{YELLOW}请确保同目录下有以 '{QUANT_PREFIX}' 开头的 .py 文件。{RESET}")
        print(f"{YELLOW}例如：{QUANT_PREFIX}engine.py, {QUANT_PREFIX}alpha101.py, {QUANT_PREFIX}my_v2.py{RESET}")
        return None
    print(f"\n{CYAN}{'='*50}{RESET}")
    print(f"{BOLD}         选择量化插件（固定前缀: {QUANT_PREFIX}）{RESET}")
    print(f"{CYAN}{'='*50}{RESET}")
    for i, p in enumerate(candidates, 1):
        basename = os.path.basename(p)
        info = ""
        try:
            mod = load_quant_engine(p)
            if mod and hasattr(mod, 'get_strategies'):
                n = len(mod.get_strategies())
                info = f"  [{n}个策略]"
        except Exception:
            pass
        print(f"  {i}. {basename}{info}")
    print(f"{CYAN}{'='*50}{RESET}")
    print(f"  提示：插件文件名前缀必须为 '{QUANT_PREFIX}'，后缀可自定义")
    print(f"  例如：{QUANT_PREFIX}engine.py / {QUANT_PREFIX}v2.py / {QUANT_PREFIX}gpt_2026.py")
    print(f"{CYAN}{'='*50}{RESET}")
    s = input(f"选择插件 (1~{len(candidates)}，回车=默认1): ").strip()
    if s == '':
        idx = 0
    elif s.isdigit():
        idx = int(s) - 1
        if idx < 0 or idx >= len(candidates):
            print(f"{YELLOW}输入超出范围，使用默认第1个{RESET}")
            idx = 0
    else:
        print(f"{YELLOW}无效输入，使用默认第1个{RESET}")
        idx = 0
    selected = candidates[idx]
    print(f"{CYAN}>>> 已选择插件: {os.path.basename(selected)}{RESET}")
    return load_quant_engine(selected)


# ==================== 配置交互 ====================
def edit_config_interactive(base_config):
    cfg = dict(base_config)
    print(f"\n{CYAN}{'='*50}{RESET}")
    print(f"{BOLD}         回测参数配置（开放参数）{RESET}")
    print(f"{CYAN}{'='*50}{RESET}")
    print(f"  以下参数可由量化策略自由定义，你也可以自定义调整：")
    print(f"{CYAN}{'='*50}{RESET}")
    prompts = {
        'initial_cash': ('初始资金（元）', float),
        'commission_rate': ('佣金率（如 0.0001 = 万1）', float),
        'min_commission': ('最低佣金（元）', float),
        'slippage': ('滑点比例（如 0.001 = 0.1%，买入+滑点，卖出-滑点）', float),
        'risk_free_rate': ('年化无风险利率（如 0.03 = 3%）', float),
        'limit_up_pct': ('涨停判定阈值（如 0.095 = 9.5%）', float),
        'limit_down_pct': ('跌停判定阈值（如 0.095 = 9.5%）', float),
    }
    for key, (desc, ptype) in prompts.items():
        current = cfg.get(key, DEFAULT_CONFIG.get(key, ''))
        v = input(f"  {desc} (当前 {current}): ").strip()
        if v:
            try:
                cfg[key] = ptype(v)
            except ValueError:
                print(f"    {YELLOW}输入无效，保留原值 {current}{RESET}")
    print(f"\n{CYAN}【A股硬规则】（不可修改）{RESET}")
    print(f"  每手股数: {cfg.get('lot_size', 100)} 股")
    print(f"  交易制度: T+1")
    print(f"  涨跌停限制: 涨停禁止买入，跌停禁止卖出")
    print(f"{CYAN}{'='*50}{RESET}")
    return cfg


# ==================== 股票选择交互 ====================
def pick_stocks(all_stocks):
    if not all_stocks:
        return []
    print(f"\n{CYAN}可用股票（共 {len(all_stocks)} 只）：{RESET}")
    for i, code in enumerate(all_stocks, 1):
        print(f"  {i:3d}. {code}")
    print(f"\n{BOLD}选择方式：{RESET}")
    print("  a. 输入序号范围，如 1-5 或 1,3,5")
    print("  b. 输入 all 选择全部")
    print("  c. 直接输入股票代码，如 000001,000002")
    print("  d. 输入单个序号或代码 = 单股量化")
    sel = input("请选择: ").strip().lower()
    selected = []
    if sel == 'all':
        selected = all_stocks[:]
    elif ',' in sel or '-' in sel:
        parts = sel.split(',')
        for p in parts:
            p = p.strip()
            if '-' in p:
                try:
                    a, b = p.split('-')
                    a, b = int(a.strip()), int(b.strip())
                    selected.extend(all_stocks[a-1:b])
                except Exception:
                    pass
            else:
                try:
                    idx = int(p)
                    if 1 <= idx <= len(all_stocks):
                        selected.append(all_stocks[idx-1])
                except Exception:
                    if p in all_stocks:
                        selected.append(p)
    else:
        for c in sel.replace(' ', '').split(','):
            if c in all_stocks:
                selected.append(c)
        if not selected:
            try:
                idx = int(sel)
                if 1 <= idx <= len(all_stocks):
                    selected.append(all_stocks[idx-1])
            except Exception:
                pass
    return list(dict.fromkeys(selected))


# ==================== 统一量化模式（单股/多股合一） ====================


# ==================== 组合回测引擎 ====================
def run_portfolio_backtest(stock_data_dict, engine, strategy_id, strategy_params, config):
    """
    组合回测引擎 —— 支持单股轮动、Top-N等权、得分加权三种模式。
    统一现金池，多股持仓，T+1制度。
    """
    portfolio_mode = config.get('portfolio_mode', 'single_rotation')
    top_n = config.get('top_n', 3)
    initial_cash = config.get('initial_cash', 200000)
    account = PortfolioAccount(initial_cash, config)

    # 获取策略类
    strategies = engine.get_strategies()
    strategy_cls = strategies[strategy_id]['class']

    # 统一日期索引：取所有股票的交易日交集（或并集？用并集更合理，缺失数据用前值）
    all_dates = set()
    for df in stock_data_dict.values():
        all_dates.update(df['date'].tolist())
    all_dates = sorted(all_dates)

    # 为每只股票建立 date -> row 的映射，方便快速查找
    stock_maps = {}
    for code, df in stock_data_dict.items():
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        stock_maps[code] = {row['date']: row for _, row in df.iterrows()}

    # 逐日遍历
    for day_idx, current_date in enumerate(all_dates):
        # 收集当日有数据的股票及其价格
        available = {}
        prices = {}
        for code, mmap in stock_maps.items():
            if current_date in mmap:
                row = mmap[current_date]
                available[code] = row
                prices[code] = float(row['close'])

        if not available:
            continue

        # T+1 解冻
        account.daily_thaw()

        # 延后执行 pending 订单
        if account._delay_execution and day_idx > 0:
            prev_date = all_dates[day_idx - 1]
            for code in list(account.pending_orders):
                # 找到对应股票的 prev_close
                prev_row = stock_maps.get(code, {}).get(prev_date)
                if prev_row is None:
                    continue
                prev_close = float(prev_row['close'])
                row = available.get(code)
                if row is None:
                    continue
                open_p = float(row['open'])
                high_p = float(row['high'])
                low_p = float(row['low'])
                close_p = float(row['close'])
                account.execute_pending(open_p, high_p, low_p, close_p, prev_close, current_date, day_idx, config)

        # 构建当日 DataFrame 片段供策略评分
        day_slice = {}
        for code, row in available.items():
            day_slice[code] = row

        # 调用策略评分（如果策略支持 score 方法）
        scores = {}
        try:
            # 尝试用策略类实例化一个临时对象调用 score
            # 由于策略可能需要历史数据，这里简化：只传递当日数据
            # 实际应由插件内部实现组合回测，这里做兜底通用实现
            for code, row in available.items():
                scores[code] = 0.0
        except Exception:
            pass

        # 如果策略有 score 方法，优先使用
        try:
            dummy_account = PortfolioAccount(initial_cash, config)
            dummy_strategy = strategy_cls(dummy_account, None, **strategy_params)
            if hasattr(dummy_strategy, 'score'):
                for code, row in available.items():
                    try:
                        s = dummy_strategy.score(code, row, day_idx)
                        scores[code] = float(s) if s is not None else 0.0
                    except Exception:
                        scores[code] = 0.0
        except Exception:
            pass

        # 如果所有得分都是0，尝试用简单动量作为默认评分
        if all(v == 0.0 for v in scores.values()):
            for code, row in available.items():
                # 简单动量：当日涨跌幅
                prev_close = None
                if day_idx > 0:
                    prev_date = all_dates[day_idx - 1]
                    prev_row = stock_maps.get(code, {}).get(prev_date)
                    if prev_row is not None:
                        prev_close = float(prev_row['close'])
                if prev_close and prev_close > 0:
                    scores[code] = (float(row['close']) - prev_close) / prev_close
                else:
                    scores[code] = 0.0

        # 根据模式调仓
        if portfolio_mode == 'single_rotation':
            _rotate_single(account, available, scores, current_date, day_idx, config)
        elif portfolio_mode == 'top_n_equal':
            _rebalance_top_n_equal(account, available, scores, current_date, day_idx, config, top_n)
        elif portfolio_mode == 'score_weighted':
            _rebalance_score_weighted(account, available, scores, current_date, day_idx, config)

        # 记录每日资产
        account.record_day(current_date, prices)

    # 计算最终指标
    final_prices = {}
    for code, mmap in stock_maps.items():
        if all_dates and all_dates[-1] in mmap:
            final_prices[code] = float(mmap[all_dates[-1]]['close'])

    metrics = _calc_portfolio_metrics(account, all_dates)
    summary = metrics.copy()
    summary['final_asset'] = account.total_asset(final_prices)
    summary['total_return'] = (summary['final_asset'] - account.initial) / account.initial * 100

    # 每只股票的独立指标（用于展示）
    stock_results = {}
    for code in stock_data_dict.keys():
        stock_results[code] = {
            'metrics': {'total_return': 0, 'total_trades': 0},
            'trades': [t for t in account.trades if t.get('code') == code],
            'daily_history': [],
        }

    return {
        'summary': summary,
        'stock_results': stock_results,
        'portfolio_history': account.daily_history,
        'config_used': config,
    }


def _rotate_single(account, available, scores, date, idx, config):
    """单股轮动：持仓得分最高1只，切换时先卖后买"""
    if not scores:
        return
    best_code = max(scores, key=scores.get)
    best_score = scores[best_code]
    if best_score <= 0:
        return  # 没有正向信号，空仓

    # 当前持仓（非冻结）
    held_codes = [c for c, p in account.positions.items() if p['hold_total'] > 0]

    # 如果已经持有最佳股票，不动
    if best_code in held_codes and len(held_codes) == 1:
        return

    # 卖出所有当前持仓（可用部分）
    for code in held_codes:
        pos = account.positions[code]
        if pos['hold_avail'] > 0:
            sell_lots = pos['hold_avail'] // account._lot_size
            if sell_lots > 0:
                row = available.get(code)
                if row is not None:
                    open_p = float(row['open'])
                    if account._delay_execution:
                        account.submit_pending('sell', code, sell_lots * account._lot_size, date, idx)
                    else:
                        account.sell(code, open_p, sell_lots * account._lot_size, date, idx)

    # 买入最佳股票
    row = available.get(best_code)
    if row is not None:
        open_p = float(row['open'])
        max_lots = int(account.cash // open_p // account._lot_size)
        if max_lots > 0:
            if account._delay_execution:
                account.submit_pending('buy', best_code, max_lots * account._lot_size, date, idx)
            else:
                account.buy(best_code, open_p, max_lots * account._lot_size, date, idx)


def _rebalance_top_n_equal(account, available, scores, date, idx, config, top_n):
    """Top-N等权：选得分最高的N只，每只目标仓位 = 总资产 / N"""
    if not scores:
        return
    sorted_codes = sorted(scores, key=scores.get, reverse=True)
    target_codes = [c for c in sorted_codes[:top_n] if scores[c] > 0]
    if not target_codes:
        return

    # 计算目标每只仓位（基于当前总资产）
    prices = {c: float(available[c]['open']) for c in target_codes if c in available}
    total_asset = account.total_asset({c: float(available[c]['close']) for c in available if c in available})
    target_value_per = total_asset / len(target_codes)

    # 先卖出不在目标列表中的持仓
    for code, pos in list(account.positions.items()):
        if code not in target_codes and pos['hold_avail'] > 0:
            sell_lots = pos['hold_avail'] // account._lot_size
            if sell_lots > 0 and code in available:
                open_p = float(available[code]['open'])
                if account._delay_execution:
                    account.submit_pending('sell', code, sell_lots * account._lot_size, date, idx)
                else:
                    account.sell(code, open_p, sell_lots * account._lot_size, date, idx)

    # 对目标股票调仓至等权
    for code in target_codes:
        if code not in available:
            continue
        open_p = float(available[code]['open'])
        pos = account.positions.get(code, {'hold_total': 0, 'hold_avail': 0})
        current_value = pos['hold_total'] * open_p
        diff_value = target_value_per - current_value

        if diff_value > 0:
            # 需要买入
            buy_amount = int(diff_value // open_p // account._lot_size) * account._lot_size
            if buy_amount > 0 and account.cash >= buy_amount * open_p * 1.001:
                if account._delay_execution:
                    account.submit_pending('buy', code, buy_amount, date, idx)
                else:
                    account.buy(code, open_p, buy_amount, date, idx)
        elif diff_value < 0:
            # 需要卖出
            sell_amount = int(abs(diff_value) // open_p // account._lot_size) * account._lot_size
            avail = pos.get('hold_avail', 0)
            sell_amount = min(sell_amount, avail)
            if sell_amount > 0:
                if account._delay_execution:
                    account.submit_pending('sell', code, sell_amount, date, idx)
                else:
                    account.sell(code, open_p, sell_amount, date, idx)


def _rebalance_score_weighted(account, available, scores, date, idx, config):
    """得分加权：按得分比例分配权重，得分可为负，只取正得分股票"""
    pos_scores = {c: s for c, s in scores.items() if s > 0 and c in available}
    if not pos_scores:
        return
    total_score = sum(pos_scores.values())
    total_asset = account.total_asset({c: float(available[c]['close']) for c in available if c in available})

    # 先卖出不在正得分列表中的持仓
    for code, pos in list(account.positions.items()):
        if code not in pos_scores and pos['hold_avail'] > 0:
            sell_lots = pos['hold_avail'] // account._lot_size
            if sell_lots > 0 and code in available:
                open_p = float(available[code]['open'])
                if account._delay_execution:
                    account.submit_pending('sell', code, sell_lots * account._lot_size, date, idx)
                else:
                    account.sell(code, open_p, sell_lots * account._lot_size, date, idx)

    # 按得分加权调仓
    for code, score in pos_scores.items():
        weight = score / total_score
        target_value = total_asset * weight
        open_p = float(available[code]['open'])
        pos = account.positions.get(code, {'hold_total': 0, 'hold_avail': 0})
        current_value = pos['hold_total'] * open_p
        diff_value = target_value - current_value

        if diff_value > 0:
            buy_amount = int(diff_value // open_p // account._lot_size) * account._lot_size
            if buy_amount > 0 and account.cash >= buy_amount * open_p * 1.001:
                if account._delay_execution:
                    account.submit_pending('buy', code, buy_amount, date, idx)
                else:
                    account.buy(code, open_p, buy_amount, date, idx)
        elif diff_value < 0:
            sell_amount = int(abs(diff_value) // open_p // account._lot_size) * account._lot_size
            avail = pos.get('hold_avail', 0)
            sell_amount = min(sell_amount, avail)
            if sell_amount > 0:
                if account._delay_execution:
                    account.submit_pending('sell', code, sell_amount, date, idx)
                else:
                    account.sell(code, open_p, sell_amount, date, idx)


def _calc_portfolio_metrics(account, all_dates):
    """计算组合级别的技术指标"""
    if not account.daily_history:
        return {}
    assets = [r['total_asset'] for r in account.daily_history]
    n = len(assets)
    if n == 0:
        return {}
    returns = []
    for i in range(1, n):
        r = (assets[i] - assets[i-1]) / assets[i-1] if assets[i-1] > 0 else 0
        returns.append(r)
    peak = assets[0]
    max_dd = 0
    for i in range(n):
        if assets[i] > peak:
            peak = assets[i]
        dd = (peak - assets[i]) / peak
        if dd > max_dd:
            max_dd = dd
    total_return = (assets[-1] - account.initial) / account.initial
    annual_return = total_return * 252 / n if n > 0 else 0
    if len(returns) > 1:
        mean_ret = np.mean(returns)
        std_ret = np.std(returns, ddof=1)
        if std_ret > 0:
            sharpe = (mean_ret * 252 - account._risk_free) / (std_ret * np.sqrt(252))
        else:
            sharpe = 0
    else:
        sharpe = 0
    buy_queue = {}
    trade_profits = []
    win_count = 0
    lose_count = 0
    win_amount = 0
    lose_amount = 0
    for t in account.trades:
        code = t.get('code', '')
        if t['action'] == 'buy':
            if code not in buy_queue:
                buy_queue[code] = []
            buy_queue[code].append((t['price'], t['amount']))
        elif t['action'] == 'sell':
            if code not in buy_queue or not buy_queue[code]:
                continue
            sell_amt = t['amount']
            sell_price = t['price']
            total_cost = 0
            total_amt = 0
            bq = buy_queue[code]
            while sell_amt > 0 and bq:
                b_price, b_amt = bq[0]
                if b_amt <= sell_amt:
                    total_cost += b_price * b_amt
                    total_amt += b_amt
                    sell_amt -= b_amt
                    bq.pop(0)
                else:
                    total_cost += b_price * sell_amt
                    total_amt += sell_amt
                    bq[0] = (b_price, b_amt - sell_amt)
                    sell_amt = 0
            if total_amt > 0:
                profit = (sell_price - total_cost / total_amt) * total_amt
                trade_profits.append(profit)
    for p in trade_profits:
        if p > 0:
            win_count += 1
            win_amount += p
        elif p < 0:
            lose_count += 1
            lose_amount += abs(p)
    total_trades = win_count + lose_count
    win_rate = win_count / total_trades * 100 if total_trades > 0 else 0
    profit_loss_ratio = (win_amount / win_count) / (lose_amount / lose_count) if win_count > 0 and lose_count > 0 else 0
    max_win_streak = 0
    max_lose_streak = 0
    cur_win = 0
    cur_lose = 0
    for r in returns:
        if r > 0:
            cur_win += 1
            cur_lose = 0
            max_win_streak = max(max_win_streak, cur_win)
        elif r < 0:
            cur_lose += 1
            cur_win = 0
            max_lose_streak = max(max_lose_streak, cur_lose)
        else:
            cur_win = 0
            cur_lose = 0
    return {
        'total_return': total_return * 100,
        'annual_return': annual_return * 100,
        'max_drawdown': max_dd * 100,
        'sharpe': sharpe,
        'win_rate': win_rate,
        'profit_loss_ratio': profit_loss_ratio,
        'total_trades': total_trades,
        'win_count': win_count,
        'lose_count': lose_count,
        'max_win_streak': max_win_streak,
        'max_lose_streak': max_lose_streak,
        'final_asset': assets[-1],
        'volatility': np.std(returns, ddof=1) * np.sqrt(252) * 100 if len(returns) > 1 else 0,
    }

def run_quant_mode():
    """
    【v3.3】统一量化入口。
    单股和多股共用同一套插件、策略、配置体系。
    选1只股票 = 单股回测 + 单股K线图
    选多只股票 = 多股汇总 + 汇总折线图（默认不生成单股详图）
    """
    # 1. 加载插件
    engine = pick_quant_engine()
    if engine is None:
        print(f"{RED}无法加载量化插件，返回主菜单。{RESET}")
        return False
    if not hasattr(engine, 'get_strategies'):
        print(f"{RED}量化插件未实现 get_strategies() 接口{RESET}")
        return False
    strategies = engine.get_strategies()
    if not strategies:
        print(f"{YELLOW}量化插件未注册任何策略{RESET}")
        return False

    # 2. 编辑开放配置
    base_config = dict(DEFAULT_CONFIG)
    base_config = edit_config_interactive(base_config)

    # 3. 选择策略
    print(f"\n{CYAN}{'='*50}{RESET}")
    print(f"{BOLD}         选择量化策略{RESET}")
    print(f"{CYAN}{'='*50}{RESET}")
    for k, v in strategies.items():
        name = v.get('name', v.get('class', object).__name__)
        desc = v.get('desc', '无描述')
        dc = v.get('default_config', {})
        extra = f" [自定义配置: {dc}]" if dc else ""
        print(f"  {k}. {name} - {desc}{extra}")
    print(f"{CYAN}{'='*50}{RESET}")
    s = input("选择策略(回车=取消): ").strip()
    if s not in strategies:
        print("取消")
        return False
    strategy_info = strategies[s]
    strategy_id = s

    # 策略级配置覆盖
    strategy_default = strategy_info.get('default_config', {})
    if strategy_default:
        print(f"\n{CYAN}策略 '{strategy_info.get('name')}' 提供了默认配置覆盖：{RESET}")
        for k_, v_ in strategy_default.items():
            print(f"  {k_}: {base_config.get(k_, 'N/A')} -> {v_}")
        print(f"{YELLOW}是否接受以上覆盖？(回车=接受，n=拒绝){RESET}")
        if input().strip().lower() in ('n', 'no', '否'):
            print(f"{CYAN}已拒绝策略级覆盖，使用用户配置。{RESET}")
            base_config = edit_config_interactive(DEFAULT_CONFIG)
        else:
            for k_, v_ in strategy_default.items():
                base_config[k_] = v_

    # 4. 策略参数配置
    kwargs = {}
    param_info = strategy_info.get('param_info', {})
    if param_info:
        name = strategy_info.get('name', '策略')
        print(f"\n【{name} 参数配置】")
        for pname, (desc, ptype, default) in param_info.items():
            v = input(f"  {desc} (默认{default}): ").strip()
            kwargs[pname] = ptype(v) if v else default

    # 5. 组合模式选择
    print(f"\n{CYAN}{'='*50}{RESET}")
    print(f"{BOLD}         📊 多股量化组合模式选择{RESET}")
    print(f"{CYAN}{'='*50}{RESET}")
    print(f"  {BOLD}1. 独立回测{RESET}（默认）—— 每只股票独立账户，互不影响")
    print(f"  {BOLD}2. 单股轮动{RESET}—— 统一账户，每次只持仓1只最强股票，切换时先卖后买")
    print(f"  {BOLD}3. Top-N等权{RESET}—— 统一账户，每天选得分最高的N只，每只等权配置")
    print(f"  {BOLD}4. 得分加权{RESET}—— 统一账户，按因子得分比例分配权重")
    print(f"{CYAN}{'='*50}{RESET}")
    mode_ans = input("  选择组合模式(1/2/3/4，回车=默认1): ").strip()
    portfolio_mode = 'independent'
    top_n = 3
    if mode_ans == '2':
        portfolio_mode = 'single_rotation'
        print(f"{GREEN}  >>> 已选择：单股轮动模式{RESET}")
    elif mode_ans == '3':
        portfolio_mode = 'top_n_equal'
        n_ans = input("  Top-N 的 N 值（默认3）: ").strip()
        top_n = int(n_ans) if n_ans.isdigit() else 3
        print(f"{GREEN}  >>> 已选择：Top-{top_n} 等权组合模式{RESET}")
    elif mode_ans == '4':
        portfolio_mode = 'score_weighted'
        print(f"{GREEN}  >>> 已选择：得分加权组合模式{RESET}")
    else:
        print(f"{YELLOW}  >>> 已选择：独立回测模式（每只独立账户）{RESET}")
    base_config['portfolio_mode'] = portfolio_mode
    base_config['top_n'] = top_n

    # 6. 延后执行配置（高亮交互）
    print(f"\n{CYAN}{'='*50}{RESET}")
    print(f"{BOLD}{YELLOW}         ⚠️  防未来函数：订单执行时机设置{RESET}")
    print(f"{CYAN}{'='*50}{RESET}")
    print(f"  {BOLD}延后一天执行{RESET}：模型第T日提交的买卖请求，第T+1日开盘才执行")
    print(f"  {BOLD}立即执行{RESET}：模型第T日提交的请求，当天立即成交（可能含未来函数）")
    print(f"{CYAN}{'='*50}{RESET}")
    delay_ans = input(f"  是否开启延后一天执行？({BOLD}y{RESET}=开启/{BOLD}n{RESET}=关闭，默认n): ").strip().lower()
    if delay_ans in ('y', 'yes', '是', 'true', '1'):
        base_config['delay_execution'] = True
        print(f"{GREEN}  >>> 已开启：延后一天执行（防未来函数）{RESET}")
    else:
        base_config['delay_execution'] = False
        print(f"{YELLOW}  >>> 已关闭：当天立即执行{RESET}")

    # 8. 选择股票
    all_stocks = list_stocks()
    if not all_stocks:
        print(f"{RED}未找到任何股票数据{RESET}")
        return False
    selected_codes = pick_stocks(all_stocks)
    if not selected_codes:
        print(f"{YELLOW}未选择任何股票{RESET}")
        return False
    print(f"\n{CYAN}已选择 {len(selected_codes)} 只股票: {', '.join(selected_codes)}{RESET}")

    stock_data = {}
    missing = []
    for code in selected_codes:
        df = load_stock_raw(code)
        if df is not None:
            stock_data[code] = df
        else:
            missing.append(code)
    if missing:
        print(f"{YELLOW}以下股票数据缺失，已跳过: {', '.join(missing)}{RESET}")
    if not stock_data:
        print(f"{RED}没有可回测的股票数据{RESET}")
        return False

    # 6. 调用插件统一回测接口
    print(f"\n{CYAN}>>> 启动量化回测，共 {len(stock_data)} 只股票...{RESET}")
    print(f"{CYAN}>>> 配置: 初始资金={base_config['initial_cash']}, 佣金={base_config['commission_rate']}, "
          f"滑点={base_config.get('slippage', 0)}{RESET}")
    try:
        result = engine.run_quant_backtest(stock_data, strategy_id, kwargs, base_config)
    except Exception as e:
        print(f"{RED}量化插件执行失败：{e}{RESET}")
        logging.error(f"量化插件执行失败: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        return False

    if result is None or not isinstance(result, dict):
        print(f"{RED}量化插件返回格式错误{RESET}")
        return False

    config_used = result.get('config_used', base_config)
    summary = result.get('summary', {})
    stock_results = result.get('stock_results', {})
    n_stocks = len(stock_results)
    save_dir = ensure_photo_dir()

    if n_stocks == 1:
        # ===== 单股展示 =====
        code = list(stock_results.keys())[0]
        r = stock_results[code]
        m = r.get('metrics', {})
        print(f"\n{CYAN}{'='*70}{RESET}")
        print(f"{BOLD}                    📊 单股量化回测报告 [{code}]{RESET}")
        print(f"{CYAN}{'='*70}{RESET}")
        print(f"   最终资产: {m.get('final_asset', 0):.2f}")
        print(f"   总收益率: {color_pct(m.get('total_return', 0))}")
        print_metrics(m, prefix="   ")
        print(f"{CYAN}{'='*70}{RESET}")
        # 单股生成详细K线图
        trades = r.get('trades', [])
        dh = r.get('daily_history', [])
        if trades or dh:
            acc = Account(config_used.get('initial_cash', DEFAULT_CONFIG['initial_cash']), config_used)
            acc.trades = trades
            acc.daily_history = dh
            df = stock_data.get(code)
            if df is not None:
                generate_chart(df, acc, code, save_dir)
    else:
        # ===== 多股展示 =====
        print(f"\n{CYAN}{'='*70}{RESET}")
        print(f"{BOLD}                    📊 多股量化回测汇总报告{RESET}")
        print(f"{CYAN}{'='*70}{RESET}")
        print(f"   回测股票数:      {n_stocks} 只")
        print(f"   模型胜率:        {summary.get('model_win_rate', 0)*100:.1f}%  ({summary.get('win_stock_count', 0)}盈/{summary.get('loss_stock_count', 0)}亏)")
        print(f"   平均总收益率:    {color_pct(summary.get('avg_total_return', 0))}")
        print(f"   平均年化收益率:  {color_pct(summary.get('avg_annual_return', 0))}")
        print(f"   平均最大回撤:    {GREEN}{summary.get('avg_max_drawdown', 0):.2f}%{RESET}")
        print(f"   平均夏普比率:    {summary.get('avg_sharpe', 0):.2f}")
        print(f"   平均波动率:      {summary.get('avg_volatility', 0):.2f}%")
        print(f"   总交易笔数:      {summary.get('total_trades', 0)} 笔")
        print(f"   平均每只股票:    {summary.get('avg_trades_per_stock', 0):.1f} 笔")
        if summary.get('best_stock'):
            print(f"   最佳股票:        {summary.get('best_stock')}  ({color_pct(stock_results[summary['best_stock']]['metrics'].get('total_return', 0))})")
        if summary.get('worst_stock'):
            print(f"   最差股票:        {summary.get('worst_stock')}  ({color_pct(stock_results[summary['worst_stock']]['metrics'].get('total_return', 0))})")
        print(f"{CYAN}{'='*70}{RESET}")

        for code, r in stock_results.items():
            m = r.get('metrics', {})
            print(f"\n{BOLD}[{code}]{RESET}  最终资产: {m.get('final_asset', 0):.2f}  收益率: {color_pct(m.get('total_return', 0))}")
            print_metrics(m, prefix="      ")

        # 绘制多股汇总图（默认，不生成单股详图）
        generate_multi_stock_chart(stock_results, config_used=config_used, save_dir=save_dir)

        # 【v3.3】可选：仅当用户明确要求时才生成单股详图
        print(f"\n{CYAN}是否生成每只股票的单股详细K线图？(y/n，默认n){RESET}")
        if input().strip().lower() in ('y', 'yes', '是'):
            for code, r in stock_results.items():
                trades = r.get('trades', [])
                dh = r.get('daily_history', [])
                if trades or dh:
                    acc = Account(config_used.get('initial_cash', DEFAULT_CONFIG['initial_cash']), config_used)
                    acc.trades = trades
                    acc.daily_history = dh
                    df = stock_data.get(code)
                    if df is not None:
                        generate_chart(df, acc, code, save_dir)

    return True


# ==================== 图表生成 ====================
def ensure_photo_dir():
    try:
        os.makedirs(PHOTO_DIR, exist_ok=True)
        return PHOTO_DIR
    except Exception:
        fallback = os.path.join(SCRIPT_DIR, "photo")
        os.makedirs(fallback, exist_ok=True)
        return fallback

def get_next_filename(code, save_dir, suffix="chart"):
    base = os.path.join(save_dir, f"{code}_{suffix}")
    n = 1
    while True:
        path = f"{base}_{n:03d}.png"
        if not os.path.exists(path):
            return path
        n += 1

def generate_chart(df, account, code, save_dir=None):
    ok = _setup_chinese_font()
    if not ok:
        print(f"{YELLOW}警告：未找到中文字体{RESET}")
    if save_dir is None:
        save_dir = ensure_photo_dir()

    n = len(df)
    date_labels = df['date'].dt.strftime('%m-%d').tolist()
    opens = df['open'].astype(float).tolist()
    highs = df['high'].astype(float).tolist()
    lows = df['low'].astype(float).tolist()
    closes = df['close'].astype(float).tolist()

    ma5 = [None]*4 + [sum(closes[i-4:i+1])/5 for i in range(4, n)]
    ma20 = [None]*19 + [sum(closes[i-19:i+1])/20 for i in range(19, n)]

    daily_pnl = []
    if account.daily_history:
        assets = [r['total_asset'] for r in account.daily_history]
        for i in range(1, len(assets)):
            daily_pnl.append(assets[i] - assets[i-1])
        daily_pnl = [0] + daily_pnl
        if len(daily_pnl) < n:
            daily_pnl = daily_pnl + [0]*(n - len(daily_pnl))
        elif len(daily_pnl) > n:
            daily_pnl = daily_pnl[:n]
    else:
        daily_pnl = [0]*n

    market_vals = []
    cashes = []
    for r in account.daily_history:
        market_vals.append(r['market_value'])
        cashes.append(r['cash'])
    if len(market_vals) < n:
        market_vals = market_vals + [0]*(n - len(market_vals))
        cashes = cashes + [cashes[-1] if cashes else 0]*(n - len(cashes))
    elif len(market_vals) > n:
        market_vals = market_vals[:n]
        cashes = cashes[:n]

    buy_x, buy_y, buy_prices = [], [], []
    sell_x, sell_y, sell_prices = [], [], []
    for t in account.trades:
        idx = t['idx']
        if idx < n:
            if t['action'] == 'buy':
                buy_x.append(idx)
                buy_y.append(lows[idx] * 0.97)
                buy_prices.append(t['price'])
            else:
                sell_x.append(idx)
                sell_y.append(highs[idx] * 1.03)
                sell_prices.append(t['price'])

    fig = plt.figure(figsize=(18, 20))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1.5, 1.5, 1.5], hspace=0.25)

    ax1 = fig.add_subplot(gs[0])
    for i in range(n):
        color = '#ff3333' if closes[i] >= opens[i] else '#00aa00'
        ax1.plot([i, i], [opens[i], closes[i]], color=color, linewidth=4, solid_capstyle='butt', zorder=3)
        ax1.plot([i, i], [lows[i], highs[i]], color=color, linewidth=1, zorder=2)
    if ma5:
        ax1.plot(range(n), ma5, color='#ff9900', linewidth=1.2, alpha=0.8, label='MA5')
    if ma20:
        ax1.plot(range(n), ma20, color='#0099ff', linewidth=1.5, alpha=0.8, label='MA20')
    if buy_x:
        ax1.scatter(buy_x, buy_y, color='#ff0000', marker='^', s=150, zorder=5, edgecolors='white', linewidths=0.5, label=f'买入({len(buy_x)}笔)')
        for bx, by, bp in zip(buy_x, buy_y, buy_prices):
            ax1.annotate(f'{bp:.2f}', (bx, by), textcoords="offset points", xytext=(0, 14), ha='center', fontsize=10, color='#ff0000')
    if sell_x:
        ax1.scatter(sell_x, sell_y, color='#00aa00', marker='v', s=150, zorder=5, edgecolors='white', linewidths=0.5, label=f'卖出({len(sell_x)}笔)')
        for sx, sy, sp in zip(sell_x, sell_y, sell_prices):
            ax1.annotate(f'{sp:.2f}', (sx, sy), textcoords="offset points", xytext=(0, -18), ha='center', fontsize=10, color='#00aa00')
    ax1.set_title(f'{code} 回测详情 | 共{n}个交易日 | 交易{len(account.trades)}笔', fontsize=18, fontweight='bold', pad=12)
    ax1.set_ylabel('价格 (元)', fontsize=13)
    ax1.legend(loc='upper left', fontsize=11)
    ax1.grid(True, alpha=0.2, linestyle='--')
    ax1.set_xlim(-1, n)
    step = max(1, n // 12)
    ax1.set_xticks(range(0, n, step))
    ax1.set_xticklabels([date_labels[i] for i in range(0, n, step)], rotation=45, ha='right', fontsize=10)

    ax2 = fig.add_subplot(gs[1])
    if account.daily_history:
        assets = [r['total_asset'] for r in account.daily_history]
        if len(assets) < n:
            assets = assets + [assets[-1]]*(n - len(assets))
        elif len(assets) > n:
            assets = assets[:n]
        ax2.plot(range(n), assets, color='#0066ff', linewidth=1.5, label='总资产', zorder=3)
        ax2.fill_between(range(n), assets, account.initial,
                         where=[a >= account.initial for a in assets], color='#ffcccc', alpha=0.5, label='盈利区')
        ax2.fill_between(range(n), assets, account.initial,
                         where=[a < account.initial for a in assets], color='#ccffcc', alpha=0.5, label='亏损区')
        ax2.axhline(y=account.initial, color='#999999', linestyle='--', linewidth=1, label=f'初始资金 {account.initial:.0f}')
        peak = assets[0]
        dd_idx = 0
        max_dd = 0
        for i in range(n):
            if assets[i] > peak:
                peak = assets[i]
            dd = (peak - assets[i]) / peak
            if dd > max_dd:
                max_dd = dd
                dd_idx = i
        if max_dd > 0:
            ax2.scatter([dd_idx], [assets[dd_idx]], color='purple', s=80, zorder=5, marker='x')
            ax2.annotate(f'最大回撤 {max_dd*100:.1f}%', (dd_idx, assets[dd_idx]), textcoords="offset points", xytext=(10, 0), fontsize=10, color='purple')
    ax2.set_ylabel('总资产 (元)', fontsize=13)
    ax2.legend(loc='upper left', fontsize=11)
    ax2.grid(True, alpha=0.2, linestyle='--')
    ax2.set_xlim(-1, n)
    ax2.set_xticks(range(0, n, step))
    ax2.set_xticklabels([date_labels[i] for i in range(0, n, step)], rotation=45, ha='right', fontsize=10)

    ax3 = fig.add_subplot(gs[2])
    colors_bar = ['#ff4444' if p >= 0 else '#00aa00' for p in daily_pnl]
    ax3.bar(range(n), daily_pnl, color=colors_bar, width=0.8, alpha=0.7, edgecolor='none')
    ax3.axhline(y=0, color='#666666', linewidth=0.8)
    ax3.set_ylabel('当日盈亏 (元)', fontsize=13)
    ax3.grid(True, alpha=0.2, linestyle='--', axis='y')
    ax3.set_xlim(-1, n)
    ax3.set_xticks(range(0, n, step))
    ax3.set_xticklabels([date_labels[i] for i in range(0, n, step)], rotation=45, ha='right', fontsize=10)

    ax4 = fig.add_subplot(gs[3])
    ax4.plot(range(n), market_vals, color='#ff6600', linewidth=1.2, label='持仓市值', alpha=0.9)
    ax4.plot(range(n), cashes, color='#0099cc', linewidth=1.2, label='现金余额', alpha=0.9)
    ax4.fill_between(range(n), market_vals, 0, color='#ffcc99', alpha=0.3)
    ax4.set_ylabel('金额 (元)', fontsize=13)
    ax4.set_xlabel('日期', fontsize=13)
    ax4.legend(loc='upper left', fontsize=11)
    ax4.grid(True, alpha=0.2, linestyle='--')
    ax4.set_xlim(-1, n)
    ax4.set_xticks(range(0, n, step))
    ax4.set_xticklabels([date_labels[i] for i in range(0, n, step)], rotation=45, ha='right', fontsize=10)

    final_price = float(df.iloc[-1]['close'])
    final_value = account.total_asset(final_price)
    profit = final_value - account.initial
    profit_pct = (profit / account.initial) * 100
    metrics = calc_metrics(account, df, 0)
    if metrics:
        info = (f"初始: {account.initial:.0f}  |  最终: {final_value:.2f}  |  "
                f"盈亏: {profit:+.2f} ({profit_pct:+.2f}%)  |  "
                f"最大回撤: {metrics.get('max_drawdown', 0):.2f}%  |  "
                f"夏普: {metrics.get('sharpe', 0):.2f}  |  "
                f"胜率: {metrics.get('win_rate', 0):.1f}%  |  "
                f"交易: {len(account.trades)}笔")
    else:
        info = (f"初始: {account.initial:.0f}  |  最终: {final_value:.2f}  |  "
                f"盈亏: {profit:+.2f} ({profit_pct:+.2f}%)  |  "
                f"交易: {len(account.trades)}笔")

    fig.text(0.5, 0.005, info, ha='center', fontsize=12,
             bbox=dict(boxstyle='round,pad=0.5', facecolor='#fff8dc', edgecolor='#d4a017', linewidth=1.5, alpha=0.9))

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.06)
    filename = get_next_filename(code, save_dir)
    try:
        plt.savefig(filename, dpi=800, bbox_inches='tight', facecolor='white', edgecolor='none')
        print(f"\n{GREEN}✓ 超高清图表已保存: {filename}{RESET}")
    except Exception:
        alt = os.path.join(SCRIPT_DIR, "photo", os.path.basename(filename))
        os.makedirs(os.path.dirname(alt), exist_ok=True)
        plt.savefig(alt, dpi=800, bbox_inches='tight', facecolor='white', edgecolor='none')
        print(f"\n{GREEN}✓ 超高清图表已保存（备用路径）: {alt}{RESET}")
    plt.close()
    return filename


def generate_multi_stock_chart(stock_results, config_used=None, save_dir=None):
    """
    【v3.3】多股量化汇总图。
    核心设计：折线图清晰展示每只股票的总资产曲线，
    不同颜色区分，直观标注最好/最差股票。
    默认不生成单股详图，仅用此汇总图展示整体表现。
    """
    ok = _setup_chinese_font()
    if not ok:
        print(f"{YELLOW}警告：未找到中文字体{RESET}")
    if save_dir is None:
        save_dir = ensure_photo_dir()

    codes = list(stock_results.keys())
    n_stocks = len(codes)
    if n_stocks == 0:
        return None

    returns = []
    drawdowns = []
    sharpes = []
    trades = []
    win_rates = []
    assets_dict = {}

    for code in codes:
        r = stock_results[code]
        m = r.get('metrics', {})
        returns.append(m.get('total_return', 0))
        drawdowns.append(abs(m.get('max_drawdown', 0)))
        sharpes.append(m.get('sharpe', 0))
        trades.append(m.get('total_trades', 0))
        win_rates.append(m.get('win_rate', 0))
        dh = r.get('daily_history', [])
        if dh:
            assets_dict[code] = [d['total_asset'] for d in dh]

    # 找出最好和最差
    best_idx = int(np.argmax(returns)) if returns else 0
    worst_idx = int(np.argmin(returns)) if returns else 0
    best_code = codes[best_idx]
    worst_code = codes[worst_idx]

    fig = plt.figure(figsize=(20, 24))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1.5, 1.5, 1.5], hspace=0.28)

    colors = plt.cm.tab10(np.linspace(0, 1, max(10, n_stocks)))
    init_cash = config_used.get('initial_cash', 200000) if config_used else 200000

    # ========== 子图1: 总资产曲线叠加（核心）==========
    ax1 = fig.add_subplot(gs[0])
    max_len = max(len(v) for v in assets_dict.values()) if assets_dict else 0

    for i, code in enumerate(codes):
        assets = assets_dict.get(code, [])
        if not assets:
            continue
        # 统一X轴：按交易日索引
        x = np.arange(len(assets))
        # 最好股票：粗实线 + 星标终点
        # 最差股票：虚线
        # 其他：普通实线
        if code == best_code:
            ax1.plot(x, assets, color=colors[i % 10], linewidth=2.5, label=f'{code} (最佳 {returns[i]:+.1f}%)',
                     zorder=5, marker='*', markevery=[len(assets)-1], markersize=15)
        elif code == worst_code:
            ax1.plot(x, assets, color=colors[i % 10], linewidth=1.5, label=f'{code} (最差 {returns[i]:+.1f}%)',
                     zorder=3, linestyle='--', alpha=0.8)
        else:
            ax1.plot(x, assets, color=colors[i % 10], linewidth=1.3, label=f'{code} ({returns[i]:+.1f}%)',
                     zorder=3, alpha=0.85)

    # 初始资金基准线
    ax1.axhline(y=init_cash, color='#333333', linestyle='--', linewidth=2, label=f'初始资金 {init_cash}', zorder=2)

    # 标注区域
    ax1.set_title('多股量化回测 — 总资产曲线对比', fontsize=22, fontweight='bold', pad=15)
    ax1.set_ylabel('总资产 (元)', fontsize=14)
    # 图例分两列显示，避免过长
    ncols = min(4, n_stocks + 1)
    ax1.legend(loc='upper left', fontsize=10, ncol=ncols, framealpha=0.9)
    ax1.grid(True, alpha=0.2, linestyle='--')

    # 在图上用文字标注最好/最差
    if assets_dict.get(best_code):
        best_assets = assets_dict[best_code]
        ax1.annotate(f'🏆 最佳: {best_code}\n{returns[best_idx]:+.1f}%',
                     xy=(len(best_assets)-1, best_assets[-1]),
                     xytext=(len(best_assets)-1 + max_len*0.05, best_assets[-1]),
                     fontsize=12, fontweight='bold', color=colors[best_idx % 10],
                     bbox=dict(boxstyle='round,pad=0.4', facecolor='white', edgecolor=colors[best_idx % 10], alpha=0.9))
    if assets_dict.get(worst_code):
        worst_assets = assets_dict[worst_code]
        ax1.annotate(f'💔 最差: {worst_code}\n{returns[worst_idx]:+.1f}%',
                     xy=(len(worst_assets)-1, worst_assets[-1]),
                     xytext=(len(worst_assets)-1 + max_len*0.05, worst_assets[-1]),
                     fontsize=12, fontweight='bold', color=colors[worst_idx % 10],
                     bbox=dict(boxstyle='round,pad=0.4', facecolor='white', edgecolor=colors[worst_idx % 10], alpha=0.9))

    # ========== 子图2: 收益率排名横向条形图 ==========
    ax2 = fig.add_subplot(gs[1])
    # 按收益率排序
    sorted_idx = np.argsort(returns)[::-1]
    sorted_codes = [codes[i] for i in sorted_idx]
    sorted_returns = [returns[i] for i in sorted_idx]
    sorted_colors = [colors[i % 10] for i in sorted_idx]
    bar_colors = ['#ff3333' if r >= 0 else '#00aa00' for r in sorted_returns]

    bars = ax2.barh(range(n_stocks), sorted_returns, color=bar_colors, edgecolor='white', height=0.6)
    ax2.set_yticks(range(n_stocks))
    ax2.set_yticklabels(sorted_codes, fontsize=12)
    ax2.set_xlabel('总收益率 (%)', fontsize=13)
    ax2.set_title('收益率排名', fontsize=18, fontweight='bold', pad=10)
    ax2.axvline(x=0, color='#333333', linewidth=0.8)
    ax2.grid(True, alpha=0.2, linestyle='--', axis='x')
    for i, (bar, val) in enumerate(zip(bars, sorted_returns)):
        ax2.text(val + (1 if val >= 0 else -1), i, f'{val:+.1f}%',
                 va='center', ha='left' if val >= 0 else 'right', fontsize=11, fontweight='bold')

    # ========== 子图3: 最大回撤 + 夏普 双轴 ==========
    ax3 = fig.add_subplot(gs[2])
    x_pos = np.arange(n_stocks)
    width = 0.35
    ax3.bar(x_pos - width/2, drawdowns, width, color='#ff6600', alpha=0.7, label='最大回撤(%)')
    ax3_twin = ax3.twinx()
    ax3_twin.bar(x_pos + width/2, sharpes, width, color='#0099ff', alpha=0.7, label='夏普比率')
    ax3.set_xticks(x_pos)
    ax3.set_xticklabels(codes, rotation=45, ha='right', fontsize=10)
    ax3.set_ylabel('最大回撤 (%)', fontsize=12, color='#ff6600')
    ax3_twin.set_ylabel('夏普比率', fontsize=12, color='#0099ff')
    ax3.set_title('风险指标对比', fontsize=18, fontweight='bold', pad=10)
    ax3.grid(True, alpha=0.2, linestyle='--', axis='y')
    lines1, labels1 = ax3.get_legend_handles_labels()
    lines2, labels2 = ax3_twin.get_legend_handles_labels()
    ax3.legend(lines1 + lines2, labels1 + labels2, loc='upper right', fontsize=10)

    # ========== 子图4: 交易笔数 vs 胜率 气泡图 ==========
    ax4 = fig.add_subplot(gs[3])
    bubble_sizes = [max(50, t * 15) for t in trades]
    scatter_colors = ['#ff3333' if r >= 0 else '#00aa00' for r in returns]
    ax4.scatter(trades, win_rates, s=bubble_sizes, c=scatter_colors, alpha=0.7, edgecolors='white', linewidths=1)
    for i, code in enumerate(codes):
        ax4.annotate(code, (trades[i], win_rates[i]), textcoords="offset points",
                     xytext=(8, 5), fontsize=10, fontweight='bold')
    ax4.set_xlabel('交易笔数', fontsize=13)
    ax4.set_ylabel('交易胜率 (%)', fontsize=13)
    ax4.set_title('交易活跃度 vs 胜率（气泡大小=交易笔数）', fontsize=18, fontweight='bold', pad=10)
    ax4.axhline(y=50, color='#999999', linestyle='--', linewidth=1, alpha=0.5)
    ax4.grid(True, alpha=0.2, linestyle='--')

    # 底部汇总文字
    summary_text = (f"共 {n_stocks} 只股票  |  "
                    f"平均收益率: {np.mean(returns):+.2f}%  |  "
                    f"模型胜率: {sum(1 for r in returns if r > 0) / n_stocks * 100:.1f}%  |  "
                    f"最佳: {best_code} ({returns[best_idx]:+.1f}%)  |  "
                    f"最差: {worst_code} ({returns[worst_idx]:+.1f}%)")
    if config_used:
        summary_text += (f"  |  初始资金: {config_used.get('initial_cash')}  |  "
                         f"佣金: {config_used.get('commission_rate')}  |  "
                         f"滑点: {config_used.get('slippage', 0)}")
    fig.text(0.5, 0.005, summary_text, ha='center', fontsize=13,
             bbox=dict(boxstyle='round,pad=0.6', facecolor='#fff8dc', edgecolor='#d4a017', linewidth=2, alpha=0.95))

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.05)
    filename = os.path.join(save_dir, f"multi_quant_summary_{n_stocks}stocks.png")
    n = 1
    while os.path.exists(filename):
        filename = os.path.join(save_dir, f"multi_quant_summary_{n_stocks}stocks_{n:03d}.png")
        n += 1
    try:
        plt.savefig(filename, dpi=800, bbox_inches='tight', facecolor='white', edgecolor='none')
        print(f"\n{GREEN}✓ 多股汇总超高清图表已保存: {filename}{RESET}")
    except Exception as e:
        alt = os.path.join(SCRIPT_DIR, "photo", os.path.basename(filename))
        os.makedirs(os.path.dirname(alt), exist_ok=True)
        plt.savefig(alt, dpi=800, bbox_inches='tight', facecolor='white', edgecolor='none')
        print(f"\n{GREEN}✓ 多股汇总超高清图表已保存（备用路径）: {alt}{RESET}")
    plt.close()
    return filename




def generate_portfolio_chart(stock_data_dict, portfolio_history, summary, config_used, save_dir):
    """
    生成组合回测资产曲线图。
    包含：组合总资产曲线、持仓市值/现金占比、每日盈亏柱状图。
    """
    ok = _setup_chinese_font()
    if not ok:
        print(f"{YELLOW}警告：未找到中文字体{RESET}")
    if save_dir is None:
        save_dir = ensure_photo_dir()

    if not portfolio_history:
        return None

    dates = [r['date'] for r in portfolio_history]
    assets = [r['total_asset'] for r in portfolio_history]
    market_vals = [r['market_value'] for r in portfolio_history]
    cashes = [r['cash'] for r in portfolio_history]
    n = len(dates)

    daily_pnl = [0]
    for i in range(1, n):
        daily_pnl.append(assets[i] - assets[i-1])

    date_labels = [d.strftime('%m-%d') if hasattr(d, 'strftime') else str(d)[:5] for d in dates]

    fig = plt.figure(figsize=(18, 16))
    gs = fig.add_gridspec(3, 1, height_ratios=[3, 1.5, 1.5], hspace=0.25)

    # 子图1: 总资产曲线
    ax1 = fig.add_subplot(gs[0])
    ax1.plot(range(n), assets, color='#0066ff', linewidth=1.5, label='组合总资产', zorder=3)
    ax1.fill_between(range(n), assets, config_used.get('initial_cash', 200000),
                     where=[a >= config_used.get('initial_cash', 200000) for a in assets],
                     color='#ffcccc', alpha=0.5, label='盈利区')
    ax1.fill_between(range(n), assets, config_used.get('initial_cash', 200000),
                     where=[a < config_used.get('initial_cash', 200000) for a in assets],
                     color='#ccffcc', alpha=0.5, label='亏损区')
    ax1.axhline(y=config_used.get('initial_cash', 200000), color='#999999', linestyle='--',
                linewidth=1, label=f'初始资金 {config_used.get("initial_cash", 200000):.0f}')

    # 标注最大回撤
    peak = assets[0]
    dd_idx = 0
    max_dd = 0
    for i in range(n):
        if assets[i] > peak:
            peak = assets[i]
        dd = (peak - assets[i]) / peak
        if dd > max_dd:
            max_dd = dd
            dd_idx = i
    if max_dd > 0:
        ax1.scatter([dd_idx], [assets[dd_idx]], color='purple', s=80, zorder=5, marker='x')
        ax1.annotate(f'最大回撤 {max_dd*100:.1f}%', (dd_idx, assets[dd_idx]),
                     textcoords="offset points", xytext=(10, 0), fontsize=10, color='purple')

    mode_name = {'single_rotation': '单股轮动', 'top_n_equal': f'Top-{config_used.get("top_n", 3)}等权',
                 'score_weighted': '得分加权'}
    mode_str = mode_name.get(config_used.get('portfolio_mode', ''), config_used.get('portfolio_mode', ''))
    ax1.set_title(f'组合回测资产曲线 — {mode_str} | 共{n}个交易日', fontsize=18, fontweight='bold', pad=12)
    ax1.set_ylabel('总资产 (元)', fontsize=13)
    ax1.legend(loc='upper left', fontsize=11)
    ax1.grid(True, alpha=0.2, linestyle='--')
    ax1.set_xlim(-1, n)
    step = max(1, n // 12)
    ax1.set_xticks(range(0, n, step))
    ax1.set_xticklabels([date_labels[i] for i in range(0, n, step)], rotation=45, ha='right', fontsize=10)

    # 子图2: 持仓市值 vs 现金余额
    ax2 = fig.add_subplot(gs[1])
    ax2.plot(range(n), market_vals, color='#ff6600', linewidth=1.2, label='持仓市值', alpha=0.9)
    ax2.plot(range(n), cashes, color='#0099cc', linewidth=1.2, label='现金余额', alpha=0.9)
    ax2.fill_between(range(n), market_vals, 0, color='#ffcc99', alpha=0.3)
    ax2.set_ylabel('金额 (元)', fontsize=13)
    ax2.legend(loc='upper left', fontsize=11)
    ax2.grid(True, alpha=0.2, linestyle='--')
    ax2.set_xlim(-1, n)
    ax2.set_xticks(range(0, n, step))
    ax2.set_xticklabels([date_labels[i] for i in range(0, n, step)], rotation=45, ha='right', fontsize=10)

    # 子图3: 每日盈亏
    ax3 = fig.add_subplot(gs[2])
    colors_bar = ['#ff4444' if p >= 0 else '#00aa00' for p in daily_pnl]
    ax3.bar(range(n), daily_pnl, color=colors_bar, width=0.8, alpha=0.7, edgecolor='none')
    ax3.axhline(y=0, color='#666666', linewidth=0.8)
    ax3.set_ylabel('当日盈亏 (元)', fontsize=13)
    ax3.set_xlabel('日期', fontsize=13)
    ax3.grid(True, alpha=0.2, linestyle='--', axis='y')
    ax3.set_xlim(-1, n)
    ax3.set_xticks(range(0, n, step))
    ax3.set_xticklabels([date_labels[i] for i in range(0, n, step)], rotation=45, ha='right', fontsize=10)

    # 底部汇总文字
    info = (f"模式: {mode_str}  |  初始: {config_used.get('initial_cash', 200000):.0f}  |  "
            f"最终: {assets[-1]:.2f}  |  盈亏: {assets[-1] - config_used.get('initial_cash', 200000):+.2f} "
            f"({((assets[-1] - config_used.get('initial_cash', 200000)) / config_used.get('initial_cash', 200000) * 100):+.2f}%)  |  "
            f"最大回撤: {summary.get('max_drawdown', 0):.2f}%  |  夏普: {summary.get('sharpe', 0):.2f}  |  "
            f"交易: {summary.get('total_trades', 0)}笔")
    fig.text(0.5, 0.005, info, ha='center', fontsize=12,
             bbox=dict(boxstyle='round,pad=0.5', facecolor='#fff8dc', edgecolor='#d4a017', linewidth=1.5, alpha=0.9))

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.06)
    filename = os.path.join(save_dir, f"portfolio_{config_used.get('portfolio_mode', 'mix')}_{n}days.png")
    n_img = 1
    while os.path.exists(filename):
        filename = os.path.join(save_dir, f"portfolio_{config_used.get('portfolio_mode', 'mix')}_{n}days_{n_img:03d}.png")
        n_img += 1
    try:
        plt.savefig(filename, dpi=800, bbox_inches='tight', facecolor='white', edgecolor='none')
        print(f"\n{GREEN}✓ 组合资产曲线图已保存: {filename}{RESET}")
    except Exception as e:
        alt = os.path.join(SCRIPT_DIR, "photo", os.path.basename(filename))
        os.makedirs(os.path.dirname(alt), exist_ok=True)
        plt.savefig(alt, dpi=800, bbox_inches='tight', facecolor='white', edgecolor='none')
        print(f"\n{GREEN}✓ 组合资产曲线图已保存（备用路径）: {alt}{RESET}")
    plt.close()
    return filename

def plot_backtest(df, account, code, save_dir=None):
    """【图表绘制接口】单股K线图+总资产曲线。"""
    return generate_chart(df, account, code, save_dir)




def clear_pycache():
    import shutil
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cleared = 0
    for root, dirs, files in os.walk(script_dir):
        for d in dirs:
            if d == '__pycache__':
                p = os.path.join(root, d)
                try:
                    shutil.rmtree(p)
                    cleared += 1
                    logging.info(f'已清理缓存目录: {p}')
                except Exception as e:
                    logging.warning(f'清理缓存目录失败 {p}: {e}')
        for f in files:
            if f.endswith('.pyc') or f.endswith('.pyo'):
                p = os.path.join(root, f)
                try:
                    os.remove(p)
                    cleared += 1
                except Exception as e:
                    logging.warning(f'清理缓存文件失败 {p}: {e}')
    if cleared:
        print(f"{CYAN}已清理 {cleared} 个缓存文件/目录{RESET}")




# ==================== 数据准备（启动页）====================
def data_preparation():
    """
    启动页：列出本地股票，提供下载入口。
    用户可输入股票代码下载数据，或直接回车进入回测。
    """
    while True:
        print(f"\n{CYAN}{'='*60}{RESET}")
        print(f"{BOLD}         📁 数据准备{RESET}")
        print(f"{CYAN}{'='*60}{RESET}")

        # 列出本地股票
        stocks = list_stocks()
        if stocks:
            print(f"{GREEN}本地已有股票数据（共 {len(stocks)} 只）：{RESET}")
            for i, code in enumerate(stocks, 1):
                print(f"  {i:3d}. {code}")
        else:
            print(f"{YELLOW}本地暂无股票数据{RESET}")

        print(f"\n{CYAN}{'='*60}{RESET}")
        print(f"{BOLD}         📥 数据下载{RESET}")
        print(f"{CYAN}{'='*60}{RESET}")
        print("  操作方式：")
        print("    a. 输入股票代码下载，如 000001 或 000001,000002")
        print("    b. 直接回车 → 进入回测")
        print(f"{CYAN}{'='*60}{RESET}")

        sel = input("请输入股票代码（回车=进入回测）：").strip()
        if not sel:
            print(f"{CYAN}>>> 进入回测流程...{RESET}")
            return True

        # 批量下载
        codes = [c.strip() for c in sel.replace(' ', '').split(',') if c.strip()]
        if not codes:
            continue

        for code in codes:
            if not code.isdigit():
                print(f"{RED}错误：{code} 不是有效的股票代码{RESET}")
                continue
            # 检查是否已存在
            path = os.path.join(DATA_DIR, f"{code}.xlsx")
            if os.path.exists(path):
                overwrite = input(f"  {code} 已存在，是否覆盖？(y/n，默认n): ").strip().lower()
                if overwrite not in ('y', 'yes', '是'):
                    print(f"  {YELLOW}跳过 {code}{RESET}")
                    continue
            ok = download_stock(code)
            if ok:
                print(f"  {GREEN}✓ {code} 下载成功{RESET}")
            else:
                print(f"  {RED}✗ {code} 下载失败{RESET}")

def main():
    clear_pycache()
    log_path = _setup_logging()
    print(f"{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}     A股量化回测引擎 v4.0{RESET}")
    print(f"{CYAN}{'='*60}{RESET}")
    logging.info('A股量化回测引擎 v4.0 启动')
    logging.info(f'日志文件: {log_path}')
    print(f"规则：T+1 | 整手({DEFAULT_CONFIG['lot_size']}股) | 佣金{DEFAULT_CONFIG['commission_rate']*10000:.0f}‰最低{DEFAULT_CONFIG['min_commission']}元")
    print(f"限制：一字涨停禁止买入 | 一字跌停禁止卖出")
    print(f"资金：{DEFAULT_CONFIG['initial_cash']} 元")
    print(f"数据：{os.path.abspath(DATA_DIR)}")
    print(f"图表：{PHOTO_DIR} (脚本目录: {SCRIPT_DIR})")
    print(f"{CYAN}{'='*60}{RESET}")

    while True:
        data_preparation()
        run_quant_mode()
        while True:
            again = input("\n是否进行下一次回测？（y/n）：").strip().lower()
            if again in ('y', 'yes', '是'):
                break
            elif again in ('n', 'no', '否'):
                print(f"\n{BOLD}感谢使用，再见！{RESET}")
                return
            else:
                print("请输入 y 或 n")

if __name__ == "__main__":
    main()
