"""临时演出无线电设备组频点协调服务（HTTP API）。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
from psycopg2.extras import Json

from . import db
from .measurement import analyze_measurement
from .schemas import (GroupCreate, InterferenceCreate, MeasurementCreate,
                      OccupationCreate, PlanCreate, RetuneRequest,
                      SessionCreate)
from .solver import (FrequencySolver, SolverLimitExceeded, analyze_unsat,
                     build_forbidden, intervals_overlap)
from .retune import RetunePlanner, RetuneLimitExceeded


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_pool()
    db.init_schema()
    yield
    db.close_pool()


app = FastAPI(
    title="频点协调服务",
    version="1.2.0",
    description="为临时演出的无线电设备组协调频点：录入场次、设备组时段与"
                "干扰关系，登记/撤销外部设备频点占用，按约束规划并发布不可"
                "变的频点分配版本，并可留存现场实测频点批次、固化核对差异。",
    lifespan=lifespan,
)


def bad_request(message: str):
    return HTTPException(status_code=400, detail=message)


# --------------------------------------------------------------------- #
# 场次
# --------------------------------------------------------------------- #
@app.post("/sessions", status_code=201)
def create_session(payload: SessionCreate, conn=Depends(db.get_conn)):
    """创建场次，登记可用的整数频点集合。"""
    frequencies = sorted(set(payload.frequencies))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (name, frequencies) VALUES (%s, %s) "
            "RETURNING id, created_at",
            (payload.name, frequencies))
        row = cur.fetchone()
    conn.commit()
    return {"id": row[0], "name": payload.name,
            "frequencies": frequencies, "created_at": row[1]}


def _load_session(conn, session_id: int, for_update: bool = False):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, frequencies, created_at FROM sessions "
            f"WHERE id = %s {'FOR UPDATE' if for_update else ''}",
            (session_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="场次不存在")
    return {"id": row[0], "name": row[1], "frequencies": list(row[2]),
            "created_at": row[3]}


def _load_groups(conn, session_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, start_min, end_min FROM groups "
            "WHERE session_id = %s ORDER BY id", (session_id,))
        return [{"id": r[0], "name": r[1], "start": r[2], "end": r[3]}
                for r in cur.fetchall()]


def _load_interferences(conn, session_id: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, group_a, group_b, min_separation FROM interferences "
            "WHERE session_id = %s ORDER BY id", (session_id,))
        return [{"id": r[0], "group_a": r[1], "group_b": r[2],
                 "min_separation": r[3]} for r in cur.fetchall()]


def _load_occupations(conn, session_id: int):
    """读取场次当前全部占用记录（撤销即删除，故此处即“当前占用”）。

    调用方在需要串行化时应先持有场次行锁（见发布与占用登记/撤销）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, frequency, start_min, end_min, created_at "
            "FROM occupations WHERE session_id = %s ORDER BY id",
            (session_id,))
        return [{"id": r[0], "frequency": r[1], "start": r[2], "end": r[3],
                 "created_at": r[4]} for r in cur.fetchall()]


def _occ_public(occ: dict) -> dict:
    """占用记录的对外结构（不含 created_at 等内部字段时同样适用）。"""
    return {"id": occ["id"], "frequency": occ["frequency"],
            "start": occ["start"], "end": occ["end"]}


@app.get("/sessions/{session_id}")
def get_session(session_id: int, conn=Depends(db.get_conn)):
    """读取场次详情（含设备组、干扰关系与当前占用记录）。"""
    session = _load_session(conn, session_id)
    session["groups"] = _load_groups(conn, session_id)
    session["interferences"] = _load_interferences(conn, session_id)
    session["occupations"] = [
        {k: occ[k] for k in ("id", "frequency", "start", "end")}
        for occ in _load_occupations(conn, session_id)]
    return session


