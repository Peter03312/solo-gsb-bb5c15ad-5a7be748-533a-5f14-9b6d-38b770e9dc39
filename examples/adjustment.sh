#!/usr/bin/env bash
# 现场外部频点占用调整：先预演、再原子确认（需服务已启动，默认 http://localhost:8000）
#
# 现场调整外部频点占用前，按场次以待撤销的占用编号、待登记频点与左闭右开
# 整数分钟时段预演；服务返回覆盖“当前发布版 + 设备组/干扰配置 + 当前占用”
# 的快照标识、当前版在变更后违反约束的组，以及保留当前版锁定的替代分配
# （先最少改频组数，再按组号升序频点序列取字典序最小）。
# 确认携带快照标识与同一变更：状态变化即冲突，否则原子完成占用变更；
# 仅当替代分配确有改频时发布不可变新版本。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

echo "== 1. 场次 频点1..6：音响[0,120)、灯光[60,180)、返送[120,240)，相邻间距3 =="
SID=$(curl -s -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"name": "现场占用调整演示", "frequencies": [1, 2, 3, 4, 5, 6]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G1=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "音响", "start": 0, "end": 120}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G2=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "灯光", "start": 60, "end": 180}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G3=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "返送", "start": 120, "end": 240}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/sessions/$SID/interferences" \
  -H 'Content-Type: application/json' \
  -d "{\"group_a\": $G1, \"group_b\": $G2, \"min_separation\": 3}" >/dev/null
curl -s -X POST "$BASE/sessions/$SID/interferences" \
  -H 'Content-Type: application/json' \
  -d "{\"group_a\": $G2, \"group_b\": $G3, \"min_separation\": 3}" >/dev/null

echo "== 2. 发布当前版 v1：音响=1、灯光=4、返送=1 =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 0, "locks": []}' | python3 -m json.tool

echo "== 3. 登记一条占用（频点1 [0,90)），取得待撤销编号 =="
OCC=$(curl -s -X POST "$BASE/sessions/$SID/occupations" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 1, "start": 0, "end": 90}')
echo "$OCC"
OLD_ID=$(echo "$OCC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 4. 预演：撤销频点1占用 + 登记频点4占用[30,150) =="
PREVIEW=$(curl -s -X POST "$BASE/sessions/$SID/occupations/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revoke\": [$OLD_ID],
       \"register\": [{\"frequency\": 4, \"start\": 30, \"end\": 150}]}")
echo "$PREVIEW" | python3 -m json.tool
TOKEN=$(echo "$PREVIEW" | python3 -c 'import json,sys; print(json.load(sys.stdin)["snapshot_token"])')

echo "== 5. 非法变更整次拒绝（未知/重复撤销、不可用频点、无效时段） -> 400 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/sessions/$SID/occupations/preview" \
  -H 'Content-Type: application/json' \
  -d '{"revoke": [999999, 999999],
       "register": [
         {"frequency": 88, "start": 0, "end": 10},
         {"frequency": 2, "start": 30, "end": 30}]}'

echo "== 6. 携带快照标识与同一变更确认（原子撤销+登记；灯光改频则发布 v2） =="
curl -s -X POST "$BASE/sessions/$SID/occupations/confirm" \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot_token\": \"$TOKEN\",
       \"revoke\": [$OLD_ID],
       \"register\": [{\"frequency\": 4, \"start\": 30, \"end\": 150}]}" \
  | python3 -m json.tool

echo "== 7. 再次使用同一令牌 -> 409 snapshot_conflict（状态已变，令牌一次性） =="
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/sessions/$SID/occupations/confirm" \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot_token\": \"$TOKEN\",
       \"revoke\": [$OLD_ID],
       \"register\": [{\"frequency\": 4, \"start\": 30, \"end\": 150}]}"

echo "== 8. 核对最终占用与最新发布版 =="
curl -s "$BASE/sessions/$SID/occupations" | python3 -m json.tool
curl -s "$BASE/sessions/$SID/plans" | python3 -m json.tool

echo "== 9. 无发布版的场次预演 -> 422 no_published_plan =="
EMPTY=$(curl -s -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"name": "无发布版", "frequencies": [1, 2]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/sessions/$EMPTY/occupations/preview" \
  -H 'Content-Type: application/json' \
  -d '{"register": [{"frequency": 1, "start": 0, "end": 10}]}'
