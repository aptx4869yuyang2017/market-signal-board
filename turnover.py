#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两市成交额观测（标准化口径：日换手率 = 成交额 / 两市总市值）

为什么不用原始成交额：
    总市值从 2014 年约 37 万亿增至现在约 100 万亿，成交额会被动抬升，
    直接看绝对值和历史分位会系统性偏高。用「成交额 ÷ 总市值」剔除市值膨胀，
    再看换手率的历史分位，才能跨年份比较冷热。

数据：
    成交额、指数点位：中证指数官网 index-perf（中证全指 000985，覆盖 A 股主要标的）
    两市总市值：沪深交易所月度市价总值（akshare macro_china_stock_market_cap），
                日频用中证全指涨跌在月度锚点之间衔接

用法：
    python turnover.py                 # 抓数 + 计算 + 生成 web/turnover.html
                                       # 并调用 leverage.py 生成 web/leverage.html
    python turnover.py --no-fetch      # 只用本地缓存
    python turnover.py --quiet
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yaml

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
WEB_DIR = BASE / "web"
for _d in (DATA_DIR, WEB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CSI_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
DAILY_FILE = DATA_DIR / "turnover_daily.csv"
CAP_FILE = DATA_DIR / "market_cap_monthly.csv"
TEMPLATE = WEB_DIR / "turnover_template.html"
OUTPUT = WEB_DIR / "turnover.html"

TIER_NOTE = {
    "冷清": "成交低迷，情绪处在冰点",
    "偏冷": "交投偏淡，观望情绪较重",
    "正常": "活跃度中性，没有明显拥挤",
    "偏热": "交投升温，注意交易拥挤",
    "过热": "情绪过热，留意回撤风险",
}

# 周报 / 推送用：蓝 = 冷，黄 = 正常，橙红 = 热
TIER_BADGE = {
    "冷清": "🔵",
    "偏冷": "🟦",
    "正常": "🟨",
    "偏热": "🟧",
    "过热": "🟥",
}


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #
def _headers() -> dict:
    return {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            "Referer": "https://www.csindex.com.cn/"}


def fetch_daily(retries: int = 3) -> pd.DataFrame:
    """中证全指：收盘点位 + 日成交额（亿元）。"""
    params = {"indexCode": "000985", "startDate": "20131201",
              "endDate": datetime.now().strftime("%Y%m%d")}
    last_err = None
    for i in range(retries):
        try:
            resp = requests.get(CSI_URL, params=params, timeout=60, headers=_headers())
            resp.raise_for_status()
            data = resp.json().get("data") or []
            if not data:
                raise RuntimeError("中证全指接口返回空数据")
            df = pd.DataFrame(data)[["tradeDate", "close", "tradingValue"]]
            df.columns = ["date", "close", "amount"]
            df["date"] = pd.to_datetime(df["date"])
            for c in ("close", "amount"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = (df.dropna(subset=["close", "amount"])
                    .loc[lambda x: (x["close"] > 0) & (x["amount"] > 0)]
                    .sort_values("date")
                    .drop_duplicates("date", keep="last").reset_index(drop=True))
            if df.empty:
                raise RuntimeError("中证全指接口返回空数据")
            return df
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < retries - 1:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"获取成交额失败: {last_err}")


def fetch_cap(retries: int = 2) -> pd.DataFrame:
    """沪深两市月末市价总值（亿元）。"""
    import akshare as ak

    last_err = None
    for i in range(retries):
        try:
            raw = ak.macro_china_stock_market_cap()
            df = raw.dropna(subset=["市价总值-上海", "市价总值-深圳"]).copy()
            parts = df["数据日期"].astype(str).str.extract(r"(\d{4})年(\d{1,2})月")
            df["month"] = pd.to_datetime(parts[0] + "-" + parts[1] + "-01")
            df["cap"] = (pd.to_numeric(df["市价总值-上海"], errors="coerce")
                         + pd.to_numeric(df["市价总值-深圳"], errors="coerce"))
            out = (df[["month", "cap"]].dropna().sort_values("month")
                     .drop_duplicates("month", keep="last").reset_index(drop=True))
            if out.empty:
                raise RuntimeError("月度市值返回空数据")
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < retries - 1:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"获取两市总市值失败: {last_err}")