# --------------------------------------------------------------------- #
# 设备组与干扰关系
# --------------------------------------------------------------------- #
@app.post("/sessions/{session_id}/groups", status_code=201)
def create_group(session_id: int, payload: GroupCreate,
                 conn=Depends(db.get_conn)):
    """录入设备组及其使用时段（左闭右开）。"""
    _load_session(conn, session_id)
    if payload.start >= payload.end:
        raise bad_request("时段起点必须早于终点（左闭右开）")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO groups (session_id, name, start_min, end_min) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (session_id, payload.name, payload.start, payload.end))
        group_id = cur.fetchone()[0]
    conn.commit()
    return {"id": group_id, "session_id": session_id, "name": payload.name,
            "start": payload.start, "end": payload.end}


@app.post("/sessions/{session_id}/interferences", status_code=201)
def create_interference(session_id: int, payload: InterferenceCreate,
                        conn=Depends(db.get_conn)):
    """录入两个设备组之间的干扰关系及最小频点间距。"""
    _load_session(conn, session_id)
    if payload.min_separation <= 0:
        raise bad_request("最小频点间距必须为正整数")
    if payload.group_a == payload.group_b:
        raise bad_request("干扰关系的两个组不能相同")
    groups = {g["id"] for g in _load_groups(conn, session_id)}
    unknown = [g for g in (payload.group_a, payload.group_b)
               if g not in groups]
    if unknown:
        raise bad_request(f"引用未知组: {unknown}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO interferences (session_id, group_a, group_b, "
            "min_separation) VALUES (%s, %s, %s, %s) RETURNING id",
            (session_id, payload.group_a, payload.group_b,
             payload.min_separation))
        interference_id = cur.fetchone()[0]
    conn.commit()
    return {"id": interference_id, "session_id": session_id,
            "group_a": payload.group_a, "group_b": payload.group_b,
            "min_separation": payload.min_separation}


# --------------------------------------------------------------------- #
# 频点占用（现场其他设备在指定时段占用场次频点）
# --------------------------------------------------------------------- #
@app.post("/sessions/{session_id}/occupations", status_code=201)
def create_occupation(session_id: int, payload: OccupationCreate,
                      conn=Depends(db.get_conn)):
    """登记一条频点占用记录，返回记录编号。

    占用频点须属于场次可用集合，时段为整数分钟左闭右开 [start, end)；
    频点不属于场次或起点不早于终点时拒绝整次操作（400）。占用登记/撤销
    与发布按场次串行（同锁场次行），已发布版保持不可变，不会因占用变化
    自动重写。
    """
    # 锁场次行：与发布及其他占用变更互斥，保证发布基于一致占用状态
    session = _load_session(conn, session_id, for_update=True)
    if payload.start >= payload.end:
        raise bad_request("占用时段起点必须早于终点（左闭右开）")
    if payload.frequency not in session["frequencies"]:
        raise bad_request(
            f"被占用频点 {payload.frequency} 不在场次可用频点集合中")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO occupations (session_id, frequency, start_min, end_min)"
            " VALUES (%s, %s, %s, %s) RETURNING id, created_at",
            (session_id, payload.frequency, payload.start, payload.end))
        occupation_id, created_at = cur.fetchone()
    conn.commit()
    return {"id": occupation_id, "session_id": session_id,
            "frequency": payload.frequency, "start": payload.start,
            "end": payload.end, "created_at": created_at}


@app.delete("/sessions/{session_id}/occupations/{occupation_id}")
def delete_occupation(session_id: int, occupation_id: int,
                      conn=Depends(db.get_conn)):
    """撤销（删除）一条占用记录；记录未知或不属于本场次时拒绝（400）。"""
    # 锁场次行：与发布及其他占用变更互斥
    _load_session(conn, session_id, for_update=True)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM occupations WHERE id = %s AND session_id = %s "
            "RETURNING frequency, start_min, end_min",
            (occupation_id, session_id))
        row = cur.fetchone()
    if row is None:
        raise bad_request(f"撤销的占用记录 {occupation_id} 不存在")
    conn.commit()
    return {"id": occupation_id, "session_id": session_id,
            "revoked": True, "frequency": row[0],
            "start": row[1], "end": row[2]}


