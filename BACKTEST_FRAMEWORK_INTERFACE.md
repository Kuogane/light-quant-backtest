# A股回测引擎 v4.0 —— 量化插件接口规范

> **目标读者**：第三方量化策略开发者  
> **核心设计**：统一量化入口，支持单股/多股独立回测 + 三种组合模式（单股轮动、Top-N等权、得分加权）。

---

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│         主程序 stock_backtest.py                        │
│  ├─ 数据下载 / 加载 / 清洗（含 volume/amount 等扩展列）   │
│  ├─ 手动逐日回测交互                                      │
│  ├─ 图表绘制（单股详情 / 多股汇总 / 组合资产曲线）        │
│  ├─ 技术指标计算                                          │
│  ├─ 【锁定】A股硬规则：T+1、整手、涨停禁买、跌停禁卖       │
│  ├─ 【开放】交易成本参数：佣金、滑点、初始资金等            │
│  ├─ 【v4.0】组合回测引擎：单股轮动 / Top-N等权 / 得分加权 │
│  └─ 【v4.0】延后执行：模型T日提交，T+1日开盘执行（防未来函数）│
└─────────────────────────────────────────────────────────┘
                            │
                            │ 调用接口（两个函数）
                            ▼
┌─────────────────────────────────────────────────────────┐
│      量化插件 quant_*.py (你写的)                        │
│  ├─ 策略定义（单股 decide + 组合 score）                  │
│  ├─ 自定义交易成本（策略级 default_config）                │
│  └─ 统一回测入口 run_quant_backtest                     │
│       （无论1只还是100只股票，都走这一个函数）            │
└─────────────────────────────────────────────────────────┘
```

**关键原则**：
- **统一入口**：单股量化与多股量化共用同一个 `run_quant_backtest` 接口。
- **组合模式**：主程序内置组合回测引擎，插件只需提供 `score()` 方法即可支持轮动/等权/加权。
- **防未来函数**：`delay_execution` 选项让模型订单延后一天执行。

---

## 二、文件要求

### 2.1 文件名

主程序自动识别同目录下所有以 `quant_` 开头的 `.py` 文件：

```bash
quant_engine.py      # 默认示例
quant_alpha101.py    # Alpha101策略
quant_my_v2.py      # 自定义v2版
```

**规则**：前缀固定 `quant_`，后缀自由命名。

### 2.2 最小依赖

```txt
numpy
pandas
```

---

## 三、必须实现的两个接口

### 接口 1：`get_strategies()` → dict

返回策略注册表。

```python
def get_strategies():
    return {
        "1": {
            "name": "策略显示名称",
            "desc": "策略一句话描述",
            "class": StrategyClass,
            "param_info": {                  # 可选，交互式参数
                "short": ("短期均线", int, 5),
            },
            "default_config": {              # 策略级默认配置，可选
                "initial_cash": 1000000,
                "commission_rate": 0.0002,
                "slippage": 0.001,
            }
        },
    }
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | str | 是 | 显示名称 |
| `desc` | str | 是 | 一句话描述 |
| `class` | class | 是 | 策略类本身 |
| `param_info` | dict | 否 | 交互参数定义 |
| `default_config` | dict | 否 | **策略级默认交易成本配置** |

---

### 接口 2：`run_quant_backtest(stock_data_dict, strategy_id, strategy_params, config)` → dict

**统一回测入口。无论 stock_data_dict 中有1只股票还是100只，都走这个接口。**

#### 参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `stock_data_dict` | `dict[str, DataFrame]` | `{ "000001": df, ... }`，每份 df 必含 `date/open/high/low/close`，**可选含 `volume/amount/pct_change/change/pre_close`** |
| `strategy_id` | str | 用户在 `get_strategies()` 中选中的 key |
| `strategy_params` | dict | 用户填写的策略参数 |
| `config` | dict | **合并后的回测配置** |

**`config` 完整结构**：

```python
{
    "initial_cash": 200000,       # 每只股票初始资金（独立模式）或组合总资金（组合模式）
    "commission_rate": 0.0001,   # 佣金率
    "min_commission": 5,          # 最低佣金
    "lot_size": 100,              # 每手股数（A股硬规则）
    "slippage": 0.0,              # 滑点比例
    "risk_free_rate": 0.03,       # 年化无风险利率
    "limit_up_pct": 0.095,        # 涨停判定阈值
    "limit_down_pct": 0.095,      # 跌停判定阈值
    "delay_execution": False,     # 【v4.0】延后一天执行
    "portfolio_mode": "independent", # 【v4.0】组合模式
    "top_n": 3,                   # 【v4.0】Top-N 的 N 值
}
```

