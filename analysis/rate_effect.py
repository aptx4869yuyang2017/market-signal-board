import pandas as pd, numpy as np
df = pd.read_csv("data/ratio_series.csv", parse_dates=["date"]).set_index("date")
df["erp"] = df["ey"]*100 - df["y10"]          # 股债利差，百分点
cur = df.iloc[-1]
print(f"最新 {df.index[-1].date()}: PE={cur.pe:.2f} EY={cur.ey*100:.2f}% y10={cur.y10:.3f}% ratio={cur.ratio:.3f} ERP={cur.erp:.2f}pp")

print("\n== 年度均值：无风险利率与盈利收益率 ==")
ann = df.resample("YE").agg(y10=("y10","mean"), ey=("ey", lambda s: s.mean()*100), ratio=("ratio","mean"), pe=("pe","mean"))
print(ann.round(2).to_string())

def dec(start):
    start = pd.Timestamp(start)
    a = df.loc[:start].iloc[-1]; b = df.iloc[-1]
    dlr = np.log(b.ratio/a.ratio)
    d_rate = -np.log(b.y10/a.y10)
    d_ey = np.log(b.ey/a.ey)
    print(f"\n{start.date()} -> {b.name.date()}  ratio {a.ratio:.2f}->{b.ratio:.2f} (x{b.ratio/a.ratio:.2f})")
    print(f"  总变化 {dlr:+.3f} = 利率贡献 {d_rate:+.3f} ({d_rate/dlr*100:.0f}%) + 盈利收益率贡献 {d_ey:+.3f} ({d_ey/dlr*100:.0f}%)")
    print(f"  EY {a.ey*100:.2f}%->{b.ey*100:.2f}%   y10 {a.y10:.2f}%->{b.y10:.2f}%")

for s in ["2014-01-02","2019-12-31","2021-12-31","2024-01-31"]:
    dec(s)

def pct_cheap(s):
    return (s > s.iloc[-1]).mean()*100
print("\n== 三种口径的『便宜度分位』(越低越便宜) ==")
print(f"  比值 EY/y10      : {pct_cheap(df.ratio):.1f}%   (当前 {df.ratio.iloc[-1]:.3f})")
print(f"  盈利收益率 EY    : {pct_cheap(df.ey):.1f}%   (当前 {df.ey.iloc[-1]*100:.2f}%)")
print(f"  股债利差 ERP     : {pct_cheap(df.erp):.1f}%   (当前 {df.erp.iloc[-1]:.2f}pp)")
print(f"  PE 分位(低=便宜)  : {(df.pe < df.pe.iloc[-1]).mean()*100:.1f}%   (当前 {df.pe.iloc[-1]:.2f})")

print("\n== 相关性（对数变化）==")
d = pd.DataFrame({"lratio": np.log(df.ratio), "ly10": np.log(df.y10), "ley": np.log(df.ey)}).dropna()
print(d.corr().round(3).to_string())
print(f"  回归 ln(ratio) ~ ln(y10): 斜率 {np.polyfit(d.ly10, d.lratio, 1)[0]:.3f}")

print("\n== 利率敏感度 ==")
y = df.y10.iloc[-1]
print(f"  当前 y10={y:.3f}%，10bp 变动 -> 比值变动 {-0.1/y*100:+.1f}%")
print(f"  ratio=2.0 在今天利率下对应 EY={2*y:.2f}% (PE={100/(2*y):.1f})")
for yr in ["2014","2019","2024"]:
    sub = df.loc[yr]
    print(f"  {yr} 年 y10 均值 {sub.y10.mean():.2f}% 时，ratio=2.0 对应 EY={2*sub.y10.mean():.2f}% (PE={100/(2*sub.y10.mean()):.1f})")
