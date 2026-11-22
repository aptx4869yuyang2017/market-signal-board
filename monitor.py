#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股债性价比周度监控（多指数，主口径 ERP，单位 bp）

定义：
    股权收益率 = 1 / 指数市盈率(PE)              （%）
    无风险利率 = 十年期国债收益率                 （%）
    ERP        = 股权收益率 − 无风险利率          （bp，1bp = 0.01%）

用法：
    python monitor.py                # 抓数 + 计算 + 周报 + 网页 + 推送
    python monitor.py --no-fetch     # 只用本地缓存重算
    python monitor.py --no-push      # 不推送
    python monitor.py --quiet        # 不打印摘要
"""
from __future__ import annotations

import argparse
import json
import math
import os
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
REPORT_DIR = BASE / "reports"
CACHE_DIR = BASE / "cache"
WEB_DIR = BASE / "web"
for _d in (DATA_DIR, REPORT_DIR, CACHE_DIR, WEB_DIR):
    _d.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("MPLCONFIGDIR", str(CACHE_DIR / "mpl"))

CSI_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
BOND_FILE = DATA_DIR / "bond_10y.csv"
LOG_FILE = DATA_DIR / "weekly_log.csv"
WEB_TEMPLATE = WEB_DIR / "template.html"
WEB_INDEX = WEB_DIR / "index.html"

TIER_BADGE = {"极端便宜": "🟩", "偏便宜": "🟢", "中性": "🟨", "偏贵": "🟧", "贵": "🟥"}
VERDICT_NOTE = {
    "双确认 · 便宜且估值配合": "便宜且估值配合，可以加。",
    "单边便宜 · 相对债券便宜，绝对估值未跟上": "相对债券便宜，但绝对估值没跟上，别急着加。",
    "双确认 · 偏贵": "两个口径都偏贵，注意防守。",
    "债券更有吸引力": "股票端吸引力不如债券。",
    "中性 · 两个口径都不极端": "两个口径都不极端，维持现有配置。",
}
VERDICT_SHORT = {
    "双确认 · 便宜且估值配合": "估值也便宜，可以加",
    "单边便宜 · 相对债券便宜，绝对估值未跟上": "只有债券端在动，别急着加",
    "双确认 · 偏贵": "两个口径都偏贵",
    "债券更有吸引力": "股票不如债券",
    "中性 · 两个口径都不极端": "维持现有配置",
}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_env() -> None:
    """加载项目根目录的 .env（已存在的环境变量优先），用于存放敏感凭据。"""
    env_file = BASE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def expand_env(node):
    """递归展开配置里的 ${VAR} 占位符，取值自环境变量。"""
    if isinstance(node, str):
        return _ENV_RE.sub(lambda m: os.getenv(m.group(1), ""), node)
    if isinstance(node, dict):
        return {k: expand_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [expand_env(x) for x in node]
    return node


def load_config() -> dict:
    load_env()
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        return expand_env(yaml.safe_load(f))


def pe_file(code: str) -> Path:
    return DATA_DIR / f"pe_{code}.csv"


# --------------------------------------------------------------------------- #
# 数据抓取
# --------------------------------------------------------------------------- #
def fetch_pe(cfg: dict, code: str, retries: int = 3) -> pd.DataFrame:
    """中证指数官网：日线收盘价 + 市盈率(PE)。"""
    params = {
        "indexCode": code,
        "startDate": pd.Timestamp(cfg["start_date"]).strftime("%Y%m%d"),
        "endDate": datetime.now().strftime("%Y%m%d"),
    }
    last_err = None
    for i in range(retries):
        try:
            resp = requests.get(
                CSI_URL, params=params, timeout=60,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                         "Referer": "https://www.csindex.com.cn/"},
            )
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("data"):
                raise RuntimeError(f"{code} 接口返回空数据")
            df = pd.DataFrame(payload["data"])
            df = df[["tradeDate", "close", cfg.get("pe_field", "peg")]]
            df.columns = ["date", "close", "pe"]
            df["date"] = pd.to_datetime(df["date"])
            for c in ("close", "pe"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = (df.dropna(subset=["pe", "close"]).sort_values("date")
                    .drop_duplicates("date", keep="last").reset_index(drop=True))
            if df.empty:
                raise RuntimeError(f"{code} 接口返回空数据")
            return df
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < retries - 1:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"获取 {code} 数据失败: {last_err}")


def fetch_pe_cyb50(cfg: dict, code: str = "399673", retries: int = 3) -> pd.DataFrame:
    """创业板50（399673）日频 PE 推算。

    该指数属国证/深证体系，中证 index-perf 无数据。乐咕乐股只给月频 PE 锚点，
    腾讯日线给日频收盘价；两者收盘价同口径，故用
        PE(t) = 月频PE(最近锚点) × 收盘价(t) / 收盘价(锚点日)
    把月频 PE 插值为日频。锚点日推算值与原始月频 PE 完全一致（校验偏差 0）。
    PE 口径为整体法静态市盈率（lyrPe），与中证 peg 字段口径接近。
    """
    import akshare as ak
    import akshare.stock_feature.stock_a_pe_and_pb as lg_mod
    from datetime import datetime as _dt

    last_err = None
    for i in range(retries):
        try:
            # 日频收盘价（腾讯源，避开东财限流）
            px = ak.stock_zh_index_daily_tx(symbol=f"sz{code}")
            px["date"] = pd.to_datetime(px["date"])
            px = (px[["date", "close"]].drop_duplicates("date")
                    .sort_values("date").reset_index(drop=True))

            # 乐咕月频 PE 锚点（整体法静态市盈率 lyrPe）
            js = lg_mod.py_mini_racer.MiniRacer()
            js.eval(lg_mod.hash_code)
            token = js.call("hex", _dt.now().date().isoformat()).lower()
            resp = requests.get(
                "https://legulegu.com/api/stockdata/index-basic-pe",
                params={"token": token, "indexCode": f"{code}.SZ"},
                **lg_mod.get_cookie_csrf(url="https://legulegu.com/stockdata/sz50-ttm-lyr"),
                timeout=60,
            )
            resp.raise_for_status()
            anchors = pd.DataFrame(resp.json()["data"])
            anchors["date"] = pd.to_datetime(anchors["date"])
            anchors = (anchors[["date", "close", "lyrPe"]]
                       .rename(columns={"close": "anchor_close", "lyrPe": "anchor_pe"})
                       .dropna(subset=["anchor_pe"])
                       .sort_values("date").reset_index(drop=True))
            if anchors.empty:
                raise RuntimeError(f"{code} 乐咕 PE 锚点为空")

            merged = pd.merge_asof(px, anchors, on="date", direction="backward")
            merged["pe"] = merged["anchor_pe"] * merged["close"] / merged["anchor_close"]
            df = (merged[["date", "close", "pe"]].dropna(subset=["pe"]))
            df["pe"] = df["pe"].round(2)
            start = pd.Timestamp(cfg["start_date"])
            df = df[df["date"] >= start].sort_values("date").reset_index(drop=True)
            if df.empty:
                raise RuntimeError(f"{code} 推算结果为空")
            return df
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < retries - 1:
                time.sleep(3 * (i + 1))
    raise RuntimeError(f"获取 {code}（创业板50）数据失败: {last_err}")


def fetch_bond(cfg: dict) -> pd.DataFrame:
    import akshare as ak  # 延迟导入，--no-fetch 时无需依赖

    start = pd.Timestamp(cfg["start_date"]).strftime("%Y%m%d")
    df = ak.bond_zh_us_rate(start_date=start)
    df = df[["日期", "中国国债收益率10年"]].copy()
    df.columns = ["date", "y10"]
    df["date"] = pd.to_datetime(df["date"])
    df["y10"] = pd.to_numeric(df["y10"], errors="coerce")
    return (df.dropna(subset=["y10"]).sort_values("date")
              .drop_duplicates("date", keep="last").reset_index(drop=True))


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
def build_frame(pe: pd.DataFrame, bond: pd.DataFrame) -> pd.DataFrame:
    df = pd.merge_asof(pe.sort_values("date"), bond.sort_values("date"), on="date",
                       direction="backward", tolerance=pd.Timedelta(days=7)).dropna(subset=["y10"])
    df = df.set_index("date").sort_index()
    df["ey_pct"] = 100.0 / df["pe"]
    df["erp_bp"] = (df["ey_pct"] - df["y10"]) * 100
    return df


def cheapness_pct(df: pd.DataFrame, years: int, asof: pd.Timestamp | None = None,
                  col: str = "erp_bp") -> float:
    asof = asof if asof is not None else df.index[-1]
    sub = df.loc[(df.index >= asof - pd.DateOffset(years=years)) & (df.index <= asof), col]
    return float((sub > sub.iloc[-1]).mean() * 100.0)


def pick_tier(pct: float, cfg: dict) -> dict:
    return next(t for t in cfg["signal"]["tiers"] if pct <= t["max"])


def verdict_for(p_erp: float, p_ey: float, cfg: dict) -> tuple[str, bool]:
    ey_ok = p_ey <= cfg["signal"]["confirm_ey_pct"]
    if p_erp <= 40 and ey_ok:
        return "双确认 · 便宜且估值配合", True
    if p_erp <= 40:
        return "单边便宜 · 相对债券便宜，绝对估值未跟上", False
    if p_erp >= 60 and not ey_ok:
        return "双确认 · 偏贵", True
    if p_erp >= 60:
        return "债券更有吸引力", True
    return "中性 · 两个口径都不极端", True


def line_values(df: pd.DataFrame) -> tuple[float, float]:
    """参考线：ERP 的 80% / 20% 分位（对应「极端便宜」与「偏贵」的分界）。"""
    return float(df["erp_bp"].quantile(0.80)), float(df["erp_bp"].quantile(0.20))


def describe(df: pd.DataFrame, cfg: dict, bond_date: pd.Timestamp) -> dict:
    last = df.iloc[-1]
    years = cfg["report"]["window_years"]
    pct_erp = {y: cheapness_pct(df, y, col="erp_bp") for y in years}
    pct_ey = {y: cheapness_pct(df, y, col="ey_pct") for y in years}
    p10 = pct_erp.get(10, pct_erp[max(pct_erp)])
    info = {
        "date": df.index[-1],
        "bond_date": bond_date,
        "n": len(df),
        "erp_bp": float(last["erp_bp"]),
        "erp_pct": float(last["erp_bp"]) / 100.0,
        "pe": float(last["pe"]),
        "ey": float(last["ey_pct"]),
        "y10": float(last["y10"]),
        "close": float(last["close"]),
        "pct_erp": pct_erp,
        "pct_ey": pct_ey,
        "change_1w": None,
        "tier": pick_tier(p10, cfg),
        "pct_erp_10": p10,
        "erp_min": float(df["erp_bp"].min()),
        "erp_max": float(df["erp_bp"].max()),
    }
    prev = df.loc[df.index <= df.index[-1] - pd.Timedelta(days=5)]
    if len(df) > 6 and not prev.empty:
        info["change_1w"] = float(last["erp_bp"] - prev["erp_bp"].iloc[-1])
    # 无风险利率：与上年末（年初基准）比较，单位 bp
    prev_year = df.loc[df.index.year < df.index[-1].year]
    if not prev_year.empty:
        info["y10_prev"] = float(prev_year["y10"].iloc[-1])
        info["y10_prev_date"] = prev_year.index[-1]
        info["y10_ytd_bp"] = float(last["y10"] - info["y10_prev"]) * 100
    info["verdict"], info["double_confirm"] = verdict_for(p10, info["pct_ey"][10], cfg)
    return info


def window_stats(df: pd.DataFrame, cfg: dict) -> dict:
    out = {}
    for w in (1, 3, 5, 10, 0):
        if w == 0:
            pe = float((df["erp_bp"] > df["erp_bp"].iloc[-1]).mean() * 100)
            py = float((df["ey_pct"] > df["ey_pct"].iloc[-1]).mean() * 100)
        else:
            pe = cheapness_pct(df, w, col="erp_bp")
            py = cheapness_pct(df, w, col="ey_pct")
        tier = pick_tier(pe, cfg)
        verdict, _ = verdict_for(pe, py, cfg)
        out[str(w)] = {
            "erp_pct": round(pe, 1),
            "ey_pct": round(py, 1),
            "tier": tier["label"],
            "verdict": VERDICT_SHORT.get(verdict, verdict),
            "color": tier["color"],
            "bg": f'{tier["color"]}14',
            "border": f'{tier["color"]}55',
        }
    return out


# --------------------------------------------------------------------------- #
# 输出：静态图（主指数）
# --------------------------------------------------------------------------- #
def render_chart(df: pd.DataFrame, info: dict, name: str, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    plt.rcParams["font.sans-serif"] = ["PingFang HK", "Hiragino Sans GB", "STHeiti", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    view = df.loc[df.index >= df.index[-1] - pd.DateOffset(years=5)]
    fig, ax1 = plt.subplots(figsize=(11, 4.6), dpi=160)
    fig.patch.set_facecolor("white")

    ax1.plot(view.index, view["erp_bp"], color="#e8833a", lw=1.8, label="ERP", zorder=3)

    cheap, rich = line_values(df)
    lo = min(float(view["erp_bp"].min()) - 20, cheap - 40)
    hi = max(float(view["erp_bp"].max()) + 20, cheap + 40)
    ax1.set_ylim(lo, hi)
    ax1.set_ylabel("ERP（bp）", fontsize=9, color="#555555")
    ax1.grid(axis="y", ls=":", color="#e6e6e6")

    for val, color, label in ((cheap, "#c8c8c8", "加仓线"), (rich, "#dcdcdc", "防守线")):
        if not (lo <= val <= hi):
            continue
        txt = f"{val:.0f}bp {label}"
        ax1.axhline(val, color=color, ls="--", lw=0.9, zorder=1)
        ax1.annotate(txt, xy=(0.004, val), xycoords=ax1.get_yaxis_transform(),
                     xytext=(0, 3), textcoords="offset points",
                     ha="left", va="bottom", color="#9a9a9a", fontsize=8,
                     bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85))

    ax2 = ax1.twinx()
    ax2.plot(view.index, view["close"], color="#9a9a9a", lw=1.3, alpha=0.85, zorder=2)
    ax2.set_ylabel(f"{name}点位", fontsize=9, color="#777777")
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))

    for ax in (ax1, ax2):
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_visible(False)

    ax1.scatter([info["date"]], [info["erp_bp"]], s=38, color="#e8833a", zorder=5,
                edgecolor="white", lw=0.8)
    ax1.annotate(f'{info["erp_bp"]:.0f}bp', xy=(info["date"], info["erp_bp"]),
                 xytext=(-14, 4), textcoords="offset points",
                 color="#c0392b", fontsize=11, fontweight="bold", ha="right", va="bottom",
                 bbox=dict(boxstyle="round,pad=0.25", fc="#fdf0e6", ec="none"))

    ax1.legend([ax1.get_lines()[0], ax2.get_lines()[0]], ["ERP (bp)", name],
               loc="upper left", frameon=False, fontsize=9, ncol=2)
    ax1.set_title(
        f'{name} · 股债利差 ERP · {info["date"]:%Y-%m-%d}   |   '
        f'5 年分位 {info["pct_erp"][5]:.1f}%   10 年分位 {info["pct_erp"][10]:.1f}%',
        fontsize=11, pad=12,
    )
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


# --------------------------------------------------------------------------- #
# 输出：周报（精简版，适合微信推送）
# --------------------------------------------------------------------------- #
def fmt_chg(x: float | None, unit: str = "bp") -> str:
    if x is None or math.isnan(x):
        return "—"
    v = round(x)
    return f"0{unit}" if v == 0 else f"{v:+d}{unit}"


def render_report(info: dict, cfg: dict, name: str, path: Path,
                  turnover_md: str | None = None) -> str:
    tier = info["tier"]
    p5, p10 = info["pct_erp"][5], info["pct_erp"][10]
    badge = TIER_BADGE.get(tier["label"], "")
    ey_p10 = info["pct_ey"][10]
    erp_min, erp_max = info["erp_min"], info["erp_max"]
    sign = ">" if ey_p10 > cfg["signal"]["confirm_ey_pct"] else "≤"

    confirm_emoji = "🟩" if info["double_confirm"] else "🟧"
    md = f"""**股债性价比**