@app.get("/sessions/{session_id}/occupations")
def list_occupations(session_id: int, conn=Depends(db.get_conn)):
    """查询场次当前占用（已撤销的记录不再出现），按记录编号升序。"""
    _load_session(conn, session_id)
    occupations = _load_occupations(conn, session_id)
    return [{"id": occ["id"], "frequency": occ["frequency"],
             "start": occ["start"], "end": occ["end"],
             "created_at": occ["created_at"]} for occ in occupations]


# --------------------------------------------------------------------- #
# 规划与发布
# --------------------------------------------------------------------- #
def _current_version(conn, session_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(version), 0) FROM plans "
                    "WHERE session_id = %s", (session_id,))
        return cur.fetchone()[0]


def _load_assignments(conn, plan_id: int) -> dict[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT group_id, frequency FROM plan_assignments "
                    "WHERE plan_id = %s ORDER BY group_id", (plan_id,))
        return {r[0]: r[1] for r in cur.fetchall()}


@app.post("/sessions/{session_id}/plans", status_code=201)
def publish_plan(session_id: int, payload: PlanCreate,
                 conn=Depends(db.get_conn)):
    """规划并发布新版本。

    须提交所见版本号 base_version：与当前最新发布版不一致时返回 409，
    并发发布只有一个可以成功。规划在同一场次行锁内读取当前占用，保证
    基于一致占用状态；占用与干扰、锁定共同导致无解时返回 422 并说明
    涉及的锁定组、干扰关系或相关占用记录，当前发布版不变。
    """
    # 锁定场次行，串行化并发发布
    session = _load_session(conn, session_id, for_update=True)
    current = _current_version(conn, session_id)
    if payload.base_version != current:
        raise HTTPException(
            status_code=409,
            detail={"error": "version_conflict",
                    "message": "所见版本已过期，请基于最新发布版重新提交",
                    "current_version": current})

    groups = _load_groups(conn, session_id)
    group_by_id = {g["id"]: g for g in groups}
    interferences = _load_interferences(conn, session_id)
    # 发布在同一场次行锁内读取当前占用，保证基于一致占用状态
    occupations = _load_occupations(conn, session_id)
    available = set(session["frequencies"])

    # 设备组时段与占用时段相交（左闭右开）时禁用该频点
    forbidden = build_forbidden(groups, occupations)
    group_intervals = {g["id"]: (g["start"], g["end"]) for g in groups}

    # 校验锁定：引用未知组 / 锁定不可用频点 / 重复锁定均拒绝整次操作
    locks: dict[int, int] = {}
    for lock in payload.locks:
        if lock.group_id not in group_by_id:
            raise bad_request(f"锁定引用了未知组: {lock.group_id}")
        if lock.frequency not in available:
            raise bad_request(
                f"组 {lock.group_id} 锁定的频点 {lock.frequency} "
                "不在场次可用频点集合中")
        if lock.group_id in locks:
            raise bad_request(f"组 {lock.group_id} 被重复锁定")
        locks[lock.group_id] = lock.frequency

    # 仅时段相交（左闭右开）的干扰关系生效
    edges = []
    for itf in interferences:
        ga, gb = group_by_id[itf["group_a"]], group_by_id[itf["group_b"]]
        if intervals_overlap(ga["start"], ga["end"],
                             gb["start"], gb["end"]):
            edges.append((itf["group_a"], itf["group_b"],
                          itf["min_separation"]))

    # 前一发布版的分配（首次规划时为空，改频数目标恒为 0）
    previous: dict[int, int] = {}
    if current > 0:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE session_id = %s "
                        "AND version = %s", (session_id, current))
            previous = _load_assignments(conn, cur.fetchone()[0])

    try:
        result = FrequencySolver(
            [g["id"] for g in groups], session["frequencies"], edges,
            previous, forbidden=forbidden).solve(locks)
    except SolverLimitExceeded:
        raise HTTPException(status_code=503,
                            detail="求解超出资源限制，请缩小规模后重试")

    if result is None:
        analysis = analyze_unsat(
            [g["id"] for g in groups], session["frequencies"], edges, locks,
            previous, occupations=[_occ_public(o) for o in occupations],
            group_intervals=group_intervals)
        raise HTTPException(
            status_code=422,
            detail={"error": "unsatisfiable",
                    "message": "约束不可满足，当前发布版不变",
                    **analysis})

    assignment, changes = result
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO plans (session_id, version, changes) "
            "VALUES (%s, %s, %s) RETURNING id, created_at",
            (session_id, current + 1, changes))
        plan_id, created_at = cur.fetchone()
        cur.executemany(
            "INSERT INTO plan_assignments (plan_id, group_id, frequency) "
            "VALUES (%s, %s, %s)",
            [(plan_id, gid, freq) for gid, freq in assignment.items()])
        if locks:
            cur.executemany(
                "INSERT INTO plan_locks (plan_id, group_id, frequency) "
                "VALUES (%s, %s, %s)",
                [(plan_id, gid, freq) for gid, freq in locks.items()])
    conn.commit()
    return {
        "session_id": session_id,
        "version": current + 1,
        "changes": changes,
        "assignments": [{"group_id": gid, "frequency": assignment[gid]}
                        for gid in sorted(assignment)],
        "locks": [{"group_id": gid, "frequency": freq}
                  for gid, freq in sorted(locks.items())],
        "created_at": created_at,
    }