def build(daily: pd.DataFrame, cap: pd.DataFrame) -> pd.DataFrame:
    """把月度总市值锚点用指数涨跌衔接成日频，再算换手率。"""
    df = daily.sort_values("date").reset_index(drop=True).copy()
    df["ym"] = df["date"].dt.to_period("M")

    cap_map = {p: float(v) for p, v in zip(cap["month"].dt.to_period("M"), cap["cap"])}
    month_end = df.groupby("ym")["date"].max().to_dict()
    month_end_dates = set(month_end.values())

    caps: list[float] = []
    anchor_cap = None
    anchor_close = None
    for row in df.itertuples():
        if anchor_cap is None:
            prev = row.ym - 1
            if prev in cap_map and prev in month_end:
                anchor_cap = cap_map[prev]
                anchor_close = float(df.loc[df["date"] == month_end[prev], "close"].iloc[-1])
        if anchor_cap is None:
            caps.append(float("nan"))
            continue
        cap_t = anchor_cap * float(row.close) / anchor_close
        # 月末用官方市价总值校准，并把锚点滚到下个月
        if row.date in month_end_dates and row.ym in cap_map:
            cap_t = cap_map[row.ym]
            anchor_cap, anchor_close = cap_map[row.ym], float(row.close)
        caps.append(cap_t)

    df["cap"] = caps
    df["turn"] = df["amount"] / df["cap"] * 100.0
    df = df.dropna(subset=["cap", "turn"])
    # 节假日偶发 0 成交额，直接剔除，避免污染分位与坐标轴
    df = df.loc[df["amount"] > 0]
    return df.loc[df["date"] >= "2014-01-01"].reset_index(drop=True)


def pct_rank(sample: pd.DataFrame, col: str) -> float:
    """样本内低于（或等于）当前值的交易日占比，越高越热。"""
    return float((sample[col] <= sample[col].iloc[-1]).mean() * 100.0)


def pick_tier(pct: float, tiers: list[dict]) -> dict:
    return next(t for t in tiers if pct <= t["max"])


WEEK_N = 5


def week_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """逐日计算滚动周度指标。

    一周 = 最近 5 个交易日；
    周度换手率 = 5 日成交额合计 ÷ 5 日总市值合计 × 100；
    amount / cap 为对应的 5 日均值。
    """
    d = df.sort_values("date").reset_index(drop=True).copy()
    d["amount_sum"] = d["amount"].rolling(WEEK_N, min_periods=WEEK_N).sum()
    d["cap_sum"] = d["cap"].rolling(WEEK_N, min_periods=WEEK_N).sum()
    d["turn"] = d["amount_sum"] / d["cap_sum"] * 100.0
    d["amount"] = d["amount_sum"] / WEEK_N
    d["cap"] = d["cap_sum"] / WEEK_N
    return d


