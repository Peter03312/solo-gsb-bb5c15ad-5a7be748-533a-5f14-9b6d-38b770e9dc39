"""现场外部频点占用调整：预演与确认的纯计算逻辑（不依赖数据库）。

现场在真正调整外部频点占用之前，先按场次给出一次完整变更——待撤销的占用
编号与待登记的频点/左闭右开整数分钟时段——进行预演：

- 预演返回一个**快照标识**，覆盖当前发布版、设备组/干扰配置与当前占用；
- 同时给出当前发布版在变更后违反约束的组及**替代分配**。替代分配必须保留
  当前版的锁定，先使相对当前版的改频组数最少，再按组号升序的频点序列取
  字典序最小（与规划求解器的优化目标一致，只是“前一版”固定为当前发布版）；
- 变更后无任何可行分配时，说明导致无解的锁定、干扰关系或占用（含本次待
  登记项），且不允许确认。

确认携带快照标识与同一变更：版本、配置或占用发生任何变化即判定冲突；否则
在同一场次行锁内原子完成占用的撤销与登记，仅当替代分配确实改变了组频点
时发布不可变新版本。

待登记占用在预演阶段尚无数据库编号，统一使用负数合成编号（``REGISTER_ID_BASE``
起，``-index-1``）参与求解与无解分析，对外再转成 ``register_index`` 引用。
"""

from __future__ import annotations

import base64
import hashlib
import json

from .retune import violations
from .solver import (FrequencySolver, SolverLimitExceeded, analyze_unsat,
                     build_forbidden)

# 待登记占用的合成编号：-(register_index + 1)，永不为正，故不会与真实编号冲突
REGISTER_ID_BASE = -1


class AdjustmentUnsat(Exception):
    """变更后约束不可满足；``analysis`` 为结构化无解说明。"""

    def __init__(self, analysis: dict):
        super().__init__("occupation adjustment unsatisfiable")
        self.analysis = analysis


# --------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------- #
def validate_adjustment(frequencies, occupations, revoke, register):
    """校验一次占用调整，返回问题列表（空列表表示整次变更合法）。

    :param frequencies: 场次可用整数频点集合。
    :param occupations: 当前占用字典列表（含 id/frequency/start/end）。
    :param revoke:     待撤销占用编号列表；未知或重复编号均记为问题。
    :param register:   待登记项字典列表（frequency/start/end）；频点不在
                       场次可用集合或时段非左闭右开（start>=end）记为问题。
    """
    available = set(frequencies)
    known = {o["id"] for o in occupations}
    issues: list[dict] = []

    seen: set[int] = set()
    for occupation_id in revoke:
        if occupation_id in seen:
            # 重复引用（无论该编号是否存在）：报一次重复
            issues.append({"kind": "duplicate_revocation",
                           "occupation_id": occupation_id})
        elif occupation_id not in known:
            # 首次出现且未知：报一次未知
            issues.append({"kind": "unknown_revocation",
                           "occupation_id": occupation_id})
        seen.add(occupation_id)

    for index, item in enumerate(register):
        if item["frequency"] not in available:
            issues.append({"kind": "unavailable_frequency",
                           "register_index": index,
                           "frequency": item["frequency"]})
        if item["start"] >= item["end"]:
            issues.append({"kind": "invalid_interval",
                           "register_index": index,
                           "start": item["start"], "end": item["end"]})

    # 撤销类问题在前（按编号；未知先于重复），登记类问题在后（按登记序号），
    # 同一登记项的频点问题先于时段问题，输出稳定
    kind_rank = {"unknown_revocation": 0, "duplicate_revocation": 1,
                 "unavailable_frequency": 0, "invalid_interval": 1}

    def key(iss):
        if "occupation_id" in iss:
            return (0, iss["occupation_id"], kind_rank[iss["kind"]])
        return (1, iss["register_index"], kind_rank[iss["kind"]])

    issues.sort(key=key)
    return issues


def register_id(index: int) -> int:
    """待登记项在预演阶段使用的合成占用编号。"""
    return REGISTER_ID_BASE - index


# --------------------------------------------------------------------- #
# 变更应用（预演）
# --------------------------------------------------------------------- #
def apply_adjustment(occupations, revoke, register):
    """在当前占用上应用变更，返回变更后的占用列表（调用方需已校验合法）。

    既有占用中编号在 revoke 内的删除（撤销），register 依次以合成编号追加；
    已存在记录按原编号升序在前，待登记项按登记序号在后。
    """
    revoke_set = set(revoke)
    resulting = [dict(o) for o in occupations if o["id"] not in revoke_set]
    for index, item in enumerate(register):
        resulting.append({
            "id": register_id(index), "register_index": index,
            "frequency": item["frequency"],
            "start": item["start"], "end": item["end"]})
    return resulting


