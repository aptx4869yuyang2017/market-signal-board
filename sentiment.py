#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向情绪监控：机构子弹 / 杠杆情绪 / 开户情绪 / 北向活跃度。

四项指标统一换算成 0–100 的「市场温度」：越高越热（过热/贪婪），越低越冷（冰点/悲观）：
    fund.score    = 100 − 样本主动权益基金现金比例分位   （现金越多越冷）
    margin.score  = 融资余额 20 日变化率分位              （去杠杆越猛越冷）
    account.score = 新增开户数分位                        （开户越少越冷）
    north.score   = 北向相对成交活跃度分位                （相对近期自身萎缩越冷，已去长期增长趋势）

低温（0–40）是左侧分批建仓窗口，高温（80–100）以防守和再平衡为主。

数据源：
    基金现金      天天基金 F10 资产配置明细（季度，25 只主动权益基金加权）
    股票仓位      乐咕 fund_stock_position_lg（周度估算，仅辅助展示）
    融资余额      复用 data/leverage_daily.csv（东方财富两融日频）
    新增开户      上交所上证统计月报 commonQuery.do（月度）
    北向成交额    东方财富 RPT_MUTUAL_DEAL_HISTORY（日频，仅活跃度）

注意：2024-08-19 起官方不再披露北向每日净买入，本脚本只抓成交总额，
不重建净流向序列。详见 docs/pages/sentiment.md。

