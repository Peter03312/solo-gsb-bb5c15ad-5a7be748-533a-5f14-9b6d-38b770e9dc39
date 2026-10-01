"""实测频点批次核对（纯计算，不依赖数据库）。

现场人员对某场次完成一次完整实测后，提交每个设备组的实测整数频点，并与
指定发布版及“当前约束”核对。本模块实现其中的判定逻辑：

- ``deviations``：实测频点偏离指定发布版的组；
- ``unavailable``：实测频点不在场次可用集合内的组（仍然如实记录）；
- ``interference_violations``：时段相交（左闭右开）的干扰组，实测频点间距
  小于最小间距的违规关系；
- ``occupation_conflicts``：实测频点与外部设备占用冲突的组，并给出全部冲突
  的占用编号（组时段与占用时段相交且频点相同）。

判定依据（发布版、当前组时段、生效干扰关系、当前占用快照）由 API 层在同一
事务内一次性传入；结果固化后不再受后续占用撤销或新版本发布影响。
"""

from __future__ import annotations

from .solver import intervals_overlap


def analyze_measurement(groups, readings, available, plan_assignment,
                        interferences, occupations):
    """核对一次实测批次。

    :param groups:          当前组信息列表（字典含 id/start/end）。
    :param readings:        实测频点 {group_id: frequency}，调用方需已保证
                            组集合与发布版及当前组集合一致、无重复组。
    :param available:       场次可用整数频点集合。
    :param plan_assignment: 指定发布版的分配 {group_id: frequency}。
    :param interferences:   干扰关系字典列表（group_a/group_b/
                            min_separation）；仅时段相交时生效。
    :param occupations:     当前占用快照字典列表（id/frequency/start/end）。
    :returns: 结构化核对结果（键序固定，便于直接固化与对外返回）。
    """
    group_by_id = {g["id"]: g for g in groups}
    available = set(available)

    readings_list = [{"group_id": gid, "frequency": readings[gid]}
                     for gid in sorted(readings)]

    # 1. 偏离发布版的组（实测频点与发布版不同）
    deviations = [
        {"group_id": gid,
         "plan_frequency": plan_assignment[gid],
         "measured_frequency": readings[gid]}
        for gid in sorted(readings)
        if readings[gid] != plan_assignment[gid]]

    # 2. 实测频点不在可用集合内的组（不拒绝，记录并标明异常）
    unavailable = [
        {"group_id": gid, "frequency": readings[gid]}
        for gid in sorted(readings) if readings[gid] not in available]

    # 3. 时段相交时违反最小间距的干扰关系（按组编号对归一，便于稳定输出）
    violations = []
    seen_pairs: set[tuple] = set()
    for itf in interferences:
        a, b = itf["group_a"], itf["group_b"]
        if a not in readings or b not in readings:
            continue
        ga, gb = group_by_id[a], group_by_id[b]
        if not intervals_overlap(ga["start"], ga["end"],
                                 gb["start"], gb["end"]):
            continue  # 端点相接不算相交，关系不生效
        pair = (a, b) if a < b else (b, a)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        sep = itf["min_separation"]
        fa, fb = readings[a], readings[b]
        actual = abs(fa - fb)
        if actual < sep:
            violations.append({
                "group_a": pair[0], "group_b": pair[1],
                "min_separation": sep,
                "frequency_a": readings[pair[0]],
                "frequency_b": readings[pair[1]],
                "actual_separation": actual})
    violations.sort(key=lambda v: (v["group_a"], v["group_b"]))

    # 4. 实测频点与外部占用冲突的组及占用编号
    #    组时段与占用时段相交（左闭右开）且频点相同即冲突；全部命中的占用都列出
    occupation_conflicts = []
    for gid in sorted(readings):
        freq = readings[gid]
        g = group_by_id[gid]
        hit_occ = []
        for occ in occupations:
            if occ["frequency"] != freq:
                continue
            if intervals_overlap(g["start"], g["end"],
                                 occ["start"], occ["end"]):
                hit_occ.append(occ["id"])
        if hit_occ:
            occupation_conflicts.append({
                "group_id": gid, "frequency": freq,
                "occupation_ids": sorted(hit_occ)})

    anomalies = []
    if deviations:
        anomalies.append("deviation")
    if unavailable:
        anomalies.append("unavailable_frequency")
    if violations:
        anomalies.append("interference_violation")
    if occupation_conflicts:
        anomalies.append("occupation_conflict")

    return {
        "readings": readings_list,
        "deviations": deviations,
        "unavailable_frequencies": unavailable,
        "interference_violations": violations,
        "occupation_conflicts": occupation_conflicts,
        "anomalies": anomalies,
    }
