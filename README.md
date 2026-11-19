# 股债性价比 · 周度监控（ERP + 两市成交额 + 杠杆资金）

每交易日 08:30 自动抓数、计算 ERP、两市成交额与杠杆资金，生成门户首页与三张交互页面、一份报告，推送一条飞书通知，并自动重新部署线上站点。

**主口径：ERP = 中证全指收益率 − 10 年期国债收益率，单位 bp。** 442bp 即 4.42%。

## 文档地图

| 想了解 / 修改 | 看哪里 |
|---|---|
| 项目怎么跑、产出什么、共享口径 | 本页 |
| 门户首页（`web/portal.html`） | [`docs/pages/portal.md`](docs/pages/portal.md) |
| 股债性价比页（`web/index.html`） | [`docs/pages/erp.md`](docs/pages/erp.md) |
| 两市成交额页（`web/turnover.html`） | [`docs/pages/turnover.md`](docs/pages/turnover.md) |
| 杠杆资金页（`web/leverage.html`） | [`docs/pages/leverage.md`](docs/pages/leverage.md) |
| 周报与飞书推送 | [`docs/pages/weekly-report.md`](docs/pages/weekly-report.md) |

> **契约约定：一个页面对应一个 md 文件。**
> 页面定位、数据字段、交互行为、文案与配色、验收标准都写在对应契约里。
> 改页面前先改契约，再改代码；代码与契约不一致视为 bug。

## 快速开始

```bash
cd "AI Invest"
/opt/homebrew/anaconda3/bin/python3 monitor.py
```

`monitor.py` 一次跑完主流程，内部会调用 `turnover.py` 生成成交额页，由 `leverage.py` 生成杠杆资金页，最后由 `portal.py` 汇总生成门户首页。

每日更新（抓数 → 页面 → git 提交 → 重新部署）直接跑：

```bash
bash daily_update.sh
```

| 产出 | 路径 |
|---|---|
| 合并报告（ERP + 成交额，可直接推微信/飞书） | `reports/2026-W46.md` |
| ERP 走势图（PNG） | `reports/2026-W46.png` |
| 门户首页 | `web/portal.html` |
| 交互页面（股债性价比） | `web/index.html` |
| 交互页面（两市成交额） | `web/turnover.html` |
| 交互页面（杠杆资金） | `web/leverage.html` |
| 周度台账 | `data/weekly_log.csv` |
| 成交额日频序列 | `data/turnover_daily.csv` |
| 两市总市值月度锚点 | `data/market_cap_monthly.csv` |
| 杠杆日频（融资余额 / 买入额 / 流通市值） | `data/leverage_daily.csv` |

看页面：直接双击 `web/index.html`，或 `open "web/index.html"`。纯本地文件，断网可用（ECharts 已下载到 `web/echarts.min.js`）。

## 产出链路

| 阶段 | 脚本 | 输入 → 输出 |
|---|---|---|
| 抓数 + 算 ERP | `monitor.py` | 中证官网 PE、中债 10Y → `data/pe_*.csv`、`data/weekly_log.csv` |
| 生成 ERP 页 | `monitor.py` + `web/template.html` | `DATA` JSON 注入 → `web/index.html` |
| 生成成交额页 | `turnover.py` + `web/turnover_template.html` | 成交额、月度市值 → `web/turnover.html` |
| 生成杠杆页 | `leverage.py` + `web/leverage_template.html` | 两融日频、成交额日频 → `web/leverage.html` |
| 生成门户首页 | `portal.py` + `web/portal_template.html` | 三张页面的 `DATA` → `web/portal.html` |
| 周报 + 推送 | `monitor.py` | 上述结果 → `reports/<YYYY-Www>.md` / `.png` + 飞书 |

设计上 **所有计算都在 Python 侧完成**，页面只负责展示、格式化与按档位上色。新增指标时先扩展 payload 契约，再写前端。

## 全局口径

### 分位方向

