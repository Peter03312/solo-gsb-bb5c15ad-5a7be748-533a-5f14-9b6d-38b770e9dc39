#!/usr/bin/env bash
# 现场实测频点批次 请求示例（需服务已启动，默认 http://localhost:8000）
#
# 现场人员对某场次完成一次完整实测后，提交每个设备组的实测整数频点，服务将其
# 与指定发布版及当前约束核对：偏离发布版的组、不可用频点、时段相交时违反最小
# 间距的干扰关系，以及与外部占用冲突的组和占用编号。批次及判定依据（含占用
# 快照）固化，后续撤销占用或发布新版本都不改变历史结果。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

echo "== 1. 创建场次（可用频点 1..6），录入三组与干扰关系并发布 v1 =="
SID=$(curl -s -X POST "$BASE/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"name": "实测演示", "frequencies": [1, 2, 3, 4, 5, 6]}' \
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
curl -s -X POST "$BASE/sessions/$SID/plans" \
  -H 'Content-Type: application/json' -d '{"base_version": 0, "locks": []}' >/dev/null
echo "session=$SID groups: 音响=$G1 灯光=$G2 返送=$G3（v1 分配：音响=1 灯光=4 返送=1）"

echo "== 2. 登记一条外部占用：频点 4 的 [30,150)（与音响、灯光时段相交） =="
curl -s -X POST "$BASE/sessions/$SID/occupations" \
  -H 'Content-Type: application/json' \
  -d '{"frequency": 4, "start": 30, "end": 150}' | python3 -m json.tool

echo "== 3. 提交实测批次 1：音响=4（偏离且撞占用）、灯光=4（撞占用且与音响间距违规）、返送=1 =="
curl -s -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 1, \"batch_no\": 1, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 4},
    {\"group_id\": $G2, \"frequency\": 4},
    {\"group_id\": $G3, \"frequency\": 1}]}" | python3 -m json.tool

echo "== 4. 相同批次号 + 相同内容重试：幂等返回原结果（HTTP 200） =="
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 1, \"batch_no\": 1, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 4},
    {\"group_id\": $G2, \"frequency\": 4},
    {\"group_id\": $G3, \"frequency\": 1}]}"

echo "== 5. 相同批次号 + 不同内容：冲突（HTTP 409 batch_conflict） =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 1, \"batch_no\": 1, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 1},
    {\"group_id\": $G2, \"frequency\": 4},
    {\"group_id\": $G3, \"frequency\": 1}]}"

echo "== 6. 校验失败示例：漏组 / 重复组 / 未知发布版 / 批次号过低 =="
curl -s -o /dev/null -w '漏组         -> HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 1, \"batch_no\": 2, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 1}, {\"group_id\": $G2, \"frequency\": 4}]}"
curl -s -o /dev/null -w '重复组       -> HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 1, \"batch_no\": 2, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 1}, {\"group_id\": $G1, \"frequency\": 2},
    {\"group_id\": $G2, \"frequency\": 4}, {\"group_id\": $G3, \"frequency\": 1}]}"
curl -s -o /dev/null -w '未知发布版   -> HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 99, \"batch_no\": 2, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 1}, {\"group_id\": $G2, \"frequency\": 4},
    {\"group_id\": $G3, \"frequency\": 1}]}"
# 批次 1 已是最新批次：再用批次号 1 提交“不同内容”按过期批次拒绝（409 stale_batch）
curl -s -o /dev/null -w '批次号过低   -> HTTP %{http_code}\n' \
  -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 1, \"batch_no\": 1, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 1}, {\"group_id\": $G2, \"frequency\": 4},
    {\"group_id\": $G3, \"frequency\": 1}]}"

echo "== 7. 批次 3：音响=99 不在可用集合内，也如实记录并标明异常 =="
curl -s -X POST "$BASE/sessions/$SID/measurements" \
  -H 'Content-Type: application/json' \
  -d "{\"plan_version\": 1, \"batch_no\": 3, \"readings\": [
    {\"group_id\": $G1, \"frequency\": 99},
    {\"group_id\": $G2, \"frequency\": 4},
    {\"group_id\": $G3, \"frequency\": 1}]}" | python3 -m json.tool

echo "== 8. 读取：批次列表 / 最新批次 / 指定批次（结果含固化的占用快照） =="
curl -s "$BASE/sessions/$SID/measurements" \
  | python3 -c 'import json,sys; print("批次号:", [b["batch_no"] for b in json.load(sys.stdin)])'
curl -s "$BASE/sessions/$SID/measurements/latest" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("最新批次:", d["batch_no"], "异常:", d["anomalies"])'
curl -s "$BASE/sessions/$SID/measurements/1" | python3 -m json.tool
