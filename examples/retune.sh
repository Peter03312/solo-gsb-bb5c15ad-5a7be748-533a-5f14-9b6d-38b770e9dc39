#!/usr/bin/env bash
# 逐组切换路线请求示例（需服务已启动，默认 http://localhost:8000）
#
# 本脚本独立创建一个场次，录入三组串行时段与两条相邻干扰关系，发布两个
# 可行的分配版本，然后请求从版本 1 到版本 2 的逐组切换路线。该查询为只读，
# 不会创建或修改任何发布版。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

echo "== 1. 创建场次（可用频点 1..7） =="
SESSION=$(curl -s -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"name": "逐组切换演示", "frequencies": [1, 2, 3, 4, 5, 6, 7]}')
echo "$SESSION"
SID=$(echo "$SESSION" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 2. 录入三个设备组（时段左闭右开；组编号为服务返回的真实 id） =="
GA=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "a", "start": 0, "end": 120}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
GB=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "b", "start": 60, "end": 180}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
GC=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "c", "start": 120, "end": 240}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "groups: a=$GA b=$GB c=$GC"

echo "== 3. 录入相邻干扰关系（a-b、b-c 间距 3；a、c 时段不相交不生效） =="
curl -s -X POST "$BASE/sessions/$SID/interferences" \
  -H 'Content-Type: application/json' \
  -d "{\"group_a\": $GA, \"group_b\": $GB, \"min_separation\": 3}"; echo
curl -s -X POST "$BASE/sessions/$SID/interferences" \
  -H 'Content-Type: application/json' \
  -d "{\"group_a\": $GB, \"group_b\": $GC, \"min_separation\": 3}"; echo

echo "== 4. 发布版本 1：(a=1, b=4, c=1) =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d "{\"base_version\": 0, \"locks\": [
    {\"group_id\": $GA, \"frequency\": 1},
    {\"group_id\": $GB, \"frequency\": 4},
    {\"group_id\": $GC, \"frequency\": 1}]}"; echo

echo "== 5. 发布版本 2：(a=4, b=1, c=4) =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d "{\"base_version\": 1, \"locks\": [
    {\"group_id\": $GA, \"frequency\": 4},
    {\"group_id\": $GB, \"frequency\": 1},
    {\"group_id\": $GC, \"frequency\": 4}]}"; echo

echo "== 6. 请求版本 1 -> 版本 2 的逐组切换路线（只读） =="
curl -s -X POST "$BASE/sessions/$SID/retune" \
  -H 'Content-Type: application/json' \
  -d '{"start_version": 1, "target_version": 2, "fixed_groups": []}' \
  | python3 -m json.tool

echo "== 7. 指定不可调频组（示例：b 组两版频点不同，会被 422 拒绝） =="
curl -s -o /tmp/retune_resp.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/retune" \
  -H 'Content-Type: application/json' \
  -d "{\"start_version\": 1, \"target_version\": 2, \"fixed_groups\": [$GB]}"
cat /tmp/retune_resp.json | python3 -m json.tool