# --------------------------------------------------------------------- #
# 发布版查询（发布版不可变，仅提供读取）
# --------------------------------------------------------------------- #
def _load_plan(conn, session_id: int, version: int):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, version, changes, created_at FROM plans "
            "WHERE session_id = %s AND version = %s",
            (session_id, version))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="发布版不存在")
    return {"id": row[0], "version": row[1], "changes": row[2],
            "created_at": row[3]}


@app.get("/sessions/{session_id}/plans")
def list_plans(session_id: int, conn=Depends(db.get_conn)):
    """列出场次的全部发布版。"""
    _load_session(conn, session_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version, changes, created_at FROM plans "
            "WHERE session_id = %s ORDER BY version", (session_id,))
        return [{"version": r[0], "changes": r[1], "created_at": r[2]}
                for r in cur.fetchall()]


@app.get("/sessions/{session_id}/plans/{version}")
def get_plan(session_id: int, version: int, conn=Depends(db.get_conn)):
    """读取指定发布版的频点分配结果。"""
    _load_session(conn, session_id)
    plan = _load_plan(conn, session_id, version)
    assignments = _load_assignments(conn, plan["id"])
    with conn.cursor() as cur:
        cur.execute("SELECT group_id, frequency FROM plan_locks "
                    "WHERE plan_id = %s ORDER BY group_id", (plan["id"],))
        locks = [{"group_id": r[0], "frequency": r[1]}
                 for r in cur.fetchall()]
    return {
        "session_id": session_id,
        "version": plan["version"],
        "changes": plan["changes"],
        "assignments": [{"group_id": gid, "frequency": assignments[gid]}
                        for gid in sorted(assignments)],
        "locks": locks,
        "created_at": plan["created_at"],
    }


@app.get("/sessions/{session_id}/plans/{version}/diff")
def get_plan_diff(session_id: int, version: int, conn=Depends(db.get_conn)):
    """读取指定发布版与前一版的差异（改频 / 新增 / 移除的组）。"""
    _load_session(conn, session_id)
    plan = _load_plan(conn, session_id, version)
    current = _load_assignments(conn, plan["id"])
    previous: dict[int, int] = {}
    if version > 1:
        prev_plan = _load_plan(conn, session_id, version - 1)
        previous = _load_assignments(conn, prev_plan["id"])

    changed, added, removed = [], [], []
    unchanged = 0
    for gid in sorted(current):
        if gid not in previous:
            added.append({"group_id": gid, "frequency": current[gid]})
        elif previous[gid] != current[gid]:
            changed.append({"group_id": gid,
                            "old_frequency": previous[gid],
                            "new_frequency": current[gid]})
        else:
            unchanged += 1
    for gid in sorted(previous):
        if gid not in current:
            removed.append({"group_id": gid,
                            "old_frequency": previous[gid]})
    return {
        "session_id": session_id,
        "version": version,
        "previous_version": version - 1 if version > 1 else None,
        "changed": changed,
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
    }