# --------------------------------------------------------------------- #
# 快照标识（无状态令牌）与变更指纹
# --------------------------------------------------------------------- #
def _sha(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def state_fingerprint(*, current_version, frequencies, groups, interferences,
                      occupations):
    """计算覆盖发布版号、可用频点、设备组时段、干扰关系与当前占用的指纹。

    任一项变化都会改变指纹，确认时据此判定“版本、配置或占用有变化”的冲突。
    """
    return _sha({
        "current_version": current_version,
        "frequencies": sorted(frequencies),
        "groups": sorted([g["id"], g["start"], g["end"]] for g in groups),
        "interferences": sorted(
            list(sorted((i["group_a"], i["group_b"])))
            + [i["min_separation"]] for i in interferences),
        "occupations": sorted(
            [o["id"], o["frequency"], o["start"], o["end"]]
            for o in occupations),
    })


def canonical_change(revoke, register):
    """把一次变更规范化，用于“同一变更”的比较与指纹。

    撤销为编号集合（合法输入不含重复）；登记为 (频点, 起点, 终点) 多重集，
    保留重复登记但忽略提交顺序。
    """
    return {
        "revoke": sorted(set(revoke)),
        "register": sorted((item["frequency"], item["start"], item["end"])
                           for item in register),
    }


def change_fingerprint(revoke, register) -> str:
    return _sha(canonical_change(revoke, register))


def encode_snapshot_token(session_id, current_version, state_hash,
                          change_hash) -> str:
    body = {"sid": session_id, "ver": current_version,
            "state": state_hash, "change": change_hash}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_snapshot_token(token: str) -> dict:
    """解码快照标识；格式非法时抛出 ValueError。"""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        body = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("非法快照标识") from exc
    if not isinstance(body, dict) or not all(
            k in body for k in ("sid", "ver", "state", "change")):
        raise ValueError("快照标识内容不完整")
    return body


# --------------------------------------------------------------------- #
# 对外引用：真实占用给出编号，待登记项给出登记序号
# --------------------------------------------------------------------- #
def occupation_ref(occ: dict) -> dict:
    base = {"frequency": occ["frequency"],
            "start": occ["start"], "end": occ["end"]}
    if occ["id"] < 0:
        return {"register_index": occ["register_index"], **base}
    return {"id": occ["id"], **base}


def _related_occupations(item, groups_by_id, occupations):
    """找出与某组在某频点上时段相交（左闭右开）的全部占用，含待登记项。"""
    from .solver import intervals_overlap  # 局部导入避免循环开销
    group = groups_by_id[item["group_id"]]
    related, seen = [], set()
    for occ in occupations:
        if occ["frequency"] != item["frequency"]:
            continue
        if not intervals_overlap(group["start"], group["end"],
                                 occ["start"], occ["end"]):
            continue
        key = ("new", occ["register_index"]) if occ["id"] < 0 \
            else ("old", occ["id"])
        if key in seen:
            continue
        seen.add(key)
        related.append(occupation_ref(occ))
    return related


def violation_detail(bad, groups, occupations):
    """在违规明细上补全相关占用引用（真实编号或待登记序号）。"""
    groups_by_id = {g["id"]: g for g in groups}
    return {
        "unavailable": list(bad["unavailable"]),
        "interferences": list(bad["interferences"]),
        "occupations": [
            {**item,
             "related_occupations": _related_occupations(
                 item, groups_by_id, occupations)}
            for item in bad["occupations"]],
    }


def violating_group_ids(bad) -> list[int]:
    """汇总当前版在变更后违反约束的全部组编号（升序去重）。"""
    groups = {item["group_id"] for item in bad["unavailable"]}
    groups |= {item["group_id"] for item in bad["occupations"]}
    for edge in bad["interferences"]:
        groups.add(edge["group_a"])
        groups.add(edge["group_b"])
    return sorted(groups)


# --------------------------------------------------------------------- #
# 预演核心
# --------------------------------------------------------------------- #
def compute_preview(*, frequencies, groups, edges, current_assignment, locks,
                    occupations, revoke, register):
    """对一次合法变更做预演。

    :param frequencies:        场次可用整数频点。
    :param groups:             当前设备组字典列表（id/start/end）。
    :param edges:              已按时段相交过滤的生效干扰关系
                               [(group_a, group_b, min_separation)]。
    :param current_assignment: 当前发布版分配 {group_id: frequency}。
    :param locks:              当前发布版锁定 {group_id: frequency}，替代
                               分配必须原样保留。
    :param occupations:        当前占用列表。
    :param revoke/register:    已通过 :func:`validate_adjustment` 的变更。
    :returns: dict(
                resulting_occupations 变更后占用（待登记项为合成编号），
                detail                违规明细（含相关占用引用），
                violating_groups      违反约束的组编号，
                replacement           替代分配 {gid: freq} 或 None，
                changed_groups        相对当前版改频的组编号，
              )
    :raises AdjustmentUnsat: 变更后无可行分配（携带无解说明）。
    """
    group_ids = [g["id"] for g in groups]
    resulting = apply_adjustment(occupations, revoke, register)
    forbidden = build_forbidden(groups, resulting)

    bad = violations(group_ids, dict(current_assignment), edges,
                     frequencies, forbidden)
    has_violation = bool(bad["unavailable"] or bad["interferences"]
                         or bad["occupations"])

    replacement = None
    changed_groups: list[int] = []
    if has_violation:
        # “前一版”即当前发布版：求解器据此最小化改频组数并做字典序决胜；
        # locks 强制保留当前版锁定（被新占用封死时该锁定会导致无解）。
        solver = FrequencySolver(
            group_ids, frequencies, edges, previous=current_assignment,
            forbidden=forbidden)
        try:
            solved = solver.solve(dict(locks))
        except SolverLimitExceeded:
            raise
        if solved is None:
            group_intervals = {g["id"]: (g["start"], g["end"])
                               for g in groups}
            analysis = analyze_unsat(
                group_ids, frequencies, edges, dict(locks),
                previous=current_assignment, occupations=resulting,
                group_intervals=group_intervals)
            by_id = {o["id"]: o for o in resulting}
            analysis["occupations"] = [
                occupation_ref(by_id[o["id"]] if "id" in o
                               and o["id"] in by_id else o)
                for o in analysis["occupations"]]
            raise AdjustmentUnsat(analysis)
        assignment, _ = solved
        replacement = assignment
        changed_groups = sorted(
            gid for gid, freq in assignment.items()
            if current_assignment.get(gid) != freq)

    return {
        "resulting_occupations": resulting,
        "detail": violation_detail(bad, groups, resulting),
        "violating_groups": violating_group_ids(bad),
        "replacement": replacement,
        "changed_groups": changed_groups,
    }