分位 = 窗口内「比当前更便宜」的天数占比，即 **越低越便宜**。

- ERP 分位 20% 的意思是：历史上只有两成时间比现在更便宜，不是「便宜了 20%」。
- 两市成交额的分位方向相反：**越高越热**，详见 [`docs/pages/turnover.md`](docs/pages/turnover.md)。

### 信号档位（ERP，共享）

主判定看 **ERP 的 10 年便宜度分位**，阈值在 `config.yaml` 的 `signal.tiers`：

| 分位 | 档位 | 颜色 | 动作 |
|---|---|---|---|
| ≤20% | 极端便宜 | 深绿 | 加仓区 |
| 20–40% | 偏便宜 | 浅绿 | 定投/加仓 |
| 40–60% | 中性 | 黄 | 维持 |
| 60–80% | 偏贵 | 橙 | 减少买入 |
| >80% | 贵 | 红 | 防守 |

辅助规则：

- **绝对参考线**：ERP ≥ 400bp 视为较好的加仓区间，< 200bp 视为债券更有吸引力（只作参考）。
- **双确认**：指数收益率（=1/PE）的 10 年分位 ≤40% 才算「真便宜」；否则是「单边便宜」，只有债券端在动。
- **趋势**：连续 3 周分位同向变动才视为趋势，单周噪声忽略。
- 网页的分位与综合判断卡片随所选时间窗口切换；周报固定用主指数 + 10 年窗口。

### 数据源

| 数据 | 来源 | 频率 | 时滞 |
|---|---|---|---|
| 指数收盘价 + PE | 中证指数官网 `index-perf` 接口 | 日 | T+0 ~ T+1 |
| 10 年期国债收益率 | 中债登，经 `akshare.bond_zh_us_rate` 转发 | 日 | T+0 |
| 创业板50 PE | 乐咕乐股月频整体法静态 PE（锚点）+ 腾讯日频收盘价插值 | 日（推算） | 月频锚点 |
| 两市成交额 | 中证全指（000985）日频成交额 | 日 | T+0 ~ T+1 |
| 两市总市值 | 沪深交易所月度市价总值锚点，月内按指数涨跌衔接 | 月 | 官方值校准 |
| 融资余额 / 融资买入额 / 流通市值 | 东方财富融资融券历史合计（`RPTA_RZRQ_LSHJ`） | 日 | T+1 |

免费源、无需 token，历史自 2014 起。官网接口无 SLA，脚本内置 3 次重试 + 本地 CSV 缓存，断网也能用上次数据出报告（`--no-fetch`）。

支持的指数：

| 指数 | 代码 | 数据 |
|---|---|---|
| 中证全指（主） | 000985 | 中证指数官网，2014 起 |
| 沪深300 | 000300 | 同上 |
| 中证500 | 000905 | 同上 |
| 中证红利 | 000922 | 同上 |
| 科创50 | 000688 | 同上，2020 起 |
| 创业板50 | 399673 | 乐咕月频 PE 锚点 + 腾讯日频收盘价推算，2014-06 起 |

