#!/usr/bin/env bash
# 频点协调服务请求示例（需服务已启动，默认 http://localhost:8000）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

echo "== 1. 创建场次（可用频点 1..6） =="
SESSION=$(curl -s -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"name": "周六演出", "frequencies": [1, 2, 3, 4, 5, 6]}')
echo "$SESSION"
SID=$(echo "$SESSION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 2. 录入设备组（时段左闭右开） =="
curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "音响", "start": 0, "end": 120}'; echo
curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "灯光", "start": 60, "end": 180}'; echo
curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "返送", "start": 120, "end": 240}'; echo

echo "== 3. 录入干扰关系（组 1 与组 2 最小间距 3） =="
curl -s -X POST "$BASE/sessions/$SID/interferences" \
  -H 'Content-Type: application/json' \
  -d '{"group_a": 1, "group_b": 2, "min_separation": 3}'; echo

echo "== 4. 首次规划并发布（base_version=0） =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 0, "locks": []}'; echo

echo "== 5. 基于版本 1 再次发布，锁定组 1 使用频点 2 =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 1, "locks": [{"group_id": 1, "frequency": 2}]}'; echo

echo "== 6. 读取发布版 2 及其与版本 1 的差异 =="
curl -s "$BASE/sessions/$SID/plans/2"; echo
curl -s "$BASE/sessions/$SID/plans/2/diff"; echo

echo "== 7. 用旧版本号发布会得到 409 冲突 =="
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 1, "locks": []}'
