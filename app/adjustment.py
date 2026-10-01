"""现场外部频点占用调整的预演计算（纯函数，不访问数据库）。

现场调整外部频点占用分两步：先**预演**（给定待撤销占用编号与待登记占用的
频点/左闭右开时段，观察当前发布版在变更后的违规组与替代分配），再凭预演返回
的快照标识**确认**同一变更。本模块只实现与存储无关的部分：

- :func:`validate_change`：整次校验待撤销编号（未知/重复）与待登记项
  （不可用频点、无效时段），任一不通过都由调用方整次拒绝；
- :func:`apply_change`：在当前占用列表上派生变更后的占用（待登记项用
  负数临时编号标识，对外输出时再翻译为 ``{id: null, index: i}``）；
- :func:`violation_detail`：当前发布版分配在变更后违反的约束（不可用频点、
  干扰间距、占用冲突）及全部相关占用（含待登记项）；
- :func:`solve_replacement`：保留当前版锁定，求相对当前版改频组数最少、
  并列时按组号升序频点序列字典序最小的替代分配。
"""

from __future__ import annotations

from .retune import violations
from .solver import (FrequencySolver, analyze_unsat, intervals_overlap)


class AdjustmentValidationError(Exception):
    """变更请求校验失败（未知/重复撤销编号、不可用频点、无效时段）。"""

    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("message", "invalid adjustment"))


def pending_id(index: int) -> int:
    """待登记项在预演内部使用的临时占用编号（负数，避免与真实编号混淆）。"""
    return -(index + 1)


def occ_ref(occ: dict) -> dict:
    """占用的对外引用：既有记录给出编号；待登记项给出在 register 中的序号。"""
    if isinstance(occ["id"], int) and occ["id"] < 0:
        return {"id": None, "index": -occ["id"] - 1,
                "frequency": occ["frequency"],
                "start": occ["start"], "end": occ["end"]}
    return {"id": occ["id"], "frequency": occ["frequency"],
            "start": occ["start"], "end": occ["end"]}


def assignment_list(assignment: dict[int, int]):
    return [{"group_id": gid, "frequency": assignment[gid]}
            for gid in sorted(assignment)]


def validate_change(available, occupations, revoke_ids, additions):
    """整次校验一次占用变更；全部通过才允许进入预演/确认。

    :param available:   场次可用整数频点集合。
    :param occupations: 当前占用记录字典列表（含 id/frequency/start/end）。
    :param revoke_ids:  待撤销的占用编号列表。
    :param additions:   待登记项列表，每项含 frequency/start/end（可含
                        ``index`` 键，缺省时按列表顺序从 0 编号）。
    :raises AdjustmentValidationError: 含全部问题的结构化说明。
    """
    available = set(available)
    current_ids = {o["id"] for o in occupations}

    seen: set[int] = set()
    duplicated, unknown = [], []
    for rid in revoke_ids:
        if rid in seen and rid not in duplicated:
            duplicated.append(rid)
        seen.add(rid)
        if rid not in current_ids and rid not in unknown:
            unknown.append(rid)

    bad_intervals, bad_freqs = [], []
    for i, item in enumerate(additions):
        index = item.get("index", i)
        if item["start"] >= item["end"]:
            bad_intervals.append(index)
        if item["frequency"] not in available:
            bad_freqs.append({"index": index, "frequency": item["frequency"]})

    if duplicated or unknown or bad_intervals or bad_freqs:
        problems = []
        if unknown:
            problems.append("未知撤销编号")
        if duplicated:
            problems.append("重复撤销编号")
        if bad_freqs:
            problems.append("不可用频点")
        if bad_intervals:
            problems.append("无效时段")
        raise AdjustmentValidationError({
            "error": "invalid_adjustment",
            "message": "占用变更校验失败，整次请求已拒绝："
                       + "、".join(problems),
            "unknown_revoke": sorted(unknown),
            "duplicate_revoke": sorted(duplicated),
            "unavailable_frequencies": bad_freqs,
            "invalid_intervals": sorted(bad_intervals),
        })


def apply_change(occupations, revoke_ids, additions):
    """派生变更后的占用列表：删除待撤销记录，待登记项赋予临时负数编号。

    待登记项保持提交顺序，临时编号为 ``pending_id(index)``。
    """
    revoke_set = set(revoke_ids)
    kept = [dict(o) for o in occupations if o["id"] not in revoke_set]
    pending = [{"id": pending_id(item.get("index", i)),
                "frequency": item["frequency"],
                "start": item["start"], "end": item["end"]}
               for i, item in enumerate(additions)]
    return kept, pending


