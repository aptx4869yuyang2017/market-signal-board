"""门户首页（web/portal.html）生成器。

读取 index / turnover / leverage / sentiment 四张页面里已注入的 DATA JSON，
填充 web/portal_template.html，输出 web/portal.html。

用法：python portal.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent / "web"
TEMPLATE = WEB_DIR / "portal_template.html"
OUTPUT = WEB_DIR / "portal.html"

_DATA_RE = re.compile(r"const DATA = (\{.*?\});\n", re.S)


def load_payload(path: Path) -> dict:
    match = _DATA_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"{path.name} 中找不到 DATA 载荷")
    return json.loads(match.group(1))


def _signed(value: float) -> str:
    return f"{value:+.0f}" if float(value).is_integer() else f"{value:+.1f}"


def _cls(value: float) -> str:
    return "up" if value >= 0 else "down"


def _pct(value: float) -> str:
    return f"{value:.1f}"


def index_sub(item: dict, is_primary: bool) -> str:
    code = item["key"]
    if is_primary:
        return f" · 主口径"
    if code == "000688":
        return " · 2020 起"
    if code == "399673":
        return " · 推算口径"
    return ""


def index_rows(indices: list[dict]) -> str:
    rows = []
    for i, item in enumerate(indices):
        stats, w10, w5 = item["stats"], item["by_window"]["10"], item["by_window"]["5"]
        chg = stats["change_1w"]
        rows.append(
            "        <tr>\n"
            f'          <td><span class="name">{item["name"]}</span>'
            f'<span class="sub">{item["key"]}{index_sub(item, i == 0)}</span></td>\n'
            f'          <td>{stats["erp_bp"]:.0f}bp</td>'
            f'<td class="{_cls(chg)}">{_signed(chg)}</td>'
            f'<td>{_pct(w10["erp_pct"])}%</td>'
            f'<td>{_pct(w5["erp_pct"])}%</td>\n'
            f'          <td><span class="pill" style="color:{w10["color"]}; '
            f'background:{w10["bg"]}">{w10["tier"]}</span></td>\n'
            "        </tr>"
        )
    return "\n".join(rows)


def build(web_dir: Path = WEB_DIR) -> Path:
    erp = load_payload(web_dir / "index.html")
    turnover = load_payload(web_dir / "turnover.html")
    leverage = load_payload(web_dir / "leverage.html")
    sentiment = load_payload(web_dir / "sentiment.html")

    main = erp["indices"][0]
    st, w10, w5 = main["stats"], main["by_window"]["10"], main["by_window"]["5"]

    to_stats, to_regime = turnover["stats"], turnover["regime"]
    lv = leverage["stats"]
    sm, sf, smg, sa, sn = (sentiment["composite"], sentiment["stats"]["fund"],
                          sentiment["stats"]["margin"], sentiment["stats"]["account"],
                          sentiment["stats"]["north"])

    values = {
        "ASOF": st["asof"],
        "UPDATED": st["updated"],
        # ERP
        "ERP_NAME": main["name"],
        "ERP_BP": f'{st["erp_bp"]:.0f}',
        "ERP_CHG": _signed(st["change_1w"]),
        "ERP_CHG_CLASS": _cls(st["change_1w"]),
        "ERP_TIER": w10["tier"],
        "ERP_COLOR": w10["color"],
        "ERP_BG": w10["bg"],
        "ERP_BORDER": w10["border"],
        "ERP_PCT10": _pct(w10["erp_pct"]),
        "ERP_PCT5": _pct(w5["erp_pct"]),
        "ERP_EY": f'{st["ey"]:.2f}',
        "ERP_Y10": f'{st["y10"]:.2f}',
        "ERP_PREMIUM": f'{st["erp_bp"] / 100:.2f}',
        "ERP_SIGNAL_TEXT": (
            f'{main["name"]} 10 年便宜度分位 {_pct(w10["erp_pct"])}%，'
            f'{w10["tier"]}。{st["verdict"]}（估值分位 {_pct(st["pct_ey_10"])}%）。'
        ),
        # 成交额
        "TO_AMT": f'{to_stats["week_amount"] / 10000:.2f}',
        "TO_CHG": _signed(to_stats["amount_chg_pct"]),
        "TO_CHG_CLASS": _cls(to_stats["amount_chg_pct"]),
        "TO_TIER": to_regime["tier"],
        "TO_COLOR": to_regime["color"],
        "TO_BG": to_regime["bg"],
        "TO_BORDER": to_regime["border"],
        "TO_NOTE": to_regime["note"],
        "TO_WEEK_TURN": f'{to_stats["week_turn"]:.2f}',
        "TO_TURN_PCT": _pct(to_regime["turn_pct"]),
        "TO_COLD": f'{to_stats["cold"]:.2f}',
        "TO_HOT": f'{to_stats["hot"]:.2f}',
        # 杠杆
        "LV_BALANCE": f'{lv["balance_wan"]:.2f}',
        "LV_BALANCE_RAW": f'{lv["balance"]:.0f}',
        "LV_RATIO": f'{lv["ratio"]:.2f}',
        "LV_RATIO_CHG": f'{lv["ratio_chg"]:+.2f}',
        "LV_CHG_CLASS": _cls(lv["ratio_chg"]),
        "LV_RATIO_PCT": _pct(lv["ratio_pct"]),
        "LV_BAND": lv["band"],
        "LV_RANGE": lv["band_range"],
        "LV_NOTE": lv["band_note"],
        "LV_COLOR": lv["color"],
        "LV_BG": lv["bg"],
        "LV_BORDER": lv["border"],
        # 反向情绪
        "SM_SCORE": _pct(sm["score"]),
        "SM_TIER": sm["tier"],
        "SM_COLOR": sm["color"],
        "SM_BG": sm["bg"],
        "SM_BORDER": sm["border"],
        "SM_NOTE": sm["note"],
        "SM_FUND_CASH": f'{sf["cash"]:.2f}',
        "SM_FUND_PCT": _pct(sf["cash_pct"]),
        "SM_MARGIN_CHG": _signed(smg["chg20"]),
        "SM_MARGIN_TIER": smg["tier"],
        "SM_ACCOUNT": f'{sa["new_a"]:.0f}',
        "SM_ACCOUNT_PCT": _pct(sa["pct"]),
        "SM_NORTH_DEAL": f'{sn["deal_ma20"]:.0f}',
        # 指数表
        "INDEX_ROWS": index_rows(erp["indices"]),
    }

    html = (web_dir / TEMPLATE.name).read_text(encoding="utf-8")
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", str(value))

    output = web_dir / OUTPUT.name
    output.write_text(html, encoding="utf-8")
    return output


if __name__ == "__main__":
    print(build())