#### 返回值

```python
{
    "summary": { ... },            # 汇总指标
    "stock_results": {            # 单股结果字典
        "000001": {
            "metrics": { ... },    # 单股技术指标
            "trades": [ ... ],    # 交易记录
            "daily_history": [ ... ],
            "final_asset": 250000.0,
            "total_return_pct": 25.0,
        },
    },
    "config_used": { ... },       # 实际使用的配置
}
```

---

## 四、策略类规范

### 4.1 类属性

```python
class MyStrategy:
    name = "策略名"
    desc = "策略描述"
    param_info = {}
    default_config = {}   # 策略级默认交易成本
```

### 4.2 `__init__` 签名

```python
def __init__(self, account, df, **kwargs):
    self.account = account      # Account 对象
    self.df = df                # DataFrame
    self.params = kwargs        # 策略参数字典
```

### 4.3 `decide` 方法（单股逐日决策）

```python
def decide(self, i, row, open_p) -> (str, int):
    """
    每日开盘前决策。
    参数:
        i: int       当前行索引
        row: Series  当日数据（date/open/high/low/close/volume/amount...）
        open_p: float 开盘价
    返回:
        (action, amount)
        action: "buy" | "sell" | "hold"
        amount: int  交易股数（必须是 100 的整数倍）
    """
    return "hold", 0
```

### 4.4 `score` 方法（【v4.0】组合模式专用）

```python
def score(self, code, row, day_idx) -> float:
    """
    每日对单只股票打分。
    参数:
        code: str    股票代码
        row: Series  当日数据
        day_idx: int 当前交易日索引
    返回:
        float  得分（正值看多，负值看空，0中性）
    说明:
        主程序组合回测引擎会调用此方法对多只股票排序/加权。
        如果不实现，默认返回 0.0（不参与组合排序）。
    """
    return 0.0
```

**重要**：`score()` 和 `decide()` 可以共享内部计算逻辑。建议把核心因子计算提取为独立方法，两者共用。

---

## 五、组合模式详解

### 5.1 独立回测（默认）

每只股票独立账户，互不影响。插件 `run_quant_backtest` 内部逐只执行 `_run_single_backtest`，返回每只股票的独立结果。

### 5.2 单股轮动

统一账户，每次只持仓 **1 只** 得分最高的股票。切换时先卖后买（T+1 制度下，卖出资金当日不可用，实际次日才能买入新股票）。

**插件要求**：实现 `score()` 方法返回每只股票的得分。

### 5.3 Top-N 等权

统一账户，每天选得分最高的 **N 只** 股票，每只目标仓位 = 总资产 / N。

**插件要求**：实现 `score()` 方法。

### 5.4 得分加权

统一账户，按因子得分比例分配权重。只取 **正得分** 的股票参与加权。

**插件要求**：实现 `score()` 方法，得分差异越大，权重差异越大。

---

## 六、A股硬规则 vs 开放配置

### 6.1 【锁定】A股硬规则

| 规则 | 说明 | 插件能否修改 |
|------|------|-------------|
| **T+1 制度** | 当日买入次日可卖 | ❌ 不能 |
| **整手交易** | 买卖数量必须是 `lot_size`（100股）的整数倍 | ❌ 不能 |
| **涨停禁止买入** | 一字涨停日，`buy()` 被拦截 | ❌ 不能 |
| **跌停禁止卖出** | 一字跌停日，`sell()` 被拦截 | ❌ 不能 |

### 6.2 【开放】交易成本参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `initial_cash` | 200000 | 初始资金 |
| `commission_rate` | 0.0001 | 佣金率 |
| `min_commission` | 5 | 最低佣金 |
| `slippage` | 0.0 | 滑点比例 |
| `risk_free_rate` | 0.03 | 年化无风险利率 |
| `delay_execution` | False | **延后一天执行** |
| `portfolio_mode` | "independent" | **组合模式** |
| `top_n` | 3 | **Top-N 数量** |

**覆盖优先级**：
```
主程序全局默认 < 用户手动修改 < 策略级 default_config < 插件内部动态修改
```

---

## 七、延后执行机制

### 7.1 为什么需要延后执行？

防止模型在 `decide()` 中使用了当日收盘后的信息（如 `row['close']`）做决策，然后当天开盘成交，形成**未来函数**。

### 7.2 机制

- **关闭**（默认）：策略第 T 天返回 `buy`/`sell` → 当日开盘立即成交
- **开启**：策略第 T 天返回 `buy`/`sell` → 存入 `pending_orders` → 第 T+1 天开盘前 `execute_pending()` 用次日开盘价执行

### 7.3 对插件的影响

