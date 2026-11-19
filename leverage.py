#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""杠杆资金监测：融资余额 ÷ A 股流通市值，融资买入额 ÷ 成交额。

两个指标：
    杠杆率%       = 沪深融资余额 ÷ 沪深 A 股流通市值 × 100
    融资买入占比% = 近 5 日融资买入额合计 ÷ 近 5 日成交额合计 × 100

数据（东方财富 融资融券历史合计，日频、沪深合计）：
    RZYE   融资余额（元）
    RZMRE  融资买入额（元）
    LTSZ   沪深 A 股流通市值（元）
    RZYEZB 融资余额占流通市值比（%），用于自校验

为什么用流通市值：
    总市值包含大量暂不流通的股份，会系统性稀释杠杆率。融资盘能买到的只有流通股，
    所以 融资余额 ÷ 流通市值 才能跨年份比较，也和常见的 2% / 3% / 4% 经验区间对齐。

    早期方案用沪深交易所月末官方流通市值做锚点、月内按指数衔接，在 2015 年 6 月
    这类剧烈波动的月份误差可达数个百分点；改为日频流通市值后不再需要插值。

用法：
    python leverage.py                 # 抓数 + 打印摘要 + 生成 web/leverage.html
    python leverage.py --no-fetch      # 只用本地缓存
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

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
WEB_DIR = BASE / "web"
for _d in (DATA_DIR, WEB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DAILY_FILE = DATA_DIR / "leverage_daily.csv"
TEMPLATE = WEB_DIR / "leverage_template.html"
OUTPUT = WEB_DIR / "leverage.html"

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_REPORT = "RPTA_RZRQ_LSHJ"
EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

WEEK_N = 5


# --------------------------------------------------------------------------- #
# 抓数
# --------------------------------------------------------------------------- #
def fetch_daily(quiet: bool = False) -> pd.DataFrame:
    """抓取沪深融资融券日频合计（亿元），全历史。

    元 → 亿元，只保留 date / balance / buy / circ。
    """
    rows: list[dict] = []
    page = 1
    pages = 1
    while page <= pages:
        params = {
            "reportName": EM_REPORT,
            "columns": "DIM_DATE,RZYE,RZMRE,LTSZ,RZYEZB",
            "source": "WEB",
            "sortColumns": "DIM_DATE",
            "sortTypes": "-1",
            "pageNumber": str(page),
            "pageSize": "500",
        }
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.get(EM_URL, params=params, headers=EM_HEADERS, timeout=30)
                res = resp.json().get("result") or {}
                rows += res.get("data") or []
                pages = int(res.get("pages") or 1)
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(1.5 * (attempt + 1))
        if last_err is not None:
            raise RuntimeError(f"融资融券第 {page} 页抓取失败: {last_err}")
        if not quiet:
            print(f"\r[杠杆] 融资融券 {page}/{pages} 页", end="", flush=True)
        page += 1
    if not quiet:
        print()

    if not rows:
        raise RuntimeError("融资融券接口返回空数据")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["DIM_DATE"], errors="coerce")
    for src, dst, scale in (("RZYE", "balance", 1e8), ("RZMRE", "buy", 1e8),
                            ("LTSZ", "circ", 1e8)):
        df[dst] = pd.to_numeric(df[src], errors="coerce") / scale
    df["ratio_src"] = pd.to_numeric(df.get("RZYEZB"), errors="coerce")

    df = (df[["date", "balance", "buy", "circ", "ratio_src"]]
          .dropna(subset=["date"])
          .sort_values("date")
          .drop_duplicates("date", keep="last")
          .reset_index(drop=True))
    # 0 值和负值都是接口异常，置空
    for c in ("balance", "buy", "circ"):
        df.loc[df[c] <= 0, c] = np.nan
    df = df.dropna(subset=["balance", "circ"]).reset_index(drop=True)

    df.to_csv(DAILY_FILE, index=False, date_format="%Y-%m-%d")
    if not quiet:
        print(f"[杠杆] 融资融券 {len(df)} 个交易日，"
              f"{df['date'].min():%Y-%m-%d} → {df['date'].max():%Y-%m-%d}")
    return df


