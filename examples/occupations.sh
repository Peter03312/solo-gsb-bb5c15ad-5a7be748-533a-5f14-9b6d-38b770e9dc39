#!/usr/bin/env bash
# 场次频点占用 请求示例（需服务已启动，默认 http://localhost:8000）
#
# 现场其他设备会在指定时段占用场次频点。本脚本演示：登记占用、查询当前占用、
# 占用对规划发布（无解 422 并指出相关占用）的影响，以及撤销占用。
# 已发布版不可变，不会因占用变化自动重写；占用登记/撤销与发布按场次串行，
# 发布始终基于一致的当前占用状态。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

echo "== 1. 创建场次（可用频点 1..5），录入组 音响[0,120)、灯光[60,180) =="
SID=$(curl -s -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"name": "占用演示", "frequencies": [1, 2, 3, 4, 5]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G1=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "音响", "start": 0, "end": 120}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G2=$(curl -s -X POST "$BASE/sessions/$SID/groups" \
  -H 'Content-Type: application/json' -d '{"name": "灯光", "start": 60, "end": 180}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/sessions/$SID/interferences" \
  -H 'Content-Type: application/json' \
  -d "{\"group_a\": $G1, \"group_b\": $G2, \"min_separation\": 3}" >/dev/null

echo "== 2. 首次发布（base_version=0）：组1=1、组2=4 =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 0, "locks": []}' | python3 -m json.tool

echo "== 3. 登记占用：外部设备在 [30,150) 占用频点 4（与两个组都相交） =="
OCC=$(curl -s -X POST "$BASE/sessions/$SID/occupations" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 4, "start": 30, "end": 150}')
echo "$OCC"
OCC_ID=$(echo "$OCC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 3a. 非法登记被整次拒绝（频点不属于场次 / 起点不早于终点） =="
curl -s -o /dev/null -w '占用非场次频点 -> HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/occupations" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 99, "start": 0, "end": 30}'
curl -s -o /dev/null -w '起点不早于终点   -> HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/occupations" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 4, "start": 60, "end": 60}'

echo "== 4. 查询当前占用 =="
curl -s "$BASE/sessions/$SID/occupations" | python3 -m json.tool

echo "== 5. 再发布：规划避开被占用频点（组 1=1，组 2 不能用 4，改到 5） =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 1, "locks": []}' | python3 -m json.tool

echo "== 6. 再登记频点 5 在 [60,180) 被占用：组 2 的 4、5 均不可用 -> 422 =="
curl -s -X POST "$BASE/sessions/$SID/occupations" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 5, "start": 60, "end": 180}' >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 2, "locks": []}'

echo "== 7. 撤销未知记录被拒绝(400)；撤销占用记录 $OCC_ID（频点4） =="
curl -s -o /dev/null -w '撤销未知记录 -> HTTP %{http_code}\n' \
  -X DELETE "$BASE/sessions/$SID/occupations/999999"
curl -s -X DELETE "$BASE/sessions/$SID/occupations/$OCC_ID" | python3 -m json.tool

echo "== 8. 撤销后基于版本 2 重新发布：组 2 可回到 4（最小改频） =="
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' \
  -d '{"base_version": 2, "locks": []}' | python3 -m json.tool

echo "== 9. 查询当前占用（已撤销记录不再出现，仅剩频点 5 的占用） =="
curl -s "$BASE/sessions/$SID/occupations" | python3 -m json.tool
