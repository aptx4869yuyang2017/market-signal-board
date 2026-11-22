# 契约 · 门户首页

| 字段 | 值 |
|---|---|
| 页面 | 门户首页（四张看板的总入口） |
| 生成物 | `web/portal.html` |
| 模板 | `web/portal_template.html` |
| 生成脚本 | `portal.py`（`build()`），由 `monitor.py` 在生成四张页面后调用 |
| 数据来源 | 读取 `web/index.html` / `web/turnover.html` / `web/leverage.html` / `web/sentiment.html` 中已注入的 `DATA`，不新增抓数 |
| 入口 | 本地双击 `web/portal.html`；线上部署为站点根 `index.html` |
| 状态 | 使用中 |
| 最近更新 | 2026-11-22 · 新增反向情绪卡片 |

## 页面定位

回答一个问题：**今天四个维度各自什么温度？**

- 读者：本人。日常先看首页判断要不要点进详情。
- 主信号：中证全指 ERP 的 10 年便宜度分位，辅以成交额冷热、杠杆档位与反向情绪温度。
- 不做新计算，只汇总四张页面已有的结果，保证口径唯一。

## 页面结构

```text
顶栏        品牌 · 首页/股债性价比/两市成交额/杠杆资金/反向情绪
Hero        标题 · 一句话说明 · 数据截至/更新时间/更新节奏 · 主信号条（ERP + 档位 + 结论）
四卡        股债性价比（ERP）· 两市成交额（周日均 + 换手率分位）· 杠杆资金（融资余额 + 杠杆率分位）· 反向情绪（温度 + 四项摘要）
指数表      六只指数的 ERP / 周变化 / 10 年分位 / 5 年分位 / 档位
页脚        口径、数据源与时滞、免责声明
```

无 JS、无图表；所有数字在生成时写入静态 HTML，断网可看。

## 数据契约

`portal.py` 用正则从三张页面提取 `const DATA = {...};`，只读以下字段：

| 来源 | 字段 | 用途 |
|---|---|---|
| `index.html` | `indices[0].stats`：`asof` / `updated` / `erp_bp` / `change_1w` / `ey` / `y10` / `pct_ey_10` / `verdict` | 顶部时间、Hero 信号条、ERP 卡 |
| `index.html` | `indices[0].by_window.10`：`erp_pct` / `tier` / `color` / `bg` / `border` | 档位、配色、进度条 |
| `index.html` | `indices[*]`：`key` / `name` / `stats` / `by_window` | 指数对比表 |
| `turnover.html` | `stats.week_amount` / `amount_chg_pct` / `week_turn` / `cold` / `hot` | 成交额卡 |
| `turnover.html` | `regime`：`turn_pct` / `tier` / `note` / `color` / `bg` / `border` | 档位、配色、进度条 |
| `leverage.html` | `stats.balance_wan` / `balance` / `ratio` / `ratio_chg` / `ratio_pct` / `band` / `band_range` / `band_note` / `color` / `bg` / `border` | 杠杆卡 |
| `sentiment.html` | `composite.score` / `tier` / `note` / `color` / `bg` / `border`；`stats.fund.cash` / `cash_pct`；`stats.margin.chg20` / `tier`；`stats.account.new_a` / `pct`；`stats.north.deal_ma20` | 反向情绪卡 |

模板占位符形如 `{{ERP_BP}}`，生成后不应残留 `{{`。

## 交互契约

- 顶栏五个胶囊：首页（当前）、股债性价比、两市成交额、杠杆资金、反向情绪；分别为 `portal.html` / `index.html` / `turnover.html` / `leverage.html` / `sentiment.html`。
- 四张卡片整体可点，点击进入对应详情页。
- 无筛选、无 Tooltip、无缩放。

## 计算契约

| 展示值 | 规则 |
|---|---|
| 风险溢价 | `erp_bp / 100`，保留 2 位（463bp → 4.63%） |
| 周日均成交额 | `week_amount / 10000`，保留 2 位（万亿） |
| 周变化 / 月变化 | 带符号，整数 bp 或 1 位小数 %/pct；`>=0` 用绿色 `up`，否则红色 `down` |
| 分位与条形宽度 | 保留 1 位小数，直接作为 `width:%` |
| 杠杆率变化 | 保留 2 位小数（±0.02pct 不能显示成 ±0.0） |
| 温度 / 基金现金比例 | 温度 1 位小数，基金现金比例 2 位小数 |

## 文案与配色

- 背景 `#f7f6f3`、卡片白、墨色 `#1f2328`、强调橙 `#e8833a`，与三张详情页一致。
- 档位颜色全部取自数据：ERP 用 `by_window.10`，成交额用 `regime`，杠杆用 `stats`，反向情绪用 `composite`，页面不自定义阈值色。
- 更新节奏文案固定为「每交易日 08:30 自动更新」。

## 边界

- 静态快照：只有运行 `monitor.py`（或 `daily_update.sh`）才更新，浏览器不会自动刷新。
- 杠杆数据 T+1，首页显示的是最近可用值，不保证与指数同一天；反向情绪卡里的基金现金为季度值、开户为月度值，各自截止日见详情页。
- 免费数据源无 SLA，仅适合个人监控，不对外披露；不构成投资建议。

## 验收清单

- [ ] `web/portal.html` 生成后无 `{{` 残留。
- [ ] 首页四个数字与对应详情页一致（ERP、周日均成交额、融资余额、温度）。
- [ ] 六只指数行数、档位颜色与 ERP 页一致。
- [ ] 线上站点根路径打开即首页，四张卡片都能跳到正确页面。
- [ ] 手机宽度下四卡纵向排列，不出现横向滚动。

## 变更记录

- 2026-11-19 · 新建门户首页，改为数据驱动生成（模板 + `portal.py`）。
- 2026-11-22 · 新增反向情绪卡片与导航，四卡布局。
- 2026-11-22 · 反向情绪卡口径翻转为市场温度（0 冰点 → 100 过热），进度条与刻度方向同步。