def weekly_rows(df: pd.DataFrame, cfg: dict) -> list[dict]:
    """近 N 周台账：每行取当周最后一个交易日的滚动周度指标。

    周度换手率 = 最近 5 个交易日成交额合计 ÷ 同 5 个交易日总市值合计；
    amount 为同期日均成交额；
    分位 = 924 以来各周周度换手率中低于（或等于）当前值的占比。
    """
    d = week_metrics(df)
    d["week"] = d["date"].dt.to_period("W-FRI")
    ends = d.groupby("week").tail(1).dropna(subset=["turn"])

    start = pd.Timestamp(cfg["turnover"]["pct_start"])
    sample = ends.loc[ends["date"] >= start]
    if sample.empty:
        sample = ends
    turn_sample = sample["turn"].to_numpy()

    rows = ends.tail(cfg["turnover"]["recent_weeks"])
    out = []
    for r in rows.itertuples():
        p = float((turn_sample <= r.turn).mean() * 100)
        out.append({
            "date": f"{r.date:%Y-%m-%d}",
            "amount": round(float(r.amount), 1),
            "turn": round(float(r.turn), 2),
            "pct": round(p, 1),
            "tier": pick_tier(p, cfg["turnover"]["tiers"])["label"],
        })
    return out


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
def make_payload(df: pd.DataFrame, cfg: dict) -> dict:
    last = df.iloc[-1]
    d = week_metrics(df)
    d["week"] = d["date"].dt.to_period("W-FRI")
    ends = d.groupby("week").tail(1).dropna(subset=["turn"])
    cur = ends.iloc[-1]
    prev = ends.iloc[-2] if len(ends) > 1 else None
    amount_chg = ((cur["amount"] / prev["amount"] - 1) * 100) if prev is not None else 0.0
    turn_chg = (cur["turn"] - prev["turn"]) if prev is not None else 0.0

    # 分位固定用 2024-09-24（924 政策转向）之后的样本。
    # 924 之后两市成交额从 0.5–1 万亿抬升到 1.5–3.5 万亿的平台，
    # 混用之前的样本会把当前读数系统性压低/抬高，失去可比性。
    start = pd.Timestamp(cfg["turnover"]["pct_start"])
    sample = df.loc[df["date"] >= start].reset_index(drop=True)
    if len(sample) < 30:
        raise RuntimeError(f"924 以来样本不足（{len(sample)} 个交易日）")
    week_sample = ends.loc[ends["date"] >= start]
    if len(week_sample) < 4:
        raise RuntimeError(f"924 以来周样本不足（{len(week_sample)} 周）")

    # 本周 = 最近 5 个交易日；周度换手率 = 5 日成交额合计 ÷ 5 日总市值合计
    week_amount = float(cur["amount"])
    week_turn = float(cur["turn"])

    # 在 924 以来的周度序列里算分位
    week_amount_pct = float((week_sample["amount"] <= week_amount).mean() * 100)
    week_turn_pct = float((week_sample["turn"] <= week_turn).mean() * 100)

    # 单日分位（保留作参考）
    daily_amount_pct = pct_rank(sample, "amount")
    daily_turn_pct = pct_rank(sample, "turn")

    # 综合判断跟随「周度换手率分位」
    tier = pick_tier(week_turn_pct, cfg["turnover"]["tiers"])
    regime = {
        "start": f"{start:%Y-%m-%d}",
        "days": int(len(sample)),
        "weeks": int(len(week_sample)),
        "turn_pct": round(week_turn_pct, 1),
        "amount_pct": round(week_amount_pct, 1),
        "daily_turn_pct": round(daily_turn_pct, 1),
        "daily_amount_pct": round(daily_amount_pct, 1),
        "tier": tier["label"],
        "note": TIER_NOTE.get(tier["label"], ""),
        "color": tier["color"],
        "bg": f'{tier["color"]}14',
        "border": f'{tier["color"]}55',
    }

    return {
        "asof": f'{last["date"]:%Y-%m-%d}',
        "updated": f'{datetime.now():%Y-%m-%d %H:%M}',
        "stats": {
            "amount": round(float(last["amount"]), 1),
            "amount_chg_pct": round(float(amount_chg), 1),
            "turn": round(float(last["turn"]), 2),
            "turn_chg": round(float(turn_chg), 2),
            "cap_wan": round(float(cur["cap"]) / 10000, 1),
            "close": round(float(last["close"]), 2),
            # 本周（5 日均成交额 / 周度换手率）
            "week_amount": round(week_amount, 1),
            "week_amount_pct": round(week_amount_pct, 1),
            "week_turn": round(week_turn, 2),
            "week_turn_pct": round(week_turn_pct, 1),
            # 冷热阈值线取 924 以来周度换手率的 20% / 80% 分位
            "cold": round(float(week_sample["turn"].quantile(0.20)), 2),
            "hot": round(float(week_sample["turn"].quantile(0.80)), 2),
        },
        "regime": regime,
        "series": {
            "dates": [f'{d:%Y-%m-%d}' for d in df["date"]],
            "amount": [round(v, 1) for v in df["amount"]],
            "turn": [round(v, 2) for v in df["turn"]],
            "cap": [round(v / 10000, 1) for v in df["cap"]],
            "close": [round(v, 2) for v in df["close"]],
        },
        "log": weekly_rows(df, cfg),
        "tiers": cfg["turnover"]["tiers"],
    }