# --------------------------------------------------------------------- #
# 逐组切换路线（只读：基于一致快照计算，不改动任何发布版）
# --------------------------------------------------------------------- #
def _retune_error(status_code: int, code: str, message: str, **extra):
    detail = {"error": code, "message": message, **extra}
    return HTTPException(status_code=status_code, detail=detail)


def _assignments_for_version(conn, session_id: int, version: int):
    """读取指定版本的计划行与分配；计划不存在返回 None。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM plans WHERE session_id = %s AND version = %s",
            (session_id, version))
        row = cur.fetchone()
    if row is None:
        return None
    return _load_assignments(conn, row[0])


def _assignment_list(assignment: dict[int, int]):
    return [{"group_id": gid, "frequency": assignment[gid]}
            for gid in sorted(assignment)]


def _endpoint_occ_detail(bad: dict, group_by_id: dict, occupations: list[dict]):
    """在端点违规（unavailable/interferences/occupations）基础上补全相关
    占用记录：按组时段与占用时段相交且频点相同匹配。"""
    related = []
    seen = set()
    for item in bad["occupations"]:
        g = group_by_id[item["group_id"]]
        for occ in occupations:
            if occ["id"] in seen:
                continue
            if occ["frequency"] != item["frequency"]:
                continue
            if intervals_overlap(g["start"], g["end"],
                                 occ["start"], occ["end"]):
                seen.add(occ["id"])
                related.append(_occ_public(occ))
    return {**bad, "related_occupations": related}


@app.post("/sessions/{session_id}/retune")
def plan_retune(session_id: int, payload: RetuneRequest,
                conn=Depends(db.get_conn)):
    """计算从起始发布版到目标发布版的逐组调频切换路线（只读，不改发布版）。

    每步只把一个组改到本场次可用频点（可借用临时频点），每步之后的完整
    分配都须满足按时段相交生效的干扰关系及最小间距；外部设备占用的频点
    在起点、终点与每个临时步骤都不可选用；不可调频组全程保持不变。先
    最小化步骤数，再按 (组编号, 新频点) 序列取字典序最小。

    本接口为只读计算：配置、发布版与当前占用在同一个可串行化快照事务中
    读取，保证快照一致，且不会创建或修改任何发布版或占用记录。
    """
    # 显式开启只读的可重复读事务，使全部读取基于同一份配置快照
    with conn.cursor() as cur:
        cur.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    try:
        session = _load_session(conn, session_id)  # 不存在 -> 404
        groups = _load_groups(conn, session_id)
        group_ids = {g["id"] for g in groups}
        interferences = _load_interferences(conn, session_id)
        # 与配置、发布版在同一个可重复读只读事务中读取，占用快照一致
        occupations = _load_occupations(conn, session_id)

        start = _assignments_for_version(
            conn, session_id, payload.start_version)
        target = _assignments_for_version(
            conn, session_id, payload.target_version)
        if start is None:
            raise _retune_error(
                400, "invalid_reference",
                f"起始发布版 {payload.start_version} 不存在",
                field="start_version")
        if target is None:
            raise _retune_error(
                400, "invalid_reference",
                f"目标发布版 {payload.target_version} 不存在",
                field="target_version")

        # 不可调频组：重复 / 引用非法（含引用版本之外或当前不存在的组）拒绝
        fixed: list[int] = []
        seen_fixed: set[int] = set()
        for gid in payload.fixed_groups:
            if gid in seen_fixed:
                raise _retune_error(
                    400, "invalid_reference",
                    f"不可调频组 {gid} 重复指定", field="fixed_groups")
            if gid not in group_ids:
                raise _retune_error(
                    400, "invalid_reference",
                    f"不可调频组引用了未知组: {gid}", field="fixed_groups")
            seen_fixed.add(gid)
            fixed.append(gid)

        # 两版组集合必须一致，且就是本场次当前组集合
        start_set, target_set = set(start), set(target)
        if start_set != target_set or start_set != group_ids:
            only_start = sorted(start_set - target_set)
            only_target = sorted(target_set - start_set)
            missing_current = sorted(
                group_ids - (start_set & target_set))
            extra_plans = sorted((start_set | target_set) - group_ids)
            raise _retune_error(
                422, "group_set_mismatch",
                "起始版与目标版的组集合不同，或与本场次当前组集合不一致，"
                "无法逐组切换",
                only_in_start=only_start,
                only_in_target=only_target,
                missing_from_plans=missing_current,
                unknown_groups=extra_plans)

        # 不可调频组在两版频点必须相同，否则需改频
        fixed_conflicts = [
            {"group_id": gid,
             "start_frequency": start[gid],
             "target_frequency": target[gid]}
            for gid in fixed if start[gid] != target[gid]]
        if fixed_conflicts:
            raise _retune_error(
                422, "fixed_group_conflict",
                "不可调频组在起始版与目标版中的频点不同，无法全程保持不变",
                fixed_groups=fixed_conflicts)

        # 仅时段相交（左闭右开）的干扰关系在当前规则下生效
        group_by_id = {g["id"]: g for g in groups}
        edges = []
        for itf in interferences:
            ga, gb = group_by_id[itf["group_a"]], group_by_id[itf["group_b"]]
            if intervals_overlap(ga["start"], ga["end"],
                                 gb["start"], gb["end"]):
                edges.append((itf["group_a"], itf["group_b"],
                              itf["min_separation"]))

        # 当前占用快照：设备组时段与占用时段相交时禁用相应频点；
        # 起终点及每个临时步骤都按这一份快照检查
        forbidden = build_forbidden(groups, occupations)

        planner = RetunePlanner(
            sorted(group_ids), session["frequencies"], edges,
            start, target, fixed, forbidden=forbidden)
        start_bad, target_bad = planner.endpoint_violations()
        if start_bad["unavailable"] or start_bad["interferences"] \
                or start_bad["occupations"] \
                or target_bad["unavailable"] or target_bad["interferences"] \
                or target_bad["occupations"]:
            raise _retune_error(
                422, "endpoint_infeasible",
                "起始版或目标版在当前规则（含频点占用）下不是可行分配",
                start=_endpoint_occ_detail(start_bad, group_by_id,
                                           occupations),
                target=_endpoint_occ_detail(target_bad, group_by_id,
                                            occupations))

        try:
            route = planner.plan()
        except RetuneLimitExceeded:
            raise HTTPException(
                status_code=503,
                detail="切换路线搜索超出资源限制，请缩小规模后重试")

        if route == "no_safe_route":
            raise _retune_error(
                422, "no_safe_route",
                "两端点均可行，但不存在满足约束的逐组切换安全路线")
    finally:
        conn.commit()  # 提交只读事务（无任何写入），归还连接

    # 回放路线，记录每步后的完整分配；新频点偏离该组目标频点即为临时频点
    current = dict(start)
    steps = []
    for index, (gid, frequency) in enumerate(route, start=1):
        current[gid] = frequency
        steps.append({
            "step": index,
            "group_id": gid,
            "frequency": frequency,
            "temporary": frequency != target[gid],
            "assignment": _assignment_list(current),
        })

    return {
        "session_id": session_id,
        "start_version": payload.start_version,
        "target_version": payload.target_version,
        "fixed_groups": sorted(fixed),
        "steps_count": len(steps),
        "start_assignment": _assignment_list(start),
        "target_assignment": _assignment_list(target),
        "steps": steps,
    }


# --------------------------------------------------------------------- #
# 现场实测频点批次（留存一次完整实测并核对差异；提交后不可变）
# --------------------------------------------------------------------- #
def _measurement_error(status_code: int, code: str, message: str, **extra):
    detail = {"error": code, "message": message, **extra}
    return HTTPException(status_code=status_code, detail=detail)


def _measurement_public(row: dict) -> dict:
    """组装批次的对外响应：固化结果 + 批次元数据 + 占用快照。"""
    result = dict(row["result"])
    result.update({
        "id": row["id"],
        "session_id": row["session_id"],
        "batch_no": row["batch_no"],
        "plan_version": row["plan_version"],
        "created_at": row["created_at"],
        "occupation_snapshot": row["occupation_snapshot"],
    })
    return result


def _load_measurement(conn, session_id: int, batch_no: int):
    """读取已固化的批次行；不存在返回 None。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, session_id, batch_no, plan_version, result, "
            "content_fingerprint, occupation_snapshot, created_at "
            "FROM measurements WHERE session_id = %s AND batch_no = %s",
            (session_id, batch_no))
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "session_id": row[1], "batch_no": row[2],
            "plan_version": row[3], "result": row[4],
            "content_fingerprint": row[5], "occupation_snapshot": row[6],
            "created_at": row[7]}


