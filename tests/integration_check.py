#!/usr/bin/env python3
"""端到端集成检查：嵌入式 PostgreSQL + 真实 uvicorn 进程。

开发期使用，不属于生产部署。运行方式：
    pip install pgserver httpx uvicorn fastapi psycopg2-binary
    python tests/integration_check.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

import httpx

BASE = "http://127.0.0.1:18000"
_failures: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def main() -> int:
    import pgserver

    pgdir = tempfile.mkdtemp(prefix="pgtest-")
    srv = pgserver.get_server(pgdir)
    os.environ["DATABASE_URL"] = srv.get_uri()
    print("postgres:", os.environ["DATABASE_URL"])

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "18000"],
        env={**os.environ})
    try:
        for _ in range(60):
            try:
                httpx.get(f"{BASE}/docs", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("server failed to start")
            return 1

        c = httpx.Client(base_url=BASE, timeout=10)

        # 1. 创建场次
        r = c.post("/sessions", json={"name": "周六演出",
                                      "frequencies": [1, 2, 3, 4, 5, 6]})
        check("创建场次", r.status_code == 201, r.text)
        sid = r.json()["id"]

        r = c.post("/sessions", json={"name": "空频点", "frequencies": []})
        check("空频点集合被拒绝(422 校验)", r.status_code == 422)

        # 2. 录入设备组
        r = c.post(f"/sessions/{sid}/groups",
                   json={"name": "音响", "start": 0, "end": 120})
        g1 = r.json()["id"]
        check("录入组1", r.status_code == 201, r.text)
        r = c.post(f"/sessions/{sid}/groups",
                   json={"name": "灯光", "start": 60, "end": 180})
        g2 = r.json()["id"]
        r = c.post(f"/sessions/{sid}/groups",
                   json={"name": "返送", "start": 120, "end": 240})
        g3 = r.json()["id"]
        check("录入组2/组3", r.status_code == 201)

        r = c.post(f"/sessions/{sid}/groups",
                   json={"name": "坏时段", "start": 10, "end": 10})
        check("起点不早于终点被拒绝(400)", r.status_code == 400, r.text)

        # 3. 干扰关系
        r = c.post(f"/sessions/{sid}/interferences",
                   json={"group_a": g1, "group_b": g2, "min_separation": 3})
        check("录入干扰关系", r.status_code == 201, r.text)
        r = c.post(f"/sessions/{sid}/interferences",
                   json={"group_a": g1, "group_b": 9999, "min_separation": 2})
        check("引用未知组被拒绝(400)", r.status_code == 400, r.text)
        r = c.post(f"/sessions/{sid}/interferences",
                   json={"group_a": g1, "group_b": g2, "min_separation": 0})
        check("非正间距被拒绝(400)", r.status_code == 400)

        # 4. 首次发布（改频数计零，字典序最小）
        r = c.post(f"/sessions/{sid}/plans",
                   json={"base_version": 0, "locks": []})
        check("首次发布", r.status_code == 201, r.text)
        body = r.json()
        check("首版改频数为 0", body["changes"] == 0)
        a = {x["group_id"]: x["frequency"] for x in body["assignments"]}
        check("相交干扰组满足间距", abs(a[g1] - a[g2]) >= 3, str(a))
        check("字典序最小", (a[g1], a[g2], a[g3]) == (1, 4, 1), str(a))

        # 5. 旧版本发布 → 409
        r = c.post(f"/sessions/{sid}/plans",
                   json={"base_version": 0, "locks": []})
        check("旧版本请求返回冲突(409)", r.status_code == 409, r.text)

        # 6. 锁定不可用频点 → 400，整次拒绝
        r = c.post(f"/sessions/{sid}/plans",
                   json={"base_version": 1,
                         "locks": [{"group_id": g1, "frequency": 99}]})
        check("锁定不可用频点被拒绝(400)", r.status_code == 400, r.text)
        r = c.post(f"/sessions/{sid}/plans",
                   json={"base_version": 1,
                         "locks": [{"group_id": 9999, "frequency": 1}]})
        check("锁定未知组被拒绝(400)", r.status_code == 400)

        # 7. 无解：锁定 g1=1 且 g2=2（间距 3）→ 422，发布版不变
        r = c.post(f"/sessions/{sid}/plans",
                   json={"base_version": 1, "locks": [
                       {"group_id": g1, "frequency": 1},
                       {"group_id": g2, "frequency": 2}]})
        check("无解返回 422", r.status_code == 422, r.text)
        detail = r.json()["detail"]
        check("无解说明涉及锁定组",
              detail["cause"] == "locks"
              and {l["group_id"] for l in detail["locks"]} == {g1, g2},
              str(detail))
        r = c.get(f"/sessions/{sid}/plans")
        check("无解后发布版不变", [p["version"] for p in r.json()] == [1])

        # 8. 正常第二版：锁定 g1=2，检验最小改频
        r = c.post(f"/sessions/{sid}/plans",
                   json={"base_version": 1,
                         "locks": [{"group_id": g1, "frequency": 2}]})
        check("第二次发布", r.status_code == 201, r.text)
        body = r.json()
        a2 = {x["group_id"]: x["frequency"] for x in body["assignments"]}
        check("锁定生效", a2[g1] == 2, str(a2))
        check("最小改频", a2[g2] == 5 and a2[g3] == 1
              and body["changes"] == 2, f"{a2} changes={body['changes']}")

        # 9. 读取发布版与差异
        r = c.get(f"/sessions/{sid}/plans/2")
        check("读取发布版 2", r.status_code == 200
              and r.json()["version"] == 2, r.text)
        r = c.get(f"/sessions/{sid}/plans/2/diff")
        diff = r.json()
        check("差异：改频组正确",
              {d["group_id"] for d in diff["changed"]} == {g1, g2}
              and diff["unchanged"] == 1, str(diff))
        r = c.get(f"/sessions/{sid}/plans/1/diff")
        check("首版差异全部为新增",
              len(r.json()["added"]) == 3 and r.json()["changed"] == [])
        r = c.get(f"/sessions/{sid}/plans/99")
        check("读取不存在的发布版(404)", r.status_code == 404)

        # 10. 并发发布：只有一个可成功
        results = []

        def publish():
            rr = httpx.post(f"{BASE}/sessions/{sid}/plans",
                            json={"base_version": 2, "locks": []},
                            timeout=30)
            results.append(rr.status_code)

        threads = [threading.Thread(target=publish) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("并发发布只有一个成功",
              results.count(201) == 1 and results.count(409) == 5,
              str(results))
        r = c.get(f"/sessions/{sid}/plans")
        check("并发后版本号为 3", [p["version"] for p in r.json()] == [1, 2, 3])

        # ---------------------------------------------------------------- #
        # 逐组切换路线（只读；为获得冻结/缓冲场景另建一个场次）
        # ---------------------------------------------------------------- #
        r = c.post("/sessions", json={"name": "切换演示",
                                      "frequencies": [1, 2, 3, 4, 5, 6, 7]})
        rid = r.json()["id"]
        check("创建切换场次", r.status_code == 201, r.text)
        # 三组串行时段：组1[0,120) 组2[60,180) 组3[120,240)
        rg = []
        for name, s, e in [("a", 0, 120), ("b", 60, 180), ("c", 120, 240)]:
            r = c.post(f"/sessions/{rid}/groups",
                       json={"name": name, "start": s, "end": e})
            rg.append(r.json()["id"])
        ga, gb, gc = rg  # 组编号为全局自增，不假设从 1 开始
        # 相邻干扰间距 3：边 ga-gb、gb-gc 生效，ga-gc 不相交不生效
        c.post(f"/sessions/{rid}/interferences",
               json={"group_a": ga, "group_b": gb, "min_separation": 3})
        c.post(f"/sessions/{rid}/interferences",
               json={"group_a": gb, "group_b": gc, "min_separation": 3})

        # v1：锁定得到 (ga=1, gb=4, gc=1)
        r = c.post(f"/sessions/{rid}/plans",
                   json={"base_version": 0,
                         "locks": [{"group_id": ga, "frequency": 1},
                                   {"group_id": gb, "frequency": 4},
                                   {"group_id": gc, "frequency": 1}]})
        check("切换场次 v1 发布", r.status_code == 201, r.text)
        # v2：锁定得到 (ga=4, gb=1, gc=4)
        r = c.post(f"/sessions/{rid}/plans",
                   json={"base_version": 1,
                         "locks": [{"group_id": ga, "frequency": 4},
                                   {"group_id": gb, "frequency": 1},
                                   {"group_id": gc, "frequency": 4}]})
        check("切换场次 v2 发布", r.status_code == 201, r.text)

        # 正常求路线
        r = c.post(f"/sessions/{rid}/retune",
                   json={"start_version": 1, "target_version": 2,
                         "fixed_groups": []})
        check("切换路线 200", r.status_code == 200, r.text)
        route = r.json()
        check("最短 4 步", route["steps_count"] == 4, str(route["steps_count"]))
        expected = [(gb, 7), (ga, 4), (gc, 4), (gb, 1)]
        actual = [(s["group_id"], s["frequency"]) for s in route["steps"]]
        check("字典序最小路线", actual == expected, str(actual))
        # 每一步后的完整分配均满足生效约束，首步借用临时频点 7
        check("首步为临时频点", route["steps"][0]["temporary"] is True
              and route["steps"][0]["frequency"] == 7, str(route["steps"][0]))
        check("后续步骤直达目标频点",
              all(s["temporary"] is False for s in route["steps"][1:]))
        cur = {ga: 1, gb: 4, gc: 1}
        for st in route["steps"]:
            cur[st["group_id"]] = st["frequency"]
            got = {a["group_id"]: a["frequency"] for a in st["assignment"]}
            assert got == cur
            assert abs(got[ga] - got[gb]) >= 3 and abs(got[gb] - got[gc]) >= 3
        check("路线终点即目标分配",
              cur == {ga: 4, gb: 1, gc: 4}, str(cur))

        # 0 步路线：同版本
        r = c.post(f"/sessions/{rid}/retune",
                   json={"start_version": 2, "target_version": 2})
        check("同版本 0 步", r.status_code == 200
              and r.json()["steps_count"] == 0, r.text)

        # 引用不存在的版本 -> 400
        r = c.post(f"/sessions/{rid}/retune",
                   json={"start_version": 1, "target_version": 99})
        check("目标版本非法 400", r.status_code == 400
              and r.json()["detail"]["error"] == "invalid_reference", r.text)
        # 引用未知 / 重复的不可调频组 -> 400
        r = c.post(f"/sessions/{rid}/retune",
                   json={"start_version": 1, "target_version": 2,
                         "fixed_groups": [999999]})
        check("未知不可调频组 400", r.status_code == 400, r.text)
        r = c.post(f"/sessions/{rid}/retune",
                   json={"start_version": 1, "target_version": 2,
                         "fixed_groups": [ga, ga]})
        check("重复不可调频组 400", r.status_code == 400, r.text)
        # 不可调频组两版频点不同 -> 422
        r = c.post(f"/sessions/{rid}/retune",
                   json={"start_version": 1, "target_version": 2,
                         "fixed_groups": [ga]})
        check("不可调频组需改频 422", r.status_code == 422
              and r.json()["detail"]["error"] == "fixed_group_conflict",
              r.text)

        # 端点在当前规则下不可行：新增组gd并发布 v3 后，v2 缺少 gd
        r = c.post(f"/sessions/{rid}/groups",
                   json={"name": "d", "start": 0, "end": 240})
        gd = r.json()["id"]
        c.post(f"/sessions/{rid}/interferences",
               json={"group_a": ga, "group_b": gd, "min_separation": 1})
        r = c.post(f"/sessions/{rid}/plans",
                   json={"base_version": 2,
                         "locks": [{"group_id": ga, "frequency": 4},
                                   {"group_id": gb, "frequency": 1},
                                   {"group_id": gc, "frequency": 4},
                                   {"group_id": gd, "frequency": 3}]})
        check("含组gd的 v3 发布", r.status_code == 201, r.text)
        # v2 缺少 gd -> 两版组集合不同
        r = c.post(f"/sessions/{rid}/retune",
                   json={"start_version": 2, "target_version": 3})
        check("两版组集合不同 422", r.status_code == 422
              and r.json()["detail"]["error"] == "group_set_mismatch", r.text)

        # 只读：retune 不产生新版本
        r = c.get(f"/sessions/{rid}/plans")
        check("路线查询不改动发布版",
              [p["version"] for p in r.json()] == [1, 2, 3], r.text)

        # 冻结场景：新场次，三组两两间距 3、频点 1..7，紧排 (1,4,7)<->(7,4,1)
        r = c.post("/sessions", json={"name": "冻结演示",
                                      "frequencies": [1, 2, 3, 4, 5, 6, 7]})
        fid = r.json()["id"]
        fg = []
        for s, e in [(0, 240), (0, 240), (0, 240)]:
            r = c.post(f"/sessions/{fid}/groups",
                       json={"start": s, "end": e})
            fg.append(r.json()["id"])
        fa, fb, fc = fg
        for a, b in [(fa, fb), (fa, fc), (fb, fc)]:
            c.post(f"/sessions/{fid}/interferences",
                   json={"group_a": a, "group_b": b, "min_separation": 3})
        c.post(f"/sessions/{fid}/plans",
               json={"base_version": 0,
                     "locks": [{"group_id": fa, "frequency": 1},
                               {"group_id": fb, "frequency": 4},
                               {"group_id": fc, "frequency": 7}]})
        c.post(f"/sessions/{fid}/plans",
               json={"base_version": 1,
                     "locks": [{"group_id": fa, "frequency": 7},
                               {"group_id": fb, "frequency": 4},
                               {"group_id": fc, "frequency": 1}]})
        r = c.post(f"/sessions/{fid}/retune",
                   json={"start_version": 1, "target_version": 2})
        check("端点可行却无安全路线 422", r.status_code == 422
              and r.json()["detail"]["error"] == "no_safe_route", r.text)

        # ---------------------------------------------------------------- #
        # 频点占用：登记 / 撤销 / 查询、对发布与逐组切换的影响
        # ---------------------------------------------------------------- #
        # 占用 422 用小场次：单组 [0,60)、可用频点 {1,2}
        r = c.post("/sessions", json={"name": "占用演示",
                                      "frequencies": [1, 2]})
        oid = r.json()["id"]
        check("创建占用演示场次", r.status_code == 201, r.text)
        r = c.post(f"/sessions/{oid}/groups",
                   json={"name": "组x", "start": 0, "end": 60})
        gx = r.json()["id"]
        c.post(f"/sessions/{oid}/plans",
               json={"base_version": 0, "locks": []})

        # 登记校验：频点不属于场次 / 起点不早于终点 -> 400 整次拒绝
        r = c.post(f"/sessions/{oid}/occupations",
                   json={"frequency": 99, "start": 0, "end": 30})
        check("占用非场次频点被拒绝(400)", r.status_code == 400, r.text)
        r = c.post(f"/sessions/{oid}/occupations",
                   json={"frequency": 1, "start": 30, "end": 30})
        check("占用起点不早于终点被拒绝(400)", r.status_code == 400, r.text)
        # 场次不存在 -> 404
        r = c.post("/sessions/999999/occupations",
                   json={"frequency": 1, "start": 0, "end": 30})
        check("向不存在场次登记占用(404)", r.status_code == 404, r.text)

        # 正常登记两条占用并查询
        r = c.post(f"/sessions/{oid}/occupations",
                   json={"frequency": 1, "start": 0, "end": 60})
        check("登记占用1", r.status_code == 201, r.text)
        occ1 = r.json()["id"]
        r = c.post(f"/sessions/{oid}/occupations",
                   json={"frequency": 2, "start": 60, "end": 120})
        check("登记占用2（与组端点相接，不生效）", r.status_code == 201, r.text)
        occ2 = r.json()["id"]
        r = c.get(f"/sessions/{oid}/occupations")
        check("查询当前占用", r.status_code == 200
              and [o["id"] for o in r.json()] == [occ1, occ2], r.text)
        r = c.get(f"/sessions/{oid}")
        check("场次详情含占用列表",
              len(r.json()["occupations"]) == 2, r.text)

        # 端点相接（[60,120) 与 [0,60)）不算相交：仅频点1不可用，仍可发布
        r = c.post(f"/sessions/{oid}/plans",
                   json={"base_version": 1, "locks": []})
        check("端点相接不影响规划，发布成功", r.status_code == 201
              and next(a["frequency"] for a in r.json()["assignments"]
                       if a["group_id"] == gx) == 2, r.text)

        # 撤销未知记录 -> 400；正常撤销后 422 消失
        r = c.delete(f"/sessions/{oid}/occupations/999999")
        check("撤销未知占用记录被拒绝(400)", r.status_code == 400, r.text)
        r = c.delete(f"/sessions/{oid}/occupations/{occ2}")
        check("撤销占用2", r.status_code == 200
              and r.json()["revoked"] is True, r.text)
        r = c.get(f"/sessions/{oid}/occupations")
        check("撤销后当前占用只剩记录1",
              [o["id"] for o in r.json()] == [occ1], r.text)

        # 占用使所有频点不可用 -> 422 且指出相关占用，发布版不变
        r = c.post(f"/sessions/{oid}/occupations",
                   json={"frequency": 2, "start": 10, "end": 50})
        check("登记占用3（频点2 覆盖组时段）", r.status_code == 201, r.text)
        occ3 = r.json()["id"]
        r = c.post(f"/sessions/{oid}/plans",
                   json={"base_version": 2, "locks": []})
        check("占用导致无解(422)", r.status_code == 422, r.text)
        detail = r.json()["detail"]
        check("422 指出相关占用",
              detail["error"] == "unsatisfiable"
              and detail["cause"] == "occupations"
              and {o["id"] for o in detail["occupations"]} == {occ1, occ3},
              str(detail))
        r = c.get(f"/sessions/{oid}/plans")
        check("占用无解后发布版不变",
              [p["version"] for p in r.json()] == [1, 2], r.text)

        # 撤销占用后同一 base_version 可立即发布（已发布版不自动重写）
        c.delete(f"/sessions/{oid}/occupations/{occ3}")
        r = c.post(f"/sessions/{oid}/plans",
                   json={"base_version": 2, "locks": []})
        check("撤销占用后发布成功", r.status_code == 201, r.text)

        # 占用不回写已发布版：v1 仍是当初的分配（只含组x=1，与现在无关的检查）
        r = c.get(f"/sessions/{oid}/plans/1")
        check("已发布版保持不可变",
              next(a["frequency"] for a in r.json()["assignments"]
                   if a["group_id"] == gx) == 1, r.text)

        # 占用与发布串行：占用登记期间并发发布只会在占用提交后看到一致状态。
        # 先恢复一个全占用状态，再串行验证行为即可（锁语义由行锁保证）。
        r = c.post(f"/sessions/{oid}/occupations",
                   json={"frequency": 1, "start": 0, "end": 60})
        check("再次登记占用1", r.status_code == 201, r.text)

        # ---------------------------------------------------------------- #
        # 占用对逐组切换路线的影响（复用切换演示场次 rid：频点 1..7）
        # ---------------------------------------------------------------- #
        # 当前 rid 有 ga gb gd 三组且 v3 为最新；重新构造独立的占用切换场次，
        # 频点 1..9，三组串行 a[0,120) b[60,180) c[120,240)，边 a-b、b-c 间距3
        r = c.post("/sessions", json={"name": "占用切换演示",
                                      "frequencies": list(range(1, 10))})
        zid = r.json()["id"]
        zg = []
        for name, s, e in [("a", 0, 120), ("b", 60, 180), ("c", 120, 240)]:
            r = c.post(f"/sessions/{zid}/groups",
                       json={"name": name, "start": s, "end": e})
            zg.append(r.json()["id"])
        za, zb, zc = zg
        c.post(f"/sessions/{zid}/interferences",
               json={"group_a": za, "group_b": zb, "min_separation": 3})
        c.post(f"/sessions/{zid}/interferences",
               json={"group_a": zb, "group_b": zc, "min_separation": 3})
        c.post(f"/sessions/{zid}/plans",
               json={"base_version": 0, "locks": [
                   {"group_id": za, "frequency": 1},
                   {"group_id": zb, "frequency": 4},
                   {"group_id": zc, "frequency": 1}]})
        c.post(f"/sessions/{zid}/plans",
               json={"base_version": 1, "locks": [
                   {"group_id": za, "frequency": 4},
                   {"group_id": zb, "frequency": 1},
                   {"group_id": zc, "frequency": 4}]})

        # 占用频点 7 的 [60,180)：与 a、b 相交（c 仅端点相接）。
        # 原字典序最小路线首步 (b,7) 借用临时频点，现须改为 (b,8)。
        r = c.post(f"/sessions/{zid}/occupations",
                   json={"frequency": 7, "start": 60, "end": 180})
        check("登记切换占用(频点7)", r.status_code == 201, r.text)
        zocc = r.json()["id"]
        r = c.post(f"/sessions/{zid}/retune",
                   json={"start_version": 1, "target_version": 2})
        check("占用下切换路线 200", r.status_code == 200, r.text)
        steps = [(s["group_id"], s["frequency"])
                 for s in r.json()["steps"]]
        check("占用改变字典序路线：首步改用频点8",
              steps == [(zb, 8), (za, 4), (zc, 4), (zb, 1)], str(steps))

        # 撤销占用后恢复原路线（b 借用 7）
        c.delete(f"/sessions/{zid}/occupations/{zocc}")
        r = c.post(f"/sessions/{zid}/retune",
                   json={"start_version": 1, "target_version": 2})
        steps = [(s["group_id"], s["frequency"])
                 for s in r.json()["steps"]]
        check("撤销占用后恢复原路线",
              steps == [(zb, 7), (za, 4), (zc, 4), (zb, 1)], str(steps))

        # 占用使旧版端点不可行：占用频点 1 的 [60,180) 与 a、b 相交，
        # c 仅端点相接。以 v2 为起点（a=4,b=1,c=4，b 撞占用）、v1 为终点
        # （a=1,b=4,c=1，a 撞占用）-> 两端均不可行，422 endpoint_infeasible
        # 并指出相关占用。
        r = c.post(f"/sessions/{zid}/occupations",
                   json={"frequency": 1, "start": 60, "end": 180})
        check("登记占用频点1", r.status_code == 201, r.text)
        zocc2 = r.json()["id"]
        r = c.post(f"/sessions/{zid}/retune",
                   json={"start_version": 2, "target_version": 1})
        check("旧版因新占用端点不可行(422)", r.status_code == 422
              and r.json()["detail"]["error"] == "endpoint_infeasible", r.text)
        detail = r.json()["detail"]
        rel_ids = {o["id"] for o in detail["start"]["related_occupations"]} \
            | {o["id"] for o in detail["target"]["related_occupations"]}
        check("endpoint_infeasible 指出相关占用", rel_ids == {zocc2},
              str(detail))
        check("占用冲突定位到具体组与频点",
              detail["start"]["occupations"] == [
                  {"group_id": zb, "frequency": 1}]
              and detail["target"]["occupations"] == [
                  {"group_id": za, "frequency": 1},
                  {"group_id": zc, "frequency": 1}], str(detail))
        # retune 只读：占用与发布版均无新增变化
        r = c.get(f"/sessions/{zid}/plans")
        check("占用下路线查询不改动发布版",
              [p["version"] for p in r.json()] == [1, 2], r.text)

        # ---------------------------------------------------------------- #
        # 现场实测频点批次：留存、核对、幂等、冲突与历史固化
        # ---------------------------------------------------------------- #
        r = c.post("/sessions", json={"name": "实测批次演示",
                                      "frequencies": [1, 2, 3, 4, 5, 6]})
        mid = r.json()["id"]
        check("创建实测批次场次", r.status_code == 201, r.text)
        mg = []
        for name, s, e in [("音响", 0, 120), ("灯光", 60, 180),
                           ("返送", 120, 240)]:
            r = c.post(f"/sessions/{mid}/groups",
                       json={"name": name, "start": s, "end": e})
            mg.append(r.json()["id"])
        m1, m2, m3 = mg
        c.post(f"/sessions/{mid}/interferences",
               json={"group_a": m1, "group_b": m2, "min_separation": 3})
        r = c.post(f"/sessions/{mid}/plans",
                   json={"base_version": 0, "locks": []})
        check("实测场次发布 v1", r.status_code == 201, r.text)
        v1 = {x["group_id"]: x["frequency"]
              for x in r.json()["assignments"]}
        check("v1 分配为 (1,4,1)", v1 == {m1: 1, m2: 4, m3: 1}, str(v1))

        def readings(a, b, cc):
            return [{"group_id": m1, "frequency": a},
                    {"group_id": m2, "frequency": b},
                    {"group_id": m3, "frequency": cc}]

        # 批次1：实测与发布版完全一致 -> 201，无任何异常
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 1,
                         "readings": readings(1, 4, 1)})
        check("提交实测批次1", r.status_code == 201, r.text)
        b1 = r.json()
        check("批次1无偏离/无异常",
              b1["deviations"] == [] and b1["anomalies"] == []
              and b1["unavailable_frequencies"] == []
              and b1["interference_violations"] == []
              and b1["occupation_conflicts"] == [], str(b1))
        check("批次1固化空占用快照", b1["occupation_snapshot"] == [])
        check("批次1留存可用频点", b1["available_frequencies"] == [1, 2, 3, 4, 5, 6])

        # 读取：最新 / 指定批次 / 列表
        r = c.get(f"/sessions/{mid}/measurements/latest")
        check("读取最新批次", r.status_code == 200
              and r.json()["batch_no"] == 1, r.text)
        r = c.get(f"/sessions/{mid}/measurements/1")
        check("读取指定批次1", r.status_code == 200
              and r.json()["id"] == b1["id"], r.text)
        r = c.get(f"/sessions/{mid}/measurements")
        check("批次列表", r.status_code == 200
              and [x["batch_no"] for x in r.json()] == [1], r.text)
        r = c.get(f"/sessions/{mid}/measurements/99")
        check("读取不存在批次(404)", r.status_code == 404)
        r = c.post("/sessions/999999/measurements",
                   json={"plan_version": 1, "batch_no": 1,
                         "readings": readings(1, 4, 1)})
        check("向不存在场次提交实测(404)", r.status_code == 404, r.text)

        # 相同批次+相同内容重试 -> 返回原结果（200，幂等，同一记录）
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 1,
                         "readings": readings(1, 4, 1)})
        check("同批次同内容重试 200 幂等",
              r.status_code == 200 and r.json()["id"] == b1["id"], r.text)
        # 相同批次+不同内容 -> 409 冲突
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 1,
                         "readings": readings(2, 4, 1)})
        check("同批次不同内容冲突(409)", r.status_code == 409
              and r.json()["detail"]["error"] == "batch_conflict", r.text)

        # 未知发布版 -> 400 invalid_reference
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 99, "batch_no": 2,
                         "readings": readings(1, 4, 1)})
        check("未知发布版被拒绝(400)", r.status_code == 400
              and r.json()["detail"]["error"] == "invalid_reference", r.text)
        # 漏组 -> 400 group_set_mismatch
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 2,
                         "readings": [{"group_id": m1, "frequency": 1},
                                      {"group_id": m2, "frequency": 4}]})
        check("漏组被拒绝(400)", r.status_code == 400
              and r.json()["detail"]["error"] == "group_set_mismatch"
              and r.json()["detail"]["missing_from_readings"] == [m3], r.text)
        # 未知组 -> 400 group_set_mismatch
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 2,
                         "readings": readings(1, 4, 1)
                         + [{"group_id": 999999, "frequency": 1}]})
        check("未知组被拒绝(400)", r.status_code == 400
              and r.json()["detail"]["unknown_groups"] == [999999], r.text)
        # 重复组 -> 400 duplicate_group
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 2,
                         "readings": [{"group_id": m1, "frequency": 1},
                                      {"group_id": m1, "frequency": 2},
                                      {"group_id": m2, "frequency": 4},
                                      {"group_id": m3, "frequency": 1}]})
        check("重复组被拒绝(400)", r.status_code == 400
              and r.json()["detail"]["error"] == "duplicate_group", r.text)

        # 批次2：m1 实测 99（不在可用集合）-> 记录并标明不可用 + 偏离
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 2,
                         "readings": readings(99, 4, 1)})
        check("提交批次2(含不可用频点)", r.status_code == 201, r.text)
        b2 = r.json()
        check("不可用频点仍留存并标记",
              b2["unavailable_frequencies"] == [
                  {"group_id": m1, "frequency": 99}]
              and any(x["group_id"] == m1 and x["frequency"] == 99
                      for x in b2["readings"]), str(b2))
        check("批次2偏离发布版",
              b2["deviations"] == [{
                  "group_id": m1, "plan_frequency": 1,
                  "measured_frequency": 99}], str(b2))
        check("批次2异常标记", set(b2["anomalies"]) ==
              {"deviation", "unavailable_frequency"}, str(b2))

        # 登记占用：频点 4 的 [30,150) 与 m1[0,120)、m2[60,180) 都相交
        r = c.post(f"/sessions/{mid}/occupations",
                   json={"frequency": 4, "start": 30, "end": 150})
        check("登记实测占用(频点4)", r.status_code == 201, r.text)
        mocc = r.json()["id"]

        # 批次5（允许跳号）：m1=m2=4 -> 偏离/间距违规/占用冲突齐全
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 5,
                         "readings": readings(4, 4, 1)})
        check("提交批次5(多类异常)", r.status_code == 201, r.text)
        b5 = r.json()
        check("批次5偏离组",
              {d["group_id"] for d in b5["deviations"]} == {m1}, str(b5))
        check("批次5间距违规",
              b5["interference_violations"] == [{
                  "group_a": m1, "group_b": m2, "min_separation": 3,
                  "frequency_a": 4, "frequency_b": 4,
                  "actual_separation": 0}], str(b5))
        check("批次5占用冲突组与占用编号",
              b5["occupation_conflicts"] == [
                  {"group_id": m1, "frequency": 4,
                   "occupation_ids": [mocc]},
                  {"group_id": m2, "frequency": 4,
                   "occupation_ids": [mocc]}], str(b5))
        check("批次5异常标记完整", set(b5["anomalies"]) ==
              {"deviation", "interference_violation", "occupation_conflict"},
              str(b5))
        check("批次5固化占用快照",
              [o["id"] for o in b5["occupation_snapshot"]] == [mocc], str(b5))

        # 历史固化：撤销占用后，批次5 的冲突结论与快照不变
        c.delete(f"/sessions/{mid}/occupations/{mocc}")
        r = c.get(f"/sessions/{mid}/measurements/5")
        b5b = r.json()
        check("撤销占用不改变历史批次",
              b5b["occupation_conflicts"] == b5["occupation_conflicts"]
              and [o["id"] for o in b5b["occupation_snapshot"]] == [mocc],
              r.text)

        # 发布新版本 v2（锁 m1=2 -> m1=2,m2=5,m3=1）后，旧批次仍以 v1 为依据
        r = c.post(f"/sessions/{mid}/plans",
                   json={"base_version": 1,
                         "locks": [{"group_id": m1, "frequency": 2}]})
        check("实测场次发布 v2", r.status_code == 201, r.text)
        r = c.get(f"/sessions/{mid}/measurements/1")
        check("新版本发布不改变历史批次依据",
              r.json()["plan_version"] == 1
              and r.json()["deviations"] == [], r.text)

        # 新增组 m4 并发布 v3：以旧版 v1 为依据提交（缺 m4）-> 组集合不一致
        r = c.post(f"/sessions/{mid}/groups",
                   json={"name": "新组", "start": 0, "end": 300})
        m4 = r.json()["id"]
        r = c.post(f"/sessions/{mid}/plans",
                   json={"base_version": 2, "locks": [
                       {"group_id": m1, "frequency": 2},
                       {"group_id": m4, "frequency": 1}]})
        check("含新组 v3 发布", r.status_code == 201, r.text)
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 1, "batch_no": 6,
                         "readings": readings(1, 4, 1)
                         + [{"group_id": m4, "frequency": 1}]})
        check("组集合与发布版/当前不一致被拒绝(400)",
              r.status_code == 400
              and r.json()["detail"]["error"] == "group_set_mismatch"
              and m4 in r.json()["detail"]["only_in_current"], r.text)
        # 以 v3 为依据提交完整四组 -> 成功
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 3, "batch_no": 6,
                         "readings": readings(2, 5, 1)
                         + [{"group_id": m4, "frequency": 1}]})
        check("按 v3 当前组集合提交批次6", r.status_code == 201, r.text)
        check("批次6无异常", r.json()["anomalies"] == [], r.text)

        # 批次号低于最新（6）-> 409 stale_batch（即使 4 从未记录）
        r = c.post(f"/sessions/{mid}/measurements",
                   json={"plan_version": 3, "batch_no": 4,
                         "readings": readings(2, 5, 1)
                         + [{"group_id": m4, "frequency": 1}]})
        check("批次号低于最新被拒绝(409)", r.status_code == 409
              and r.json()["detail"]["error"] == "stale_batch", r.text)

        # 并发：相同批次+相同内容 -> 只有一条记录，其余幂等返回
        codes, ids = [], []

        def submit_same():
            rr = c.post(f"/sessions/{mid}/measurements",
                        json={"plan_version": 3, "batch_no": 7,
                              "readings": readings(2, 5, 1)
                              + [{"group_id": m4, "frequency": 1}]})
            codes.append(("same", rr.status_code))
            if rr.status_code in (200, 201):
                ids.append(rr.json()["id"])

        threads = [threading.Thread(target=submit_same) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("并发同内容：一次创建、其余幂等",
              sorted(code for _, code in codes) == [200] * 5 + [201]
              and len(set(ids)) == 1, str(codes))

        # 并发：相同批次+互不相同内容 -> 只有一个成功，其余 409，绝不矛盾
        codes2 = []

        def submit_diff(i):
            # 每个线程使用不同频点，保证两两内容不同；频点 7 越界也可受理
            rr = c.post(f"/sessions/{mid}/measurements",
                        json={"plan_version": 3, "batch_no": 8,
                              "readings": readings(2 + i, 5, 1)
                              + [{"group_id": m4, "frequency": 1}]})
            codes2.append(rr.status_code)

        threads = [threading.Thread(target=submit_diff, args=(i,))
                   for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("并发异内容：仅一条成功其余冲突",
              codes2.count(201) == 1 and codes2.count(409) == 5, str(codes2))
        r = c.get(f"/sessions/{mid}/measurements")
        check("并发后批次无矛盾记录",
              [x["batch_no"] for x in r.json()] == [1, 2, 5, 6, 7, 8], r.text)

        print()
        if _failures:
            print(f"{len(_failures)} 项失败: {_failures}")
            return 1
        print("全部集成检查通过")
        return 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