用法：
    python sentiment.py                 # 抓数 + 生成 web/sentiment.html
    python sentiment.py --no-fetch      # 只用本地 CSV 缓存重算
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
WEB_DIR = BASE / "web"
for _d in (DATA_DIR, WEB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

FUND_FILE = DATA_DIR / "fund_cash_quarterly.csv"
POS_FILE = DATA_DIR / "fund_position_weekly.csv"
ACCOUNT_FILE = DATA_DIR / "account_monthly.csv"
NORTH_FILE = DATA_DIR / "northbound_daily.csv"
LEV_FILE = DATA_DIR / "leverage_daily.csv"
# 中证全指日频点位：由 turnover.py 抓取维护，这里只读复用，不重复请求同一接口
INDEX_FILE = DATA_DIR / "turnover_daily.csv"
TEMPLATE = WEB_DIR / "sentiment_template.html"
OUTPUT = WEB_DIR / "sentiment.html"

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


# --------------------------------------------------------------------------- #
# 抓数
# --------------------------------------------------------------------------- #
def _get_with_retry(url: str, *, params: dict | None = None, headers: dict | None = None,
                    tries: int = 3, timeout: int = 25) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def fetch_fund_cash(cfg: dict, quiet: bool = False) -> pd.DataFrame:
    """逐只基金抓 F10 资产配置明细，输出长表。"""
    rows: list[dict] = []
    codes = cfg["sentiment"]["fund_codes"]
    for n, code in enumerate(codes, 1):
        url = f"https://fundf10.eastmoney.com/zcpz_{code}.html"
        text = ""
        for attempt in range(3):
            try:
                text = _get_with_retry(url, tries=1, timeout=20).text
                table = BeautifulSoup(text, "lxml").select_one("table.tzxq")
                if table and table.select("tbody tr"):
                    break
            except Exception:  # noqa: BLE001
                pass
            text = ""
            time.sleep(1.2 * (attempt + 1))
        if not text:
            if not quiet:
                print(f"[反向] 基金 {code} 抓取失败，跳过", file=sys.stderr)
            continue

        table = BeautifulSoup(text, "lxml").select_one("table.tzxq")
        for tr in table.select("tbody tr"):
            td = [x.get_text(strip=True) for x in tr.select("td")]
            if len(td) < 5 or not td[0].startswith("20"):
                continue

            def num(v: str) -> float:
                return float(v.replace("%", "").replace(",", "")) if v not in ("", "---", "--") else np.nan

            rows.append({
                "date": td[0], "fund_code": code,
                "cash": num(td[3]), "net_assets": num(td[4]),
                "stock": num(td[1]), "bond": num(td[2]),
            })
        time.sleep(0.7)  # 东财限流，别把样本打掉
        if not quiet and n % 5 == 0:
            print(f"\r[反向] 基金资产配置 {n}/{len(codes)}", end="", flush=True)
    if not quiet:
        print()

    if not rows:
        raise RuntimeError("基金资产配置没有抓到任何数据")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("cash", "net_assets", "stock", "bond"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    start = pd.Timestamp(cfg.get("start_date", "2014-01-01"))
    df = (df.dropna(subset=["date"])
          .loc[lambda x: x["date"] >= start]
          .sort_values(["date", "fund_code"])
          .drop_duplicates(["date", "fund_code"], keep="last")
          .reset_index(drop=True))
    df.to_csv(FUND_FILE, index=False, date_format="%Y-%m-%d")
    if not quiet:
        print(f"[反向] 基金现金样本 {df.fund_code.nunique()} 只 / {df.date.nunique()} 个季度，"
              f"截至 {df.date.max():%Y-%m-%d}")
    return df


def fetch_fund_position(quiet: bool = False) -> pd.DataFrame:
    """乐咕全样本基金股票仓位估算（周度），仅辅助展示。"""
    import akshare as ak

    raw = ak.fund_stock_position_lg()
    df = raw[["date", "position"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["position"] = pd.to_numeric(df["position"], errors="coerce")
    df = df.dropna(subset=["date", "position"]).sort_values("date").reset_index(drop=True)
    df.to_csv(POS_FILE, index=False, date_format="%Y-%m-%d")
    return df


def fetch_accounts(quiet: bool = False) -> pd.DataFrame:
    """上交所月报：A 股个人新开户数（万户），2021-12 起。"""
    headers = dict(UA)
    headers["Referer"] = "https://www.sse.com.cn/aboutus/publication/monthly/investor"
    year_now = datetime.now().year
    rows: list[dict] = []
    for year in range(2021, year_now + 1):
        # MDATE=YYYYMM 返回当年 1 月至该月的累计表；未来月份返回空，
        # 因此从 12 月往前找第一个有数据的月份，拿到当年最新完整月报。
        result = []
        for month in range(12, 0, -1):
            params = {
                "sqlId": "COMMON_SSE_TZZ_M_STOCK_ACCT_C",
                "isPagination": "false",
                "MDATE": f"{year}{month:02d}",
            }
            result = _get_with_retry(SSE_URL, params=params, headers=headers).json().get("result") or []
            if result:
                break
        for item in result:
            term = str(item.get("TERM") or "")
            if "." not in term:
                continue
            y, m = term.split(".", 1)
            try:
                value = float(item.get("A_PERSON"))
            except (TypeError, ValueError):
                continue
            # 未来月份官方表里是 0.0000，不是真实开户数
            if value <= 0:
                continue
            rows.append({"date": f"{int(y):04d}-{int(m):02d}", "new_a": value})
        if not quiet:
            print(f"\r[反向] 上交所开户月报 {year}", end="", flush=True)
    if not quiet:
        print()

    if not rows:
        raise RuntimeError("上交所开户数据为空")
    df = (pd.DataFrame(rows).drop_duplicates("date", keep="last")
          .sort_values("date").reset_index(drop=True))
    df.to_csv(ACCOUNT_FILE, index=False)
    return df


def fetch_northbound(quiet: bool = False) -> pd.DataFrame:
    """东方财富北向每日成交总额（沪股通 + 深股通），换算为亿元。"""
    params = {
        "reportName": "RPT_MUTUAL_DEAL_HISTORY",
        "columns": "MUTUAL_TYPE,TRADE_DATE,DEAL_AMT",
        "source": "WEB",
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
        "pageSize": "500",
        "filter": '(TRADE_DATE>=\'2018-01-01\')(MUTUAL_TYPE in ("001","003"))',
    }
    rows: list[dict] = []
    page, pages = 1, 1
    while page <= pages:
        p = dict(params, pageNumber=str(page))
        res = _get_with_retry(EM_URL, params=p).json().get("result") or {}
        rows += res.get("data") or []
        pages = int(res.get("pages") or 1)
        if not quiet:
            print(f"\r[反向] 北向成交额 {page}/{pages} 页", end="", flush=True)
        page += 1
    if not quiet:
        print()

    if not rows:
        raise RuntimeError("北向成交额数据为空")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["TRADE_DATE"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["deal"] = pd.to_numeric(df["DEAL_AMT"], errors="coerce") / 100.0  # 百万元 → 亿元
    df = (df.dropna(subset=["date", "deal"])
          .loc[lambda x: x["deal"] > 0]
          .groupby("date", as_index=False)["deal"].sum()
          .sort_values("date").reset_index(drop=True))
    df.to_csv(NORTH_FILE, index=False)
    return df


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
def _load(path: Path, date_col: str = "date") -> pd.DataFrame:
    if not path.exists():
        raise RuntimeError(f"缺少缓存 {path.name}，请先联网运行一次 python sentiment.py")
    return pd.read_csv(path, parse_dates=[date_col] if date_col == "date" else False)


def _load_index() -> pd.DataFrame:
    """中证全指点位缓存缺失时返回空表，页面只隐藏辅助曲线、不影响四项指标。"""
    if not INDEX_FILE.exists():
        return pd.DataFrame(columns=["date", "close"])
    return _load(INDEX_FILE)


def load_all() -> dict:
    return {
        "fund": _load(FUND_FILE),
        "pos": _load(POS_FILE),
        "account": _load(ACCOUNT_FILE, date_col=""),
        "north": _load(NORTH_FILE, date_col=""),
        "lev": _load(LEV_FILE),
        "idx": _load_index(),
    }


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
def pct_rank(values: pd.Series, value: float, years: float = 0) -> float:
    v = pd.to_numeric(values, errors="coerce").dropna()
    if years:
        end = pd.Timestamp(v.index.max()) if isinstance(v.index, pd.DatetimeIndex) else None
        if end is not None:
            v = v[v.index >= end - pd.Timedelta(days=365.25 * years)]
    if v.empty:
        return 50.0
    return round(float((v <= value).mean() * 100), 1)


def pick_tier(value: float, tiers: list[dict]) -> dict:
    return next((t for t in tiers if value < t["max"]), tiers[-1])


def rolling_pct(s: pd.Series, years: float = 0) -> pd.Series:
    """逐点算「截至当日」的历史分位，不做未来函数。"""
    x = pd.to_numeric(s, errors="coerce").dropna().sort_index()
    if x.empty:
        return x
    idx, vals = x.index, x.to_numpy(dtype=float)
    out = np.empty(len(vals), dtype=float)
    for i, v in enumerate(vals):
        start = 0
        if years:
            cut = idx[i] - pd.Timedelta(days=365.25 * years)
            start = int(np.searchsorted(idx, cut, side="left"))
        hist = vals[start:i + 1]
        out[i] = (hist <= v).mean() * 100 if hist.size else 50.0
    return pd.Series(out, index=idx)


def temperature_history(funds: pd.DataFrame, margin: pd.DataFrame,
                        acct: pd.DataFrame, north: pd.DataFrame, cfg: dict) -> dict:
    """四项温度的历史序列，等权平均成整体温度（0 冰点 / 100 过热）。

    以融资余额日频为基准轴；基金（季度）、开户（月度）前向填充到当日，
    表示「当时最新公布值」。各项分位只用当日及以前的数据，不含未来函数。
    起点取开户数据满一年后，避免 5 年分位窗口样本过少导致曲线乱跳。
    """
    sc = cfg["sentiment"]
    base = margin.set_index("date")["chg20"].sort_index()
    first = acct["date"].min() + pd.DateOffset(months=12)
    base = base[base.index >= first]
    empty = {"dates": [], "score": [], "fund": [], "margin": [], "account": [], "north": []}
    if base.empty:
        return empty

    f = rolling_pct(funds.set_index("date")["cash"], sc.get("fund_pct_years", 10))
    m = rolling_pct(margin.set_index("date")["chg20"])
    a = rolling_pct(acct.set_index("date")["new_a"], sc.get("account_pct_years", 5))
    n = rolling_pct(north.set_index("date")["rel_activity"].dropna(), sc.get("north_pct_years", 5))

    def align(s: pd.Series) -> pd.Series:
        return s.reindex(s.index.union(base.index)).ffill().reindex(base.index)

    # 温度口径：现金分位高=子弹足=冷；融资跌幅大/开户少/北向冷清=冷
    # 所以 fund 取 100−分位，其余三项直接取分位（分位高=热）
    df = pd.DataFrame({
        "fund": 100 - align(f),
        "margin": align(m),
        "account": align(a),
        "north": align(n),
    }).dropna()
    if df.empty:
        return empty

    score = df.mean(axis=1)
    return {
        "dates": [f"{d:%Y-%m-%d}" for d in score.index],
        "score": [round(float(v), 1) for v in score],
        "fund": [round(float(v), 1) for v in df["fund"]],
        "margin": [round(float(v), 1) for v in df["margin"]],
        "account": [round(float(v), 1) for v in df["account"]],
        "north": [round(float(v), 1) for v in df["north"]],
    }


def _colors(tier: dict) -> dict:
    return {
        "color": tier["color"],
        "bg": tier["color"] + "14",
        "border": tier["color"] + "55",
    }


def fund_quarterly(raw: pd.DataFrame) -> pd.DataFrame:
    """按季度做净资产加权，输出 date / cash / funds。"""
    x = raw.dropna(subset=["cash", "net_assets"]).copy()
    x = x.loc[x["net_assets"] > 0]
    x = x.groupby("date").apply(
        lambda g: pd.Series({
            "cash": float(np.average(g["cash"], weights=g["net_assets"])),
            "funds": int(len(g)),
        }), include_groups=False
    ).reset_index()
    x = x.loc[x["funds"] >= 5].sort_values("date").reset_index(drop=True)
    x["cash"] = x["cash"].round(2)
    return x


def margin_daily(lev: pd.DataFrame) -> pd.DataFrame:
    x = lev.sort_values("date").copy()
    x["chg20"] = (x["balance"] / x["balance"].shift(20) - 1) * 100
    x["drawdown"] = (x["balance"] / x["balance"].rolling(252, min_periods=120).max() - 1) * 100
    x = x.loc[x["date"] >= "2015-01-01"].dropna(subset=["chg20"]).reset_index(drop=True)
    x["chg20"] = x["chg20"].round(2)
    x["drawdown"] = x["drawdown"].round(2)
    return x


def account_monthly(raw: pd.DataFrame) -> pd.DataFrame:
    x = raw.copy()
    x["date"] = pd.to_datetime(x["date"], format="%Y-%m")
    x["new_a"] = pd.to_numeric(x["new_a"], errors="coerce")
    return x.dropna(subset=["new_a"]).sort_values("date").reset_index(drop=True)


def north_daily(raw: pd.DataFrame, trend_days: int = 250) -> pd.DataFrame:
    x = raw.copy()
    x["date"] = pd.to_datetime(x["date"], format="%Y-%m-%d")
    x["deal"] = pd.to_numeric(x["deal"], errors="coerce")
    x["deal_ma20"] = x["deal"].rolling(20, min_periods=20).mean()
    x["deal_ma5"] = x["deal"].rolling(5, min_periods=5).mean()
    # 相对活跃度：短期均额 / 长期均额。北向成交额长期增长（2018 年 205 亿 → 2026 年 3187 亿），
    # 直接对绝对额取滚动分位会让读数长期贴在 80+，失去区分度。除以长期均线后再取分位，
    # 衡量的是「相对近期自己是否放大/萎缩」，与情绪高低的对应关系更稳定。
    x["deal_ma_trend"] = x["deal"].rolling(trend_days, min_periods=trend_days).mean()
    x["rel_activity"] = x["deal_ma20"] / x["deal_ma_trend"]
    return x.dropna(subset=["deal_ma20"]).sort_values("date").reset_index(drop=True)


def north_bands(rel: pd.Series, tiers: list[dict]) -> list[dict]:
    """把五档温度阈值映射到相对活跃度的全样本分位边界，供折线图铺背景带。

    例如 score_tiers 的 max=20 表示「极端悲观」上界，对应 rel 的 20% 分位；
    这样背景带与温度档位用同一套配色和占比，图上读数高低与卡片档位一致。
    """
    x = pd.to_numeric(rel, errors="coerce").dropna()
    if x.empty:
        return []
    bands = []
    prev = 0.0
    for tier in tiers:
        hi = float(x.quantile(tier["max"] / 100)) if tier["max"] < 100 else float(x.max())
        bands.append({
            "lo": round(prev, 3),
            "hi": round(hi, 3),
            "color": tier["color"],
            "label": tier["label"],
        })
        prev = hi
    return bands


def make_payload(data: dict, cfg: dict) -> dict:
    sc = cfg["sentiment"]
    funds = fund_quarterly(data["fund"])
    margin = margin_daily(data["lev"])
    acct = account_monthly(data["account"])
    north = north_daily(data["north"], trend_days=int(sc.get("north_trend_days", 250)))
    if funds.empty or margin.empty or acct.empty or north.empty:
        raise RuntimeError("四项指标中存在空序列，无法生成反向情绪页")

    # ---- 机构子弹：基金现金 ----
    f_cur, f_prev = funds.iloc[-1], funds.iloc[-2] if len(funds) > 1 else funds.iloc[-1]
    f_series = funds.set_index("date")["cash"]
    f_pct = pct_rank(f_series, float(f_cur["cash"]), sc.get("fund_pct_years", 10))

    pos = data["pos"].sort_values("date")
    pos_last = pos.iloc[-1] if not pos.empty else None

    # ---- 杠杆情绪：融资余额 20 日变化 ----
    m_cur = margin.iloc[-1]
    m_ser = margin.set_index("date")["chg20"]
    m_pct = pct_rank(m_ser, float(m_cur["chg20"]))
    m_tier = pick_tier(float(m_cur["chg20"]), sc["margin_tiers"])

    # ---- 开户情绪 ----
    a_cur, a_prev = acct.iloc[-1], acct.iloc[-2] if len(acct) > 1 else acct.iloc[-1]
    a_year = acct.iloc[-13] if len(acct) > 12 else acct.iloc[0]
    a_ser = acct.set_index("date")["new_a"]
    a_pct = pct_rank(a_ser, float(a_cur["new_a"]), sc.get("account_pct_years", 5))
    mom = (a_cur["new_a"] / a_prev["new_a"] - 1) * 100 if a_prev["new_a"] else 0.0
    yoy = (a_cur["new_a"] / a_year["new_a"] - 1) * 100 if a_year["new_a"] else 0.0

    # ---- 北向活跃度 ----
    n_cur = north.iloc[-1]
    n_ser = north.set_index("date")["rel_activity"].dropna()
    n_pct = pct_rank(n_ser, float(n_cur["rel_activity"]), sc.get("north_pct_years", 5))

    # 温度口径（0 冰点 / 100 过热），恰好是旧「悲观分」的镜像：
    # 基金现金分位高=子弹足=市场冷 → 100−分位；其余三项分位高=情绪热 → 直接用分位
    scores = {
        "fund": round(100 - f_pct, 1),
        "margin": round(m_pct, 1),
        "account": round(a_pct, 1),
        "north": round(n_pct, 1),
    }
    composite = round(float(np.mean(list(scores.values()))), 1)
    c_tier = pick_tier(composite, sc["score_tiers"])

    def score_meta(score: float) -> dict:
        t = pick_tier(score, sc["score_tiers"])
        return {
            "score_tier": t["label"],
            "score_note": t["note"],
            "score_color": t["color"],
            "score_bg": t["color"] + "14",
            "score_border": t["color"] + "55",
        }

    stats = {
        "fund": {
            **score_meta(scores["fund"]),
            "asof": f'{f_cur["date"]:%Y-%m-%d}',
            "cash": round(float(f_cur["cash"]), 2),
            "cash_prev": round(float(f_prev["cash"]), 2),
            "cash_chg": round(float(f_cur["cash"] - f_prev["cash"]), 2),
            "cash_pct": f_pct,
            "funds": int(f_cur["funds"]),
            "stock_pos": round(float(pos_last["position"]), 2) if pos_last is not None else None,
            "stock_pos_asof": f'{pos_last["date"]:%Y-%m-%d}' if pos_last is not None else "",
            "stock_pos_pct": round(float(pos_last["position"]), 2) if pos_last is not None else None,
            "score": scores["fund"],
        },
        "margin": {
            **score_meta(scores["margin"]),
            "asof": f'{m_cur["date"]:%Y-%m-%d}',
            "balance_wan": round(float(m_cur["balance"]) / 10000, 2),
            "chg20": round(float(m_cur["chg20"]), 2),
            "chg20_pct": m_pct,
            "drawdown": round(float(m_cur["drawdown"]), 2),
            "tier": m_tier["label"],
            "note": m_tier["note"],
            "color": m_tier["color"],
            "score": scores["margin"],
        },
        "account": {
            **score_meta(scores["account"]),
            "asof": f'{a_cur["date"]:%Y-%m}',
            "new_a": round(float(a_cur["new_a"]), 2),
            "mom": round(float(mom), 1),
            "yoy": round(float(yoy), 1),
            "pct": a_pct,
            "score": scores["account"],
        },
        "north": {
            **score_meta(scores["north"]),
            "asof": f'{n_cur["date"]:%Y-%m-%d}',
            "deal_ma20": round(float(n_cur["deal_ma20"]), 0),
            "deal_ma5": round(float(n_cur["deal_ma5"]), 0),
            "rel_activity": round(float(n_cur["rel_activity"]), 3),
            "deal_pct": n_pct,
            "score": scores["north"],
            "net_disclosed": False,
        },
    }

    # ---- 辅助曲线：中证全指日频点位（只读 turnover.py 的缓存，不参与任何评分）----
    idx = data.get("idx")
    if idx is None or idx.empty or "close" not in idx.columns:
        bench = {"dates": [], "close": []}
    else:
        b = idx.dropna(subset=["date", "close"]).copy()
        b["date"] = pd.to_datetime(b["date"])
        b["close"] = pd.to_numeric(b["close"], errors="coerce")
        b = b.loc[b["close"] > 0].sort_values("date").drop_duplicates("date", keep="last")
        bench = {
            "dates": [f"{d:%Y-%m-%d}" for d in b["date"]],
            "close": [round(float(v), 2) for v in b["close"]],
        }

    # 末端四项与顶部卡片严格对齐：北向比两融晚一个交易日，历史序列轴止于两融最新日，
    # 直接用当期分位覆盖最后一点，避免图尾与卡片差 0.x 分。
    temp = temperature_history(funds, margin, acct, north, cfg)
    if temp["score"]:
        for k in ("fund", "margin", "account", "north"):
            temp[k][-1] = scores[k]
        temp["score"][-1] = composite

    notes = [
        f'机构子弹：{stats["fund"]["funds"]} 只主动权益基金 {stats["fund"]["asof"]} '
        f'现金占净值比 {stats["fund"]["cash"]:.2f}%，环比 {stats["fund"]["cash_chg"]:+.2f}pp，'
        f'历史分位 {f_pct:.1f}%（现金越高，子弹越足）。',
        f'杠杆情绪：融资余额近 20 日 {stats["margin"]["chg20"]:+.2f}%，'
        f'距近一年高点 {stats["margin"]["drawdown"]:.2f}%，档位「{m_tier["label"]}」。'
        '快速偿还往往伴随恐慌，但也可能来自政策或季节性因素。',
        f'开户情绪：{stats["account"]["asof"]} A 股个人新开户 '
        f'{stats["account"]["new_a"]:.2f} 万户，环比 {mom:+.1f}%、同比 {yoy:+.1f}%，'
        f'2021-12 以来分位 {a_pct:.1f}%。',
        f'北向资金：2024-08-19 起官方不再披露每日净买入，本页只跟踪沪股通 + 深股通'
        f'成交总额作为活跃度代理，不能据此判断外资流入或流出。当前相对活跃度 '
        f'{stats["north"]["rel_activity"]:.3f}（20 日均 ÷ 250 日均，低于 1 为萎缩），'
        f'分位 {n_pct:.1f}%。',
    ]

    return {
        "asof": max(stats["margin"]["asof"], stats["north"]["asof"]),
        "updated": f"{datetime.now():%Y-%m-%d %H:%M}",
        "composite": {"score": composite, "tier": c_tier["label"], **c_tier, **_colors(c_tier)},
        "tiers": [{"max": t["max"], "label": t["label"], "color": t["color"]} for t in sc["score_tiers"]],
        "stats": stats,
        "series": {
            "benchmark": bench,
            "temperature": temp,
            "fund": {
                "dates": [f"{d:%Y-%m-%d}" for d in funds["date"]],
                "cash": [float(v) for v in funds["cash"]],
            },
            "margin": {
                "dates": [f"{d:%Y-%m-%d}" for d in margin["date"]],
                "chg20": [float(v) for v in margin["chg20"]],
                "drawdown": [None if pd.isna(v) else float(v) for v in margin["drawdown"]],
            },
            "account": {
                "dates": [f"{d:%Y-%m}" for d in acct["date"]],
                "new_a": [float(v) for v in acct["new_a"]],
            },
            "north": {
                "dates": [f"{d:%Y-%m-%d}" for d in north["date"]],
                "deal_ma20": [round(float(v), 0) for v in north["deal_ma20"]],
                "rel_activity": [None if pd.isna(v) else round(float(v), 3)
                                 for v in north["rel_activity"]],
                # 背景带：把配置里的五档温度阈值映射到相对活跃度的全样本分位边界
                "bands": north_bands(north["rel_activity"], sc["score_tiers"]),
            },
        },
        "notes": notes,
    }


def render(payload: dict) -> Path:
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"缺少模板: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT


def run(cfg: dict, no_fetch: bool = False, quiet: bool = False,
        render_page: bool = True) -> tuple[dict, Path | None]:
    if no_fetch:
        data = load_all()
    else:
        try:
            data = {
                "fund": fetch_fund_cash(cfg, quiet=quiet),
                "pos": fetch_fund_position(quiet=quiet),
                "account": fetch_accounts(quiet=quiet),
                "north": fetch_northbound(quiet=quiet),
                "lev": _load(LEV_FILE),
                "idx": _load_index(),
            }
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                print(f"[warn] 反向情绪抓数失败，改用本地缓存: {exc}", file=sys.stderr)
            data = load_all()
    payload = make_payload(data, cfg)
    path = render(payload) if render_page else None
    if not quiet:
        s = payload["stats"]
        print(f'[反向] 温度 {payload["composite"]["score"]:.1f} '
              f'({payload["composite"]["tier"]}) | 基金现金 {s["fund"]["cash"]:.2f}% '
              f'| 融资20日 {s["margin"]["chg20"]:+.2f}% ({s["margin"]["tier"]}) '
              f'| 开户 {s["account"]["new_a"]:.1f}万户 | 北向成交 {s["north"]["deal_ma20"]:.0f}亿')
    return payload, path


def main() -> int:
    parser = argparse.ArgumentParser(description="反向情绪监控")
    parser.add_argument("--no-fetch", action="store_true", help="只用本地缓存重算")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    try:
        _, path = run(cfg, no_fetch=args.no_fetch, quiet=args.quiet)
        print(f"网页: {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"生成失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