@app.post("/sessions/{session_id}/measurements", status_code=201)
def submit_measurement(session_id: int, payload: MeasurementCreate,
                       response: Response, conn=Depends(db.get_conn)):
    """留存某场次一次完整实测频点，并核对其与指定发布版及当前约束的差异。

    - 组集合必须与指定发布版及场次当前组集合一致：未知发布版、漏组、重复组
      或引用未知组一律拒绝（400/404），不产生批次；
    - 实测频点即使不在可用集合内也如实记录，并在结果中标明异常；
    - 批次号低于已记录最新批次时拒绝（409 stale_batch）；相同批次号+相同
      内容的重试返回原结果（200，幂等），相同批次号+不同内容返回冲突（409
      batch_conflict）；
    - 核对基于同一份当前占用状态（与发布/占用变更共用场次行锁），结果与占用
      快照一并固化；后续撤销占用或发布新版本都不改变历史结果。
    """
    # 锁场次行：与发布、占用登记/撤销互斥，保证基于一致的当前占用状态
    session = _load_session(conn, session_id, for_update=True)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(batch_no), 0) FROM measurements "
            "WHERE session_id = %s", (session_id,))
        latest_batch = cur.fetchone()[0]
    if payload.batch_no < latest_batch:
        raise _measurement_error(
            409, "stale_batch",
            "批次号低于已记录的最新批次，请使用更大的批次号",
            submitted_batch=payload.batch_no,
            latest_batch=latest_batch)

    # 重复组先于其余校验：重复提交不构成合法内容（同批次重试也不例外）
    seen: set[int] = set()
    duplicated: list[int] = []
    for item in payload.readings:
        if item.group_id in seen and item.group_id not in duplicated:
            duplicated.append(item.group_id)
        seen.add(item.group_id)
    if duplicated:
        raise _measurement_error(
            400, "duplicate_group", "实测频点存在重复设备组",
            duplicated_groups=sorted(duplicated))

    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE session_id = %s AND version = %s",
                    (session_id, payload.plan_version))
        plan_row = cur.fetchone()
    if plan_row is None:
        raise _measurement_error(
            400, "invalid_reference",
            f"发布版 {payload.plan_version} 不存在",
            field="plan_version", plan_version=payload.plan_version)
    plan_id = plan_row[0]
    plan_assignment = _load_assignments(conn, plan_id)
    plan_set = set(plan_assignment)

    groups = _load_groups(conn, session_id)
    current_set = {g["id"] for g in groups}
    reading_set = {item.group_id for item in payload.readings}
    if reading_set != plan_set or reading_set != current_set:
        raise _measurement_error(
            400, "group_set_mismatch",
            "实测组集合与发布版组集合或场次当前组集合不一致：漏组、未知组或"
            "多报均拒绝",
            missing_from_readings=sorted((plan_set & current_set)
                                         - reading_set),
            unknown_groups=sorted(reading_set - current_set),
            only_in_plan=sorted(plan_set - current_set),
            only_in_current=sorted(current_set - plan_set))

    fingerprint = {
        "plan_version": payload.plan_version,
        "readings": sorted(
            ({"group_id": item.group_id, "frequency": item.frequency}
             for item in payload.readings),
            key=lambda r: r["group_id"]),
    }

    # 同批次重试：相同内容返回原结果（200），不同内容冲突（409）
    existing = _load_measurement(conn, session_id, payload.batch_no)
    if existing is not None:
        if existing["content_fingerprint"] == fingerprint:
            response.status_code = 200
            return _measurement_public(existing)
        raise _measurement_error(
            409, "batch_conflict",
            "相同批次号已提交过不同内容；相同批次+相同内容的重试才会返回原结果",
            batch_no=payload.batch_no)

    # 判定依据均在本场次行锁内读取：配置、发布版与当前占用为同一份一致快照
    interferences = _load_interferences(conn, session_id)
    occupations = _load_occupations(conn, session_id)
    readings = {item.group_id: item.frequency for item in payload.readings}

    result = analyze_measurement(
        groups, readings, session["frequencies"], plan_assignment,
        interferences, occupations)
    result["available_frequencies"] = session["frequencies"]
    occupation_snapshot = [
        {"id": occ["id"], "frequency": occ["frequency"],
         "start": occ["start"], "end": occ["end"]}
        for occ in occupations]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO measurements (session_id, batch_no, plan_version, "
            "content_fingerprint, occupation_snapshot, result) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, created_at",
            (session_id, payload.batch_no, payload.plan_version,
             Json(fingerprint), Json(occupation_snapshot), Json(result)))
        measurement_id, created_at = cur.fetchone()
        cur.executemany(
            "INSERT INTO measurement_readings (measurement_id, group_id, "
            "frequency) VALUES (%s, %s, %s)",
            [(measurement_id, gid, freq)
             for gid, freq in sorted(readings.items())])
    conn.commit()

    stored = _load_measurement(conn, session_id, payload.batch_no)
    return _measurement_public(stored)


