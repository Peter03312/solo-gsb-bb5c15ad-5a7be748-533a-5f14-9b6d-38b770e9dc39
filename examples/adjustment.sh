#!/usr/bin/env bash
# 现场外部频点占用调整：预演 + 快照确认 请求示例（需服务已启动，默认 :8000）
#
# 现场调整外部占用前先“预演”：给出待撤销占用编号与待登记占用（频点 +
# 左闭右开整数分钟时段），服务返回覆盖当前发布版、设备组/干扰配置与占用的
# 快照标识、当前版变更后违反约束的组，以及保留当前版锁定、改频组数最少
# （并列按组号升序频点序列字典序最小）的替代分配。确认携带同一快照标识与
# 同一变更：状态或变更有变化即 409，否则原子完成占用变更，仅当分配改变时
# 发布不可变新版，返回最终占用与版号。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
pp() { python3 -m json.tool; }

echo "== 1. 建场次（频点 1..6）+ 三组（a[0,120) b[60,180) c[120,240)）+ 干扰 a-b/b-c 间距3 =="
SID=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"name":"占用调整演示","frequencies":[1,2,3,4,5,6]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G1=$(curl -s -X POST "$BASE/sessions/$SID/groups" -H 'Content-Type: application/json' \
  -d '{"name":"a","start":0,"end":120}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G2=$(curl -s -X POST "$BASE/sessions/$SID/groups" -H 'Content-Type: application/json' \
  -d '{"name":"b","start":60,"end":180}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G3=$(curl -s -X POST "$BASE/sessions/$SID/groups" -H 'Content-Type: application/json' \
  -d '{"name":"c","start":120,"end":240}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/sessions/$SID/interferences" -H 'Content-Type: application/json' \
  -d "{\"group_a\":$G1,\"group_b\":$G2,\"min_separation\":3}" >/dev/null
curl -s -X POST "$BASE/sessions/$SID/interferences" -H 'Content-Type: application/json' \
  -d "{\"group_a\":$G2,\"group_b\":$G3,\"min_separation\":3}" >/dev/null

echo "== 2. 首次发布：v1 = (a=1, b=4, c=1) =="
curl -s -X POST "$BASE/sessions/$SID/plans" -H 'Content-Type: application/json' \
  -d '{"base_version":0,"locks":[]}' | pp

echo "== 3. 预演：登记占用 频点4 [30,150)（与三组都相交；组b 当前=4 将违规） =="
PREVIEW=$(curl -s -X POST "$BASE/sessions/$SID/occupations/adjustment/preview" \
  -H 'Content-Type: application/json' \
  -d '{"revoke":[],"register":[{"frequency":4,"start":30,"end":150}]}')
echo "$PREVIEW" | pp
SNAP=$(echo "$PREVIEW" | python3 -c 'import json,sys; print(json.load(sys.stdin)["snapshot_id"])')
echo ">> 快照标识 snapshot_id=$SNAP（预演不落任何占用/版本）"

echo "== 4. 确认同一变更：原子登记占用，并因分配改变发布不可变新版 v2 =="
curl -s -X POST "$BASE/sessions/$SID/occupations/adjustment/confirm" \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot_id\":$SNAP,\"revoke\":[],\"register\":[{\"frequency\":4,\"start\":30,\"end\":150}]}" | pp

echo "== 5. 预演撤销该占用：当前版恢复可行，无需改频 -> 确认后不发新版 =="
OCC_ID=$(curl -s "$BASE/sessions/$SID/occupations" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
SNAP2=$(curl -s -X POST "$BASE/sessions/$SID/occupations/adjustment/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revoke\":[$OCC_ID],\"register\":[]}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["snapshot_id"])')
curl -s -X POST "$BASE/sessions/$SID/occupations/adjustment/confirm" \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot_id\":$SNAP2,\"revoke\":[$OCC_ID],\"register\":[]}" | pp

echo "== 6. 非法变更整次拒绝：未知/重复撤销编号 + 不可用频点 + 无效时段（400，不留变更） =="
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/sessions/$SID/occupations/adjustment/preview" \
  -H 'Content-Type: application/json' \
  -d '{"revoke":[999999,999999],"register":[{"frequency":77,"start":20,"end":20}]}'

echo "== 7. 快照过期：预演后占用状态变化，再用旧快照确认 -> 409 =="
SNAP3=$(curl -s -X POST "$BASE/sessions/$SID/occupations/adjustment/preview" \
  -H 'Content-Type: application/json' \
  -d '{"revoke":[],"register":[{"frequency":6,"start":0,"end":30}]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["snapshot_id"])')
curl -s -X POST "$BASE/sessions/$SID/occupations" -H 'Content-Type: application/json' \
  -d '{"frequency":5,"start":0,"end":30}' >/dev/null   # 经既有接口改动占用
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/sessions/$SID/occupations/adjustment/confirm" \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot_id\":$SNAP3,\"revoke\":[],\"register\":[{\"frequency\":6,\"start\":0,\"end\":30}]}"

echo "== 8. 无解：锁定组被迫离开锁定频点时 422，说明锁定/干扰/占用且不允许确认 =="
SID2=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d '{"name":"无解演示","frequencies":[1,2]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
GX=$(curl -s -X POST "$BASE/sessions/$SID2/groups" -H 'Content-Type: application/json' \
  -d '{"name":"x","start":0,"end":60}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/sessions/$SID2/plans" -H 'Content-Type: application/json' \
  -d "{\"base_version\":0,\"locks\":[{\"group_id\":$GX,\"frequency\":1}]}" >/dev/null
curl -s -w '\nHTTP %{http_code}\n' -X POST \
  "$BASE/sessions/$SID2/occupations/adjustment/preview" \
  -H 'Content-Type: application/json' \
  -d '{"revoke":[],"register":[{"frequency":1,"start":0,"end":60},{"frequency":2,"start":0,"end":60}]}'