**无影响**。插件只需正常实现 `decide()`，返回买卖信号即可。延后执行由主程序引擎层处理。

---

## 八、完整最小示例

```python
# quant_minimal.py
import numpy as np
import pandas as pd

try:
    from stock_backtest import Account, calc_metrics, is_limit_up, is_limit_down
    HAS_MAIN = True
except Exception:
    HAS_MAIN = False

class StrategyBuyHold:
    name = "买入持有"
    desc = "第1天全仓买入"
    param_info = {}
    default_config = {'commission_rate': 0.00015, 'slippage': 0.001}

    def __init__(self, account=None, df=None, **kwargs):
        self.account = account
        self.df = df
        self.params = kwargs

    def _max_buy(self, price, ratio=1.0):
        lots = int(self.account.cash * ratio // price // 100) if price > 0 else 0
        return lots * 100

    def decide(self, i, row, open_p):
        if i == 0 and self.account.hold_total == 0:
            return 'buy', self._max_buy(open_p, 0.95)
        return 'hold', 0

    def score(self, code, row, day_idx):
        # 组合模式示例：首日看多
        if day_idx == 0:
            return 1.0
        return 0.0

def _run_single(df, strategy_cls, params, config):
    account = Account(config.get('initial_cash', 200000), config)
    strategy = strategy_cls(account, df, **params)
    n = len(df)
    for i in range(n):
        row = df.iloc[i]
        price = float(row['close'])
        open_p = float(row['open'])
        high_p = float(row['high'])
        low_p = float(row['low'])
        account.daily_thaw()
        prev_close = float(df.iloc[i-1]['close']) if i > 0 else None
        is_yz = is_limit_up(open_p, high_p, low_p, price, prev_close, config)
        is_dt = is_limit_down(open_p, high_p, low_p, price, prev_close, config)
        action, amount = strategy.decide(i, row, open_p)
        if action == 'buy' and amount > 0 and not is_yz:
            account.buy(open_p, amount, row['date'], i)
        elif action == 'sell' and amount > 0 and not is_dt:
            account.sell(open_p, amount, row['date'], i)
        account.record_day(row['date'], price)
    metrics = calc_metrics(account, df, 0) if HAS_MAIN else {}
    return {
        'metrics': metrics,
        'trades': account.trades,
        'daily_history': account.daily_history,
        'final_asset': account.cash + account.hold_total * float(df.iloc[-1]['close']),
        'total_return_pct': metrics.get('total_return', 0),
    }

def run_quant_backtest(stock_data_dict, strategy_id, strategy_params, config):
    strategy_cls = StrategyBuyHold
    cfg = dict(config)
    cfg.update(StrategyBuyHold.default_config)
    stock_results = {}
    for code, df in stock_data_dict.items():
        stock_results[code] = _run_single(df, strategy_cls, strategy_params, cfg)
    returns = [r['metrics'].get('total_return', 0) for r in stock_results.values() if r.get('metrics')]
    win = sum(1 for r in returns if r > 0)
    summary = {
        'model_win_rate': win / len(returns) if returns else 0,
        'avg_total_return': np.mean(returns) if returns else 0,
        'avg_max_drawdown': 0,
        'avg_sharpe': 0,
        'total_trades': sum(r['metrics'].get('total_trades', 0) for r in stock_results.values()),
        'win_stock_count': win,
        'loss_stock_count': sum(1 for r in returns if r < 0),
        'avg_trades_per_stock': 0,
        'best_stock': None,
        'worst_stock': None,
    }
    return {'summary': summary, 'stock_results': stock_results, 'config_used': cfg}

def get_strategies():
    return {
        "1": {
            "name": StrategyBuyHold.name,
            "desc": StrategyBuyHold.desc,
            "class": StrategyBuyHold,
            "param_info": StrategyBuyHold.param_info,
            "default_config": StrategyBuyHold.default_config,
        }
    }
```

---

## 九、版本历史

| 版本 | 日期 | 说明 |
|------|------|------|
| v3.0 | 2026-05-14 | 插件化架构，多股回测 |
| v3.1 | 2026-05-15 | 开放配置：佣金/滑点/初始资金可由插件定义 |
| v3.2 | 2026-05-15 | 统一入口：单股/多股合并为 `run_quant_backtest` |
| v3.3 | 2026-05-15 | 统一量化：单股/多股共用同一模型；延后执行防未来函数 |
| **v4.0** | **2026-05-15** | **组合模式：单股轮动 / Top-N等权 / 得分加权；`score()` 接口；日志系统；递归清理缓存** |

---

*本文档由 A股模拟交易回测引擎 v4.0 自动生成。*