@app.get("/sessions/{session_id}/measurements")
def list_measurements(session_id: int, conn=Depends(db.get_conn)):
    """列出场次全部实测批次（按批次号升序），含各自固化的核对结果。"""
    _load_session(conn, session_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, session_id, batch_no, plan_version, result, "
            "content_fingerprint, occupation_snapshot, created_at "
            "FROM measurements WHERE session_id = %s ORDER BY batch_no",
            (session_id,))
        rows = cur.fetchall()
    return [_measurement_public(
        {"id": r[0], "session_id": r[1], "batch_no": r[2],
         "plan_version": r[3], "result": r[4],
         "content_fingerprint": r[5], "occupation_snapshot": r[6],
         "created_at": r[7]}) for r in rows]


@app.get("/sessions/{session_id}/measurements/latest")
def get_latest_measurement(session_id: int, conn=Depends(db.get_conn)):
    """读取场次最新实测批次；尚无批次时返回 404。"""
    _load_session(conn, session_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(batch_no), 0) FROM measurements "
            "WHERE session_id = %s", (session_id,))
        latest_batch = cur.fetchone()[0]
    if latest_batch == 0:
        raise _measurement_error(404, "measurement_not_found",
                                 "该场次尚无实测批次")
    row = _load_measurement(conn, session_id, latest_batch)
    return _measurement_public(row)


@app.get("/sessions/{session_id}/measurements/{batch_no}")
def get_measurement(session_id: int, batch_no: int,
                    conn=Depends(db.get_conn)):
    """读取指定批次号的实测记录与固化核对结果。"""
    _load_session(conn, session_id)
    row = _load_measurement(conn, session_id, batch_no)
    if row is None:
        raise _measurement_error(
            404, "measurement_not_found", f"实测批次 {batch_no} 不存在",
            batch_no=batch_no)
    return _measurement_public(row)
