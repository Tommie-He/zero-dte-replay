# -*- coding: utf-8 -*-
"""
demo_feed.py — Demo版离线数据源 (读取 demo_data/ 合成样例日, 完全不需要网络/API key)

给Demo版app提供与现有数据层等价的接口:
  list_days() -> [{'id','date','kind','s0',...}]
  load_underlying(day_id) -> DataFrame(1s OHLCV, tz=NY)      # 替代 hyp_lib.load_und / _load_one
  strikes(day_id, cp) -> [float]                              # 替代 hyp_lib._list_strikes
  DemoQuoteSource(day_id): now/f1/ensure/dead/shutdown        # 替代 sim_trader._QuoteBook (接口子集)
    - 合成中价序列覆盖全天每一秒 → 报价永远连续, 无空窗
"""
import os, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "demo_data")
NY = "America/New_York"
_CACHE = {}


def list_days():
    with open(os.path.join(DATA, "days.json"), encoding="utf-8") as f:
        return json.load(f)


def _day(day_id):
    if day_id in _CACHE: return _CACHE[day_id]
    d = os.path.join(DATA, day_id)
    und = pd.read_parquet(os.path.join(d, "underlying.parquet"))
    z = np.load(os.path.join(d, "options.npz"))
    entry = dict(und=und, epochs=z["epochs"].astype(np.int64),
                 strikes=z["strikes"].astype(float), C=z["C"], P=z["P"])
    _CACHE[day_id] = entry
    return entry


def load_underlying(day_id):
    return _day(day_id)["und"]


def strikes(day_id, cp=None):
    return list(_day(day_id)["strikes"])


def mid_series(day_id, cp, strike):
    """该合约全天逐秒中价 -> (epochs_ns, mids) 或 None"""
    e = _day(day_id)
    ks = e["strikes"]
    i = int(np.argmin(np.abs(ks - float(strike))))
    if abs(ks[i] - float(strike)) > 1e-6: return None
    return e["epochs"], (e["C"] if cp == "C" else e["P"])[i]


class DemoQuoteSource:
    """与 sim_trader._QuoteBook 相同的调用面(now/f1/ensure/dead/shutdown), 但离线零延迟。"""

    def __init__(self, day_id):
        self.day_id = day_id
        self.closed = False

    def shutdown(self): self.closed = True

    def dead(self, cp, sk):
        return mid_series(self.day_id, cp, sk) is None

    def ensure(self, cp, sk, T):
        return mid_series(self.day_id, cp, sk) is not None

    def now(self, cp, sk, T):
        ser = mid_series(self.day_id, cp, sk)
        if ser is None: return np.nan
        ep, mid = ser
        t = int(T.value)
        i = int(np.searchsorted(ep, t, "right")) - 1
        if i < 0: return np.nan
        return float(mid[min(i, len(mid) - 1)])

    def f1(self, cp, sk, T):
        tt = T.floor("1s") + pd.Timedelta(seconds=1)
        return self.now(cp, sk, tt), tt