{badge} ERP 10 年分位 {p10:.1f}% ≤{tier["max"]}% → {tier["label"]}
{confirm_emoji} 估值 10 年分位 {ey_p10:.1f}% {sign}{cfg["signal"]["confirm_ey_pct"]}% → {"双确认" if info["double_confirm"] else "单边"}
{VERDICT_NOTE.get(info["verdict"], "")}

**ERP {info["erp_bp"]:.0f}bp（周 {fmt_chg(info["change_1w"])}）**
无风险利率 {info["y10"]:.2f}% ＋ 风险溢价 {info["erp_pct"]:.2f}% ＝ 股权收益率 {info["ey"]:.2f}%
分位 {p5:.1f}% / {p10:.1f}%（5 年 / 10 年）
"""
    if turnover_md:
        md += "\n***\n\n" + turnover_md.strip() + "\n"
    md += f'\n数据截至 {info["date"]:%m-%d}｜每交易日 08:30 更新\n'
    path.write_text(md, encoding="utf-8")
    return md


# --------------------------------------------------------------------------- #
# 输出：台账
# --------------------------------------------------------------------------- #
LOG_COLS = ["index", "date", "y10", "ey", "erp_bp", "pct5y", "pct10y", "pct_ey10y", "tier"]


def make_row(df: pd.DataFrame, cfg: dict, asof: pd.Timestamp, code: str) -> dict:
    sub = df.loc[:asof]
    p5 = cheapness_pct(df, 5, asof, col="erp_bp")
    p10 = cheapness_pct(df, 10, asof, col="erp_bp")
    ey10 = cheapness_pct(df, 10, asof, col="ey_pct")
    return {
        "index": code,
        "date": f"{sub.index[-1]:%Y-%m-%d}",
        "y10": round(float(sub["y10"].iloc[-1]), 2),
        "ey": round(float(sub["ey_pct"].iloc[-1]), 2),
        "erp_bp": round(float(sub["erp_bp"].iloc[-1])),
        "pct5y": round(p5, 1),
        "pct10y": round(p10, 1),
        "pct_ey10y": round(ey10, 1),
        "tier": pick_tier(p10, cfg)["label"],
    }


def seed_log(df: pd.DataFrame, cfg: dict, code: str) -> pd.DataFrame:
    rows = []
    for d in pd.date_range(end=df.index[-1] - pd.Timedelta(days=1),
                           periods=cfg["report"]["recent_weeks"], freq="W-FRI"):
        sub = df.loc[:d]
        if not sub.empty:
            rows.append(make_row(df, cfg, sub.index[-1], code))
    return pd.DataFrame(rows, columns=LOG_COLS)


def update_log(log: pd.DataFrame, row: dict) -> pd.DataFrame:
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
    return (log.drop_duplicates(["index", "date"], keep="last")
               .sort_values(["index", "date"]).reset_index(drop=True))


# --------------------------------------------------------------------------- #
# 输出：交互网页
# --------------------------------------------------------------------------- #
def index_payload(df: pd.DataFrame, cfg: dict, info: dict, log: pd.DataFrame,
                  code: str, name: str) -> dict:
    cheap, rich = line_values(df)
    return {
        "key": code,
        "name": name,
        "stats": {
            "asof": f'{info["date"]:%Y-%m-%d}',
            "updated": f'{datetime.now():%Y-%m-%d %H:%M}',
            "erp_bp": round(info["erp_bp"]),
            "tier": info["tier"]["label"],
            "verdict": info["verdict"],
            "change_1w": round(info["change_1w"]) if info["change_1w"] is not None else 0,
            "y10": round(info["y10"], 2),
            "y10_prev": round(info["y10_prev"], 2) if "y10_prev" in info else None,
            "y10_ytd_bp": round(info["y10_ytd_bp"]) if "y10_ytd_bp" in info else None,
            "y10_prev_date": f'{info["y10_prev_date"]:%Y-%m-%d}' if "y10_prev_date" in info else None,
            "ey": round(info["ey"], 2),
            "pe": round(info["pe"], 2),
            "close": round(info["close"], 2),
            "pct_ey_10": round(info["pct_ey"][10], 1),
            "cheap": round(cheap),
            "rich": round(rich),
        },
        "by_window": window_stats(df, cfg),
        "series": {
            "dates": [f"{d:%Y-%m-%d}" for d in df.index],
            "erp_bp": [round(v) for v in df["erp_bp"]],
            "y10": [round(v, 2) for v in df["y10"]],
            "ey": [round(v, 2) for v in df["ey_pct"]],
            "close": [round(v, 1) for v in df["close"]],
            "pe": [round(v, 2) for v in df["pe"]],
        },
        "log": log[log["index"] == code].tail(cfg["report"]["recent_weeks"])
                 .drop(columns=["index"]).to_dict(orient="records"),
    }


def render_web(indices: list[dict], cfg: dict, log: pd.DataFrame) -> Path:
    if not WEB_TEMPLATE.exists():
        raise FileNotFoundError(f"缺少模板: {WEB_TEMPLATE}")
    payload = {
        "indices": indices,
        "tiers": cfg["signal"]["tiers"],
        "tier_colors": {t["label"]: t["color"] for t in cfg["signal"]["tiers"]},
        "confirm_ey_pct": cfg["signal"]["confirm_ey_pct"],
    }
    html = WEB_TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    WEB_INDEX.write_text(html, encoding="utf-8")
    return WEB_INDEX


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #
def push_feishu(webhook: str, title: str, body: str) -> None:
    """飞书自定义机器人：交互卡片（lark_md），能正常渲染加粗与换行。"""
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    emoji_color = {"🟩": "green", "🟢": "green", "🟨": "yellow", "🟧": "orange",
                   "🟥": "red", "🔵": "blue", "🟦": "blue"}
    rendered: list[str] = []
    elements: list[dict] = []

    def flush() -> None:
        if rendered:
            text = "\n".join(rendered).replace("<sub>", "").replace("</sub>", "")
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": text}})
            rendered.clear()

    for ln in lines:
        if ln.strip() == "***":  # 飞书不支持 *** 分割线，切成段落并插入原生 hr 元素
            flush()
            elements.append({"tag": "hr"})
            continue
        if not ln.strip():
            continue
        head = ln.lstrip("*")
        color = emoji_color.get(head[0]) if head else None
        rendered.append(f"<font color='{color}'>{ln}</font>" if color else ln)
    flush()
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "orange"},
        "elements": elements,
    }
    resp = requests.post(webhook, json={"msg_type": "interactive", "card": card}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code", payload.get("StatusCode")) not in (0, None):
        raise RuntimeError(f"飞书返回: {payload.get('msg') or payload}")


def send_all(cfg: dict, title: str, body: str) -> list[str]:
    """推送飞书群机器人，失败不影响主流程。"""
    fs = (cfg.get("push", {}) or {}).get("feishu") or {}
    if not (fs.get("enabled") and fs.get("webhook")):
        return []
    try:
        push_feishu(fs["webhook"], title, body)
        return ["feishu"]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] feishu 推送失败: {exc}", file=sys.stderr)
        return []


def notify(cfg: dict, info: dict, name: str, md_path: Path,
           turnover_payload: dict | None = None) -> list[str]:
    title = f'股债性价比 {name} {info["date"]:%m-%d} · {info["tier"]["label"]}'
    if turnover_payload:
        title += f' ｜ 成交{turnover_payload["regime"]["tier"]}'
    body = md_path.read_text(encoding="utf-8")
    return send_all(cfg, title, body)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="股债性价比周度监控（多指数 / ERP）")
    parser.add_argument("--no-fetch", action="store_true", help="只用本地缓存")
    parser.add_argument("--no-web", action="store_true", help="不生成交互网页")
    parser.add_argument("--no-push", action="store_true", help="不推送")
    parser.add_argument("--quiet", action="store_true", help="不打印摘要")
    parser.add_argument("--test-push", action="store_true", help="只发一条测试推送到已启用渠道")
    args = parser.parse_args()

    cfg = load_config()

    if args.test_push:
        title = "【测试】股债性价比推送"
        body = ("# 推送测试\n\n"
                "如果你在飞书群里看到这条，说明通道已经打通。\n\n"
                "- ERP 442bp（示例）\n- 5 年分位 28.9%｜10 年分位 14.9%\n- 极端便宜 · 单边便宜")
        sent = send_all(cfg, title, body)
        print("已发送:" + (", ".join(sent) if sent else "无（没有启用任何渠道）"))
        return 0

    indices = cfg["indices"]

    if args.no_fetch:
        bond = pd.read_csv(BOND_FILE, parse_dates=["date"])
    else:
        bond = fetch_bond(cfg)
        bond.to_csv(BOND_FILE, index=False, date_format="%Y-%m-%d")

    frames, skipped = {}, []
    for item in indices:
        code = item["code"]
        try:
            if args.no_fetch:
                pe = pd.read_csv(pe_file(code), parse_dates=["date"])
            elif item.get("source") == "cyb50_lg":
                pe = fetch_pe_cyb50(cfg, code)
                pe.to_csv(pe_file(code), index=False, date_format="%Y-%m-%d")
            else:
                pe = fetch_pe(cfg, code)
                pe.to_csv(pe_file(code), index=False, date_format="%Y-%m-%d")
            frames[code] = build_frame(pe, bond)
        except Exception as exc:  # noqa: BLE001
            skipped.append(f'{item["name"]}({code})')
            print(f"[warn] 跳过 {item['name']}: {exc}", file=sys.stderr)

    if not frames:
        print("没有可用的指数数据", file=sys.stderr)
        return 1

    usable = [i for i in indices if i["code"] in frames]
    primary = usable[0]
    pdf = frames[primary["code"]]
    bond_date = pd.Timestamp(bond["date"].max())
    info = describe(pdf, cfg, bond_date)

    if LOG_FILE.exists():
        log = pd.read_csv(LOG_FILE, dtype={"date": str, "index": str})
        if "index" not in log.columns:
            log = pd.concat([seed_log(frames[i["code"]], cfg, i["code"])
                             for i in usable], ignore_index=True)
    else:
        log = pd.concat([seed_log(frames[i["code"]], cfg, i["code"])
                         for i in usable], ignore_index=True)
    log = update_log(log, make_row(pdf, cfg, pdf.index[-1], primary["code"]))
    log.to_csv(LOG_FILE, index=False)

    # 两市成交额（独立数据源，失败不影响 ERP 主流程）
    turnover_payload = None
    turnover_path = None
    turnover_md = None
    leverage_payload = None
    leverage_path = None
    try:
        import turnover as turnover_mod
        try:
            turnover_result = turnover_mod.run(cfg, no_fetch=args.no_fetch, quiet=True,
                                               render_page=not args.no_web)
        except Exception as exc:  # noqa: BLE001
            if args.no_fetch:
                raise
            print(f"[warn] 成交额抓取失败，改用本地缓存: {exc}", file=sys.stderr)
            turnover_result = turnover_mod.run(cfg, no_fetch=True, quiet=True,
                                               render_page=not args.no_web)
        turnover_payload = turnover_result["payload"]
        turnover_path = turnover_result["path"]
        turnover_md = turnover_mod.summary_md(turnover_payload)
        leverage_payload = turnover_result.get("leverage")
        leverage_path = turnover_result.get("leverage_path")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 成交额数据生成失败: {exc}", file=sys.stderr)

    # 反向情绪监控（独立数据源，失败不影响 ERP / 成交额主流程）
    sentiment_payload = None
    sentiment_path = None
    try:
        import sentiment as sentiment_mod
        try:
            sentiment_result = sentiment_mod.run(cfg, no_fetch=args.no_fetch, quiet=True,
                                                 render_page=not args.no_web)
        except Exception as exc:  # noqa: BLE001
            if args.no_fetch:
                raise
            print(f"[warn] 反向情绪抓数失败，改用本地缓存: {exc}", file=sys.stderr)
            sentiment_result = sentiment_mod.run(cfg, no_fetch=True, quiet=True,
                                                 render_page=not args.no_web)
        sentiment_payload, sentiment_path = sentiment_result
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 反向情绪数据生成失败: {exc}", file=sys.stderr)

    week = f'{info["date"]:%G-W%V}'
    md_path = REPORT_DIR / f"{week}.md"
    png_path = REPORT_DIR / f"{week}.png"
    render_chart(pdf, info, primary["name"], png_path)
    render_report(info, cfg, primary["name"], md_path, turnover_md=turnover_md)

    web_path = None
    payloads: list[dict] = []
    if not args.no_web:
        payloads = [index_payload(frames[i["code"]], cfg,
                                  info if i["code"] == primary["code"] else describe(
                                      frames[i["code"]], cfg, bond_date),
                                  log, i["code"], i["name"])
                    for i in usable]
        web_path = render_web(payloads, cfg, log)
        try:
            import portal as portal_mod
            portal_mod.build()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 门户页生成失败: {exc}", file=sys.stderr)

    sent = [] if args.no_push else notify(cfg, info, primary["name"], md_path,
                                          turnover_payload=turnover_payload)

    if not args.quiet:
        print(f'[{info["date"]:%Y-%m-%d}] {primary["name"]} ERP {info["erp_bp"]:.0f}bp | '
              f'{info["tier"]["label"]} | {info["verdict"]}')
        for p in payloads:
            print(f'  {p["name"]:<6} ERP {p["stats"]["erp_bp"]:>4}bp  10年分位 {p["by_window"]["10"]["erp_pct"]:>5.1f}%  {p["by_window"]["10"]["tier"]}')
        if skipped:
            print(f"  跳过: {', '.join(skipped)}")
        print(f"报告: {md_path}")
        print(f"图表: {png_path}")
        if web_path:
            print(f"网页: {web_path}")
        if turnover_payload:
            tst = turnover_payload["stats"]
            trg = turnover_payload["regime"]
            print(f'  两市成交额（日均） {tst["week_amount"]:.0f}亿 | 本周换手率 {tst["week_turn"]:.2f}% | '
                  f'924以来分位 {trg["turn_pct"]:.1f}% | {trg["tier"]}')
            if leverage_payload:
                ls = leverage_payload["stats"]
                print(f'  杠杆率 {ls["ratio"]:.2f}% ({leverage_payload["asof"]}) | {ls["band"]} | '
                      f'融资余额 {ls["balance_wan"]:.2f}万亿 | 融资买入占比 {ls["buy_share"]:.2f}%')
        if turnover_path:
            print(f"成交额页: {turnover_path}")
        if leverage_path:
            print(f"杠杆页: {leverage_path}")
        if sentiment_payload:
            sp = sentiment_payload["stats"]
            print(f'  反向情绪 温度 {sentiment_payload["composite"]["score"]:.1f} '
                  f'({sentiment_payload["composite"]["tier"]}) | '
                  f'基金现金 {sp["fund"]["cash"]:.2f}% | '
                  f'融资20日 {sp["margin"]["chg20"]:+.2f}% ({sp["margin"]["tier"]}) | '
                  f'开户 {sp["account"]["new_a"]:.1f}万户')
        if sentiment_path:
            print(f"反向情绪页: {sentiment_path}")
        print(f"推送: {', '.join(sent) if sent else '关闭'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