def _amount_text(amount_yi: float) -> str:
    """成交额（亿元）→ 万亿/亿 可读文本。"""
    return f"{amount_yi / 10000:.2f} 万亿" if amount_yi >= 10000 else f"{amount_yi:.0f} 亿"


def summary_md(payload: dict) -> str:
    """周报 / 推送用的成交额摘要（Markdown，飞书卡片可直接渲染）。"""
    st, rg = payload["stats"], payload["regime"]
    badge = TIER_BADGE.get(rg["tier"], "")
    da, dt = st.get("amount_chg_pct"), st.get("turn_chg")

    threshold_line = f'（冷/热阈值 {st["cold"]:.2f}% / {st["hot"]:.2f}%）'
    if dt is not None:
        bp = round(dt * 100)
        txt = f"换手率 {bp:+d}bp" if bp else "换手率 0bp"
        color = "orange" if bp > 0 else "blue" if bp < 0 else None
        threshold_line += f" ｜<font color='{color}'>{txt}</font>" if color else f" ｜{txt}"
    amount_pct_line = f'分位 {st["week_amount_pct"]:.1f}%'
    if da is not None:
        amount_pct_line += f' | 较上周 {da:+.1f}%'

    lines = [
        "**两市成交额**",
        "",
        f'{badge} 成交量 {rg["tier"]} ｜ {rg["note"]}',
        f'**本周换手率 {st["week_turn"]:.2f}%  分位 {st["week_turn_pct"]:.1f}%**',
        threshold_line,
        "",
        f'本周成交额（日均） {_amount_text(st["week_amount"])}',
        amount_pct_line,
    ]
    return "\n".join(lines)


def render(payload: dict) -> Path:
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"缺少模板: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    OUTPUT.write_text(html, encoding="utf-8")
    return OUTPUT


def run(cfg: dict | None = None, no_fetch: bool = False, quiet: bool = False,
        render_page: bool = True) -> dict:
    if cfg is None:
        with open(BASE / "config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    if no_fetch:
        daily = pd.read_csv(DAILY_FILE, parse_dates=["date"])
        cap = pd.read_csv(CAP_FILE, parse_dates=["month"])
    else:
        daily = fetch_daily()
        daily.to_csv(DAILY_FILE, index=False, date_format="%Y-%m-%d")
        cap = fetch_cap()
        cap.to_csv(CAP_FILE, index=False, date_format="%Y-%m-%d")

    df = build(daily, cap)
    payload = make_payload(df, cfg)

    # 杠杆资金独立成页，抓取失败不影响成交额页
    lv_payload = None
    lv_path = None
    try:
        import leverage
        lv_payload = leverage.run(df, cfg, no_fetch=no_fetch, quiet=True)
        lv_path = leverage.render(lv_payload) if render_page else None
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"[杠杆] 跳过：{exc}", file=sys.stderr)

    path = render(payload) if render_page else None

    st = payload["stats"]
    if not quiet:
        print(f'[成交额] {payload["asof"]} 本周成交额（日均） {st["week_amount"]:.0f} 亿 | '
              f'本周换手率 {st["week_turn"]:.2f}% | 924以来分位 {payload["regime"]["turn_pct"]:.1f}% | '
              f'{payload["regime"]["tier"]}')
        if lv_payload:
            ls = lv_payload["stats"]
            print(f'[杠杆] {lv_payload["asof"]} 融资余额 {ls["balance_wan"]:.2f} 万亿 | '
                  f'杠杆率 {ls["ratio"]:.2f}% ({ls["band"]}) | '
                  f'融资买入占比 {ls["buy_share"]:.2f}%')
        if path:
            print(f"网页: {path}")
        if lv_path:
            print(f"杠杆页: {lv_path}")
    return {"payload": payload, "path": path,
            "leverage": lv_payload, "leverage_path": lv_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="两市成交额观测（换手率标准化）")
    parser.add_argument("--no-fetch", action="store_true", help="只用本地缓存")
    parser.add_argument("--quiet", action="store_true", help="不打印摘要")
    args = parser.parse_args()
    try:
        run(no_fetch=args.no_fetch, quiet=args.quiet)
    except Exception as exc:  # noqa: BLE001
        print(f"生成失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