def build_forbidden_after(groups, occupations_after):
    """变更后占用展开为 {group_id: set(被占用频点)}（左闭右开相交判定）。"""
    forbidden: dict[int, set[int]] = {g["id"]: set() for g in groups}
    for occ in occupations_after:
        for g in groups:
            if intervals_overlap(g["start"], g["end"],
                                 occ["start"], occ["end"]):
                forbidden[g["id"]].add(occ["frequency"])
    return forbidden


def violation_detail(groups, assignment, edges, frequencies, occupations_after):
    """当前版分配在变更后占用下的违规情况，并补全全部相关占用引用。

    :returns: ``{unavailable, interferences, occupations,
    related_occupations, violating_groups}``；``occupations`` 项给出冲突的
    组与频点，``related_occupations`` 给出命中的既有/待登记占用。
    """
    group_ids = [g["id"] for g in groups]
    forbidden = build_forbidden_after(groups, occupations_after)
    bad = violations(group_ids, assignment, edges, frequencies, forbidden)

    group_by_id = {g["id"]: g for g in groups}
    related, seen_keys = [], set()
    for item in bad["occupations"]:
        g = group_by_id[item["group_id"]]
        for occ in occupations_after:
            key = (occ["id"], occ["frequency"], occ["start"], occ["end"])
            if key in seen_keys or occ["frequency"] != item["frequency"]:
                continue
            if intervals_overlap(g["start"], g["end"],
                                 occ["start"], occ["end"]):
                seen_keys.add(key)
                related.append(occ_ref(occ))

    violating = {x["group_id"] for x in bad["unavailable"]}
    violating.update(x["group_id"] for x in bad["occupations"])
    for edge in bad["interferences"]:
        violating.add(edge["group_a"])
        violating.add(edge["group_b"])

    return {**bad, "related_occupations": related,
            "violating_groups": sorted(violating)}


def solve_replacement(groups, frequencies, edges, current_assignment,
                      locks, occupations_after):
    """求变更后的替代分配 ``(assignment, changes)``；无解返回 ``None``。

    保留当前版锁定（锁定组只能使用当前版锁定频点）；相对当前版分配最小化
    改频组数，并列时取按组号升序频点序列字典序最小的方案。
    """
    forbidden = build_forbidden_after(groups, occupations_after)
    return FrequencySolver(
        [g["id"] for g in groups], frequencies, edges,
        current_assignment, forbidden=forbidden).solve(locks)


def unsat_explanation(groups, frequencies, edges, current_assignment,
                      locks, occupations_after):
    """替代分配无解时的结构化说明（adjustment 专用）。

    与发布接口的 :func:`analyze_unsat` 语义一致，但当前版锁定是**必须保留**
    的（不是本次新提交的试探锁定），因此即使不可约核心完全由占用/干扰构成，
    也补充列出与该核心相关的当前版锁定；待登记占用的临时负数编号在此统一
    翻译为对外引用 ``{id: null, index}``。
    """
    analysis = analyze_unsat(
        [g["id"] for g in groups], frequencies, edges, locks,
        current_assignment, occupations=occupations_after,
        group_intervals={g["id"]: (g["start"], g["end"]) for g in groups})

    # 不可约核心涉及的组：干扰端点 + 与核心占用时段相交的组
    core_groups = set()
    for edge in analysis["interferences"]:
        core_groups.add(edge["group_a"])
        core_groups.add(edge["group_b"])
    core_occ_keys = {(o["id"], o["frequency"], o["start"], o["end"])
                     for o in analysis["occupations"]}
    for occ in occupations_after:
        key = (occ["id"], occ["frequency"], occ["start"], occ["end"])
        if key not in core_occ_keys:
            continue
        for g in groups:
            if intervals_overlap(g["start"], g["end"],
                                 occ["start"], occ["end"]):
                core_groups.add(g["id"])

    listed = {(l["group_id"], l["frequency"]) for l in analysis["locks"]}
    for gid, freq in sorted(locks.items()):
        if gid in core_groups and (gid, freq) not in listed:
            analysis["locks"].append({"group_id": gid, "frequency": freq})
    analysis["locks"].sort(key=lambda l: l["group_id"])

    # 临时负数编号翻译为 {id: null, index}；既有编号按编号升序在前，
    # 待登记项按提交序号升序在后
    def _order_key(key):
        rid = key[0]
        if isinstance(rid, int) and rid < 0:
            return (1, -rid - 1, 0, 0, 0)
        return (0, rid, key[1], key[2], key[3])

    analysis["occupations"] = [
        occ_ref(next(o for o in occupations_after
                     if (o["id"], o["frequency"], o["start"], o["end"])
                     == key))
        for key in sorted(core_occ_keys, key=_order_key)]
    return analysis

