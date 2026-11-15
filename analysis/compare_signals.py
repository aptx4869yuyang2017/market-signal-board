import pandas as pd, numpy as np
from scipy.stats import spearmanr

df = pd.read_csv("data/ratio_series.csv", parse_dates=["date"]).set_index("date")
df["erp"] = df["ey"]*100 - df["y10"]          # 股债利差，pp
df["ey_pct"] = df["ey"]*100

# 未来 252 个交易日收益（重叠，仅作方向性检验）
df["fwd"] = df["close"].shift(-252) / df["close"] - 1
mo = df.resample("ME").last().dropna(subset=["fwd"])

print(f"样本：{mo.index[0].date()} ~ {mo.index[-1].date()}，{len(mo)} 个月度观测\n")

def ic(sub, col):
    r, p = spearmanr(sub[col], sub["fwd"])
    return r, p

for label, sub in [("全样本", mo), ("2019 年以后", mo[mo.index >= "2019-01-01"])]:
    print(f"== {label}：与未来 12 个月收益的秩相关 (IC) ==")
    for col, name in [("ratio","倍数 EY/y10"), ("erp","差值 EY-y10"), ("ey_pct","盈利收益率 EY"), ("pe","市盈率 PE")]:
        r, p = ic(sub, col)
        print(f"  {name:<14} IC={r:+.3f}  (p={p:.3f})")
    print()

print("== 五分位：按指标高低分组，未来 12 个月平均收益 ==")
for col, name in [("ratio","倍数"), ("erp","差值")]:
    sub = mo.dropna(subset=[col]).copy()
    sub["q"] = pd.qcut(sub[col], 5, labels=["最低","较低","中","较高","最高"])
    g = sub.groupby("q", observed=True)["fwd"].agg(["mean","count"])
    s = "  ".join(f"{k}:{v*100:+.1f}%" for k, v in g["mean"].items())
    print(f"  {name}: {s}")

print("\n== 2014-12-31 的横截面：三个指标各说什么 ==")
row = df.loc[:"2014-12-31"].iloc[-1]
def rank(v, s): return (s > v).mean()*100
print(f"  PE={row.pe:.1f}  EY={row.ey*100:.2f}%  y10={row.y10:.2f}%  ratio={row.ratio:.2f}  ERP={row.erp:.2f}pp")
print(f"  便宜度分位 -> 倍数 {rank(row.ratio, df.ratio):.0f}% | 差值 {rank(row.erp, df.erp):.0f}% | EY {rank(row.ey, df.ey):.0f}%")
print("  (分位越低=越便宜；2015 年上半年中证全指大涨)")