def load_daily() -> pd.DataFrame:
    if not DAILY_FILE.exists():
        raise RuntimeError(f"缺少缓存 {DAILY_FILE.name}，请先联网运行一次")
    df = pd.read_csv(DAILY_FILE, parse_dates=["date"])
    for c in ("balance", "buy", "circ"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
def build(daily: pd.DataFrame, lev: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """把成交额与两融对齐到交易日，生成日频指标。

    daily: 中证全指日频（date / close / amount）
    lev:   融资融券日频（date / balance / buy / circ）
    """
    start = pd.Timestamp(cfg["leverage"].get("start", "2014-01-01"))
    d = (daily.loc[daily["date"] >= start, ["date", "close", "amount"]]
         .sort_values("date").reset_index(drop=True))

    d = d.merge(lev[["date", "balance", "buy", "circ"]], on="date", how="left")
    # 两融偶尔比行情晚一天：余额是存量，前向填充合理；买入额是流量，不填
    d["balance"] = pd.to_numeric(d["balance"], errors="coerce").ffill()
    d["circ"] = pd.to_numeric(d["circ"], errors="coerce").ffill()

    d["ratio"] = d["balance"] / d["circ"] * 100.0
    d["buy_share"] = d["buy"] / d["amount"] * 100.0
    d["buy_sum"] = d["buy"].rolling(WEEK_N, min_periods=WEEK_N).sum()
    d["amount_sum"] = d["amount"].rolling(WEEK_N, min_periods=WEEK_N).sum()
    d["buy_share5"] = d["buy_sum"] / d["amount_sum"] * 100.0
    d["buy_ma20"] = d["buy_share"].rolling(20, min_periods=20).mean()
    return d.dropna(subset=["circ", "ratio"]).reset_index(drop=True)


def pick_band(ratio: float, bands: list[dict]) -> dict:
    return next(b for b in bands if ratio < b["max"])


def weekly_rows(d: pd.DataFrame, cfg: dict) -> list[dict]:
    x = d.copy()
    x["week"] = x["date"].dt.to_period("W-FRI")
    ends = x.groupby("week").tail(1).dropna(subset=["ratio", "buy_share5"])
    rows = ends.tail(cfg["leverage"].get("recent_weeks", 12))
    out = []
    for r in rows.itertuples():
        band = pick_band(float(r.ratio), cfg["leverage"]["bands"])
        out.append({
            "date": f"{r.date:%Y-%m-%d}",
            "balance": round(float(r.balance), 0),
            "ratio": round(float(r.ratio), 2),
            "band": band["label"],
            "buy_share": round(float(r.buy_share5), 2),
        })
    return out


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #
def make_payload(d: pd.DataFrame, cfg: dict) -> dict:
    val = d.dropna(subset=["ratio", "buy_share5"])
    if val.empty:
        raise RuntimeError("杠杆数据为空")
    last = val.iloc[-1]

    x = d.copy()
    x["week"] = x["date"].dt.to_period("W-FRI")
    ends = x.groupby("week").tail(1).dropna(subset=["ratio", "buy_share5"])
    cur = ends.iloc[-1]
    prev = ends.iloc[-2] if len(ends) > 1 else cur

    ratio_chg = float(cur["ratio"] - prev["ratio"])
    balance_chg = float((cur["balance"] / prev["balance"] - 1) * 100) if prev["balance"] else 0.0
    buy_chg = float(cur["buy_share5"] - prev["buy_share5"])

    ratio_pct = float((val["ratio"] <= cur["ratio"]).mean() * 100)
    buy_pct = float((val["buy_share5"] <= cur["buy_share5"]).mean() * 100)

    band = pick_band(float(cur["ratio"]), cfg["leverage"]["bands"])
    bands = cfg["leverage"]["bands"]
    ymax = max(5.2, float(val["ratio"].quantile(0.995)) + 0.3)

    return {
        "asof": f'{last["date"]:%Y-%m-%d}',
        "updated": f'{datetime.now():%Y-%m-%d %H:%M}',
        "stats": {
            "balance": round(float(cur["balance"]), 0),
            "balance_wan": round(float(cur["balance"]) / 10000, 2),
            "balance_chg_pct": round(balance_chg, 1),
            "ratio": round(float(cur["ratio"]), 2),
            "ratio_chg": round(ratio_chg, 2),
            "ratio_pct": round(ratio_pct, 1),
            "band": band["label"],
            "band_range": band.get("range", ""),
            "band_note": band.get("note", ""),
            "color": band["color"],
            "bg": f'{band["color"]}14',
            "border": f'{band["color"]}55',
            "buy_share": round(float(cur["buy_share5"]), 2),
            "buy_share_chg": round(buy_chg, 2),
            "buy_share_pct": round(buy_pct, 1),
            "circ_wan": round(float(cur["circ"]) / 10000, 1),
            "ymax": round(ymax, 1),
        },
        "series": {
            "dates": [f'{v:%Y-%m-%d}' for v in d["date"]],
            "ratio": [round(v, 2) for v in d["ratio"]],
            "balance": [round(v, 0) for v in d["balance"]],
            "buy_share": [None if pd.isna(v) else round(v, 2) for v in d["buy_share"]],
            "buy_ma20": [None if pd.isna(v) else round(v, 2) for v in d["buy_ma20"]],
            "circ": [round(v / 10000, 1) for v in d["circ"]],
        },
        "log": weekly_rows(d, cfg),
        "bands": bands,
    }


def render(payload: dict) -> Path:
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"缺少模板: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT


def run(daily: pd.DataFrame, cfg: dict, no_fetch: bool = False,
        quiet: bool = False) -> dict:
    lev = load_daily() if no_fetch else fetch_daily(quiet=quiet)
    df = build(daily, lev, cfg)
    payload = make_payload(df, cfg)
    if not quiet:
        st = payload["stats"]
        print(f'[杠杆] {payload["asof"]} 融资余额 {st["balance_wan"]:.2f} 万亿 | '
              f'杠杆率 {st["ratio"]:.2f}% ({st["band"]}) | '
              f'融资买入占比 {st["buy_share"]:.2f}%')
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="杠杆资金监测")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    daily = pd.read_csv(BASE / "data" / "turnover_daily.csv", parse_dates=["date"])
    try:
        payload = run(daily, cfg, no_fetch=args.no_fetch)
        print(f"网页: {render(payload)}")
    except Exception as exc:  # noqa: BLE001
        print(f"生成失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
