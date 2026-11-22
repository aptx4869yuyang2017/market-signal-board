#!/usr/bin/env bash
# 每个交易日：抓数 → 重算四张看板与门户首页 → 提交 git → 重新部署到 traesite
# 用法：bash daily_update.sh（定时任务也是调它）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

SITE_ID="37kpkjj3lvulefh33ap"
PY="/opt/homebrew/anaconda3/bin/python3"
[ -x "$PY" ] || PY="python3"

echo "[1/5] 抓数并重算页面 $(date '+%F %T')"
"$PY" monitor.py

echo "[2/5] 提交数据、页面与记忆"
git add data web memory
if ! git diff --cached --quiet; then
  git commit -m "chore: 每日数据更新 $(date '+%F')"
fi

echo "[3/5] 准备部署源"
tmp_root="${TMPDIR:-/tmp}"
src="$(mktemp -d "${tmp_root%/}/trae-deploy.XXXXXX")"
cfg="$(mktemp "${tmp_root%/}/trae-exec.XXXXXX.json")"
cleanup() { rm -rf "$src" "$cfg"; }
trap cleanup EXIT

mkdir -p "$src/public"
"$PY" - "$ROOT/web" "$src/public" <<'PY'
import pathlib
import sys

web, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])


def link_fix(text: str) -> str:
    """本地 portal.html 是首页、index.html 是 ERP 页；线上改成 index.html / erp.html。"""
    text = text.replace('href="./index.html"', 'href="./erp.html"')
    return text.replace('href="./portal.html"', 'href="./index.html"')


(out / "index.html").write_text(
    link_fix((web / "portal.html").read_text(encoding="utf-8")), encoding="utf-8"
)
(out / "erp.html").write_text(
    link_fix((web / "index.html").read_text(encoding="utf-8")), encoding="utf-8"
)
for name in ("turnover.html", "leverage.html", "sentiment.html"):
    (out / name).write_text(
        link_fix((web / name).read_text(encoding="utf-8")), encoding="utf-8"
    )
(out / "echarts.min.js").write_bytes((web / "echarts.min.js").read_bytes())
PY

printf '{}' > "$cfg"

SKILL_DIR="$(ls -d "$HOME/.trae-cn/plugins/trae-remote-official/deployment"/*/skills/deployment 2>/dev/null | sort -V | tail -1)"
CLI="$SKILL_DIR/scripts/trae-deploy"
[ -f "$CLI" ] || { echo "找不到 trae-deploy CLI: $SKILL_DIR" >&2; exit 1; }

echo "[4/5] 创建部署"
result="$(sh "$CLI" deployments create --site-id "$SITE_ID" \
  --title "AI Invest 每日更新 $(date '+%F')" \
  --description "每日更新：重新抓取指数 PE、10Y 国债收益率、两市成交额、融资融券与反向情绪数据，重算 ERP / 成交额 / 杠杆 / 反向情绪四张看板与门户首页后重新发布。" \
  --type static-web --source-dir "$src" --execution-config "$cfg" --app-tags 234)"
dep_id="$(printf '%s' "$result" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d["data"]["deployment_id"] if d.get("ok") else "")')"
[ -n "$dep_id" ] || { echo "$result" >&2; exit 1; }

echo "[5/5] 等待部署 $dep_id"
sh "$CLI" deployments wait --deployment-id "$dep_id" --timeout-seconds 300