指数之间**只看分位，不看绝对值**。创业板指暂缺、创业板50 替代方案的口径与局限见 [`docs/pages/erp.md`](docs/pages/erp.md#已知差异与补充方案)。

## 配置与运行

### 常用参数

| 开关 | 作用 |
|---|---|
| `--no-fetch` | 不抓数，用缓存重算（断网时用） |
| `--no-web` | 不生成网页 |
| `--no-push` | 不推送 |
| `--quiet` | 不打印摘要 |
| `--test-push` | 只发一条测试推送到已启用渠道 |

单独重算成交额页：`python turnover.py`（会同时生成杠杆页，可加 `--no-fetch`）。
只重算杠杆页：`python leverage.py`（可加 `--no-fetch`）。

### 改阈值

全部在 `config.yaml`：

```yaml
signal:
  tiers: [...]          # 按 10 年分位分档
  confirm_ey_pct: 40    # 双确认：估值分位阈值
turnover:
  pct_start: "2024-09-24"
  tiers: [...]          # 按换手率分位分档
leverage:
  start: "2014-01-01"
  bands: [...]          # 杠杆率绝对区间（2% / 3% / 4%）
```

### 推送配置（飞书）

Webhook 属于敏感凭据，存放在项目根目录的 `.env`（已被 `.gitignore` 排除）：

```bash
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx
```

`config.yaml` 通过占位符引用：

```yaml
push:
  feishu:
    enabled: true
    webhook: "${FEISHU_WEBHOOK}"
```

获取方式：飞书群 → 设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook。

### 定时任务

计划任务「AI Invest 每日数据更新与部署」每天 08:30（Asia/Shanghai）触发，执行 `bash daily_update.sh`：

1. 抓数并重算三张页面与门户首页；
2. 把 `data/`、`web/` 的变更提交到 git（不自动 push）；
3. 推送一条飞书日报；
4. 重新部署到 https://37kpkjj3lvulefh33ap.traesite.cn 。

报告按 ISO 周命名，日更会覆盖当周同名文件，即 `reports/2026-W47.md` 始终是该周最新一份快照。

## 文件说明

```text
README.md                 项目总览、共享口径、文档索引
docs/pages/erp.md         股债性价比页契约（web/index.html）
docs/pages/turnover.md    两市成交额页契约（web/turnover.html）
docs/pages/leverage.md    杠杆资金页契约（web/leverage.html）
docs/pages/weekly-report.md  周报与飞书推送契约
config.yaml               参数：指数、阈值、成交额档位、杠杆档位、推送
monitor.py                主脚本（抓数 → 计算 → 报告 → 网页 → 推送）
turnover.py               两市成交额页面（换手率标准化 → web/turnover.html）
leverage.py               杠杆资金（融资余额 ÷ 流通市值 → web/leverage.html）
portal.py                 门户首页生成器（读三张页面的 DATA → web/portal.html）
daily_update.sh           每日更新：抓数 → 页面 → git 提交 → 重新部署
data/                     原始序列与周度台账
reports/                  每周周报与走势图
web/                      交互页面（*_template.html 是模板，portal.html / index.html / turnover.html / leverage.html 是生成物）
analysis/                 一次性分析脚本（利率归因、口径对比）
```

## 边界

- 单一指标，不能替代仓位管理与风险预算。
- 分位对窗口敏感：5 年与 10 年结论可能不同，报告同时给出。
- 10Y 国债已在 1.6% 附近，利率对任何比值型指标都有放大效应；400bp 参考线的历史含义会随时间弱化。
- 免费数据源无 SLA，只适合个人监控，不适合对外披露。
- 页面是静态快照：本地文件要运行脚本才更新；线上站点由每日 08:30 的任务重新生成并部署。

## 契约维护

新增一个页面时：

1. 在 `docs/pages/` 新建 `<slug>.md`，按下面的骨架写完整，再更新本页「文档地图」。
2. 在 `config.yaml` / 生成脚本中加数据与参数，payload 字段必须与契约一致。
3. 模板里给关键区域加稳定 `id`，并把 id、交互行为写进契约。

每份契约的固定骨架：

```text
文档信息 → 页面定位 → 页面结构 → 数据契约 → 交互契约
→ 计算契约 → 文案与配色 → 边界 → 验收清单 → 变更记录
```

以下改动都算**契约变更**，必须同一次提交里同步文档与代码：

- payload 字段增删、类型、单位、空值规则
- 阈值、分位窗口、样本起点、信号判定逻辑
- 新增/改名卡片、图例、按钮、Tooltip 字段
- 颜色语义、文案口径、页面标题与副标题
- 输出路径、模板文件名、推送格式

每次契约变更在对应文档末尾「变更记录」加一行：日期 · 改了什么 · 为什么。
