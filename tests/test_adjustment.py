"""现场占用调整（预演/确认纯计算逻辑）单元测试：可脱离数据库运行。"""

import pytest

from app.adjustment import (AdjustmentUnsat, apply_adjustment,
                            canonical_change, change_fingerprint,
                            compute_preview, decode_snapshot_token,
                            encode_snapshot_token, register_id,
                            state_fingerprint, validate_adjustment)
from app.solver import SolverLimitExceeded


FREQS = [1, 2, 3, 4, 5, 6]
GROUPS = [
    {"id": 1, "start": 0, "end": 120},
    {"id": 2, "start": 60, "end": 180},
    {"id": 3, "start": 120, "end": 240},
]
EDGES = [(1, 2, 3), (2, 3, 3)]  # 1-3 时段不相交（左闭右开端点相接），不生效
# 无占用时的字典序最小发布：(1=1, 2=4, 3=1)
CURRENT = {1: 1, 2: 4, 3: 1}


def occ(oid, freq, s, e):
    return {"id": oid, "frequency": freq, "start": s, "end": e}


class TestValidate:
    def test_clean_change(self):
        issues = validate_adjustment(
            FREQS, [occ(7, 4, 0, 60)], [7], [{"frequency": 1, "start": 0,
                                              "end": 10}])
        assert issues == []

    def test_unknown_and_duplicate_revocation(self):
        # [9(未知), 7(已知), 9(重复)]：未知与重复各报一次（按编号同序时未知先）
        issues = validate_adjustment(
            FREQS, [occ(7, 4, 0, 60)], [9, 7, 9], [])
        kinds = [(i["kind"], i["occupation_id"]) for i in issues]
        assert kinds == [("unknown_revocation", 9),
                         ("duplicate_revocation", 9)]
        # 未知编号重复两次：一次未知、一次重复
        issues2 = validate_adjustment(FREQS, [occ(7, 4, 0, 60)], [9, 9], [])
        assert [(i["kind"], i["occupation_id"]) for i in issues2] == [
            ("unknown_revocation", 9), ("duplicate_revocation", 9)]
        # 已知编号再次出现：第一次正常，第二次记重复
        issues3 = validate_adjustment(FREQS, [occ(7, 4, 0, 60)], [7, 7], [])
        assert [i["kind"] for i in issues3] == ["duplicate_revocation"]

    def test_unavailable_frequency_and_invalid_interval(self):
        issues = validate_adjustment(
            FREQS, [], [],
            [{"frequency": 99, "start": 0, "end": 10},
             {"frequency": 2, "start": 5, "end": 5}])
        assert {(i["kind"], i["register_index"]) for i in issues} == {
            ("unavailable_frequency", 0), ("invalid_interval", 1)}

    def test_issues_ordered_revocation_before_register(self):
        # id=2 未知；id=1 第一次未知、…… 构造含全部四类：
        # revoke=[2,1,2]：2 两次均未知；register 含非场次频点
        issues = validate_adjustment(
            FREQS, [occ(1, 1, 0, 60)], [2, 1, 2],
            [{"frequency": 99, "start": 0, "end": 10}])
        kinds = [(i["kind"], i.get("occupation_id", i.get("register_index")))
                 for i in issues]
        # 序列 [2(未知),1(已知,正常),2(重复)]；撤销类按编号升序、同编号未知
        # 先于重复，登记类排在最后
        assert kinds == [
            ("unknown_revocation", 2),
            ("duplicate_revocation", 2),
            ("unavailable_frequency", 0)]


class TestApply:
    def test_revoke_and_register(self):
        result = apply_adjustment(
            [occ(1, 1, 0, 60), occ(2, 2, 0, 60)], [1],
            [{"frequency": 3, "start": 0, "end": 60}])
        assert [o["id"] for o in result] == [2, register_id(0)]
        assert result[-1]["register_index"] == 0

    def test_register_ids_are_negative_and_unique(self):
        result = apply_adjustment([], [], [
            {"frequency": 1, "start": 0, "end": 1},
            {"frequency": 2, "start": 0, "end": 1}])
        assert [o["id"] for o in result] == [-1, -2]


class TestPreview:
    def test_no_violation_when_occ_does_not_overlap(self):
        # 组 2 [60,180)；占用频点 4 的 [0,60) 与组 2 端点相接，不构成违规
        pv = compute_preview(
            frequencies=FREQS, groups=GROUPS, edges=EDGES,
            current_assignment=CURRENT, locks={}, occupations=[],
            revoke=[], register=[{"frequency": 4, "start": 0, "end": 60}])
        assert pv["violating_groups"] == []
        assert pv["replacement"] is None
        assert pv["changed_groups"] == []

    def test_occupation_violation_yields_min_change_replacement(self):
        # 占用频点 4 的 [30,150) 与组 1、组 2 相交：组 2 当前 4 被禁用，
        # 须改频；最小改频 1 组，字典序最小 -> 组 2 改到 5（组1保持 1）。
        pv = compute_preview(
            frequencies=FREQS, groups=GROUPS, edges=EDGES,
            current_assignment=CURRENT, locks={}, occupations=[],
            revoke=[], register=[{"frequency": 4, "start": 30, "end": 150}])
        assert pv["violating_groups"] == [2]
        assert pv["replacement"] == {1: 1, 2: 5, 3: 1}
        assert pv["changed_groups"] == [2]
        bad = pv["detail"]["occupations"]
        assert bad == [{"group_id": 2, "frequency": 4,
                        "related_occupations": [
                            {"register_index": 0, "frequency": 4,
                             "start": 30, "end": 150}]}]

    def test_revoke_can_clear_existing_violation_without_changes(self):
        # 当前版 (1,4,1) 已被既有占用（频点4，与组1/2相交）打破；撤销该占用后
        # 恢复可行，当前版不再违规、无需替代分配。
        existing = occ(7, 4, 30, 150)
        pv = compute_preview(
            frequencies=FREQS, groups=GROUPS, edges=EDGES,
            current_assignment=CURRENT, locks={}, occupations=[existing],
            revoke=[7], register=[])
        assert pv["violating_groups"] == []
        assert pv["replacement"] is None
        assert all(o["id"] != 7 for o in pv["resulting_occupations"])

    def test_interference_violation_reported_with_group_pair(self):
        # 两组全程重叠、间距 3，可用频点 1..5：当前 (1,4) 可行。登记占用使组 2
        # 的 4、5 都禁用，只剩 1/2/3 与组1=1 不满足间距 3（且组1也无法旁置）
        # -> 无解，占用核心中含本次待登记项（以 register_index 引用）。
        groups = [{"id": 1, "start": 0, "end": 100},
                  {"id": 2, "start": 0, "end": 100}]
        with pytest.raises(AdjustmentUnsat) as exc:
            compute_preview(
                frequencies=[1, 2, 3, 4, 5], groups=groups, edges=[(1, 2, 3)],
                current_assignment={1: 1, 2: 4}, locks={},
                occupations=[], revoke=[],
                register=[{"frequency": 4, "start": 0, "end": 100},
                          {"frequency": 5, "start": 0, "end": 100}])
        analysis = exc.value.analysis
        assert analysis["cause"] == "occupations"
        refs = {(o["register_index"], o["frequency"])
                for o in analysis["occupations"]}
        assert refs == {(0, 4), (1, 5)}

    def test_lock_must_be_retained_and_can_cause_unsat(self):
        # 当前版锁定组 2=4。登记占用禁用组 2 的 4；若无锁定组 2 可改到 5
        # （与组1=1 间距 4），但锁定必须保留 -> 无解，cause=locks 并附相关占用。
        groups = [{"id": 1, "start": 0, "end": 100},
                  {"id": 2, "start": 0, "end": 100}]
        with pytest.raises(AdjustmentUnsat) as exc:
            compute_preview(
                frequencies=[1, 2, 3, 4, 5], groups=groups, edges=[(1, 2, 3)],
                current_assignment={1: 1, 2: 4}, locks={2: 4},
                occupations=[], revoke=[],
                register=[{"frequency": 4, "start": 0, "end": 100}])
        analysis = exc.value.analysis
        assert analysis["cause"] == "locks"
        assert analysis["locks"] == [{"group_id": 2, "frequency": 4}]
        assert analysis["occupations"] == [
            {"register_index": 0, "frequency": 4, "start": 0, "end": 100}]

    def test_lock_retained_when_solution_exists(self):
        # 当前版锁组1=1；占用 4 迫使组2离开4 -> 组2=5，组1锁定仍为1。
        pv = compute_preview(
            frequencies=FREQS, groups=GROUPS, edges=EDGES,
            current_assignment=CURRENT, locks={1: 1}, occupations=[],
            revoke=[], register=[{"frequency": 4, "start": 30, "end": 150}])
        assert pv["replacement"][1] == 1
        assert pv["replacement"][2] == 5


class TestFingerprintsAndToken:
    def _state(self, occupations=()):
        return dict(current_version=2, frequencies=FREQS, groups=GROUPS,
                    interferences=[{"id": 1, "group_a": 1, "group_b": 2,
                                    "min_separation": 3}],
                    occupations=list(occupations))

    def test_state_fingerprint_changes_with_any_dimension(self):
        base = state_fingerprint(**self._state())
        changed = self._state()
        changed["current_version"] = 3
        assert state_fingerprint(**changed) != base
        changed = self._state()
        changed["occupations"] = [occ(1, 1, 0, 10)]
        assert state_fingerprint(**changed) != base
        changed = self._state()
        changed["groups"] = GROUPS + [{"id": 4, "start": 0, "end": 9}]
        assert state_fingerprint(**changed) != base

    def test_state_fingerprint_stable_for_same_state(self):
        occupations = [occ(2, 3, 0, 10), occ(1, 1, 5, 9)]
        assert state_fingerprint(**self._state(occupations)) == \
            state_fingerprint(**self._state(list(reversed(occupations))))

    def test_change_fingerprint_ignores_order_and_duplicates(self):
        a = change_fingerprint(
            [1, 2], [{"frequency": 4, "start": 0, "end": 10}])
        b = change_fingerprint(
            [2, 1], [{"frequency": 4, "start": 0, "end": 10}])
        assert a == b
        assert canonical_change(
            [1, 1], [])["revoke"] == [1]
        # 不同频点/时段即不同变更
        c = change_fingerprint(
            [1, 2], [{"frequency": 5, "start": 0, "end": 10}])
        assert a != c

    def test_token_roundtrip(self):
        token = encode_snapshot_token(7, 3, "statehash", "changehash")
        body = decode_snapshot_token(token)
        assert body == {"sid": 7, "ver": 3, "state": "statehash",
                        "change": "changehash"}

    def test_token_rejects_garbage(self):
        with pytest.raises(ValueError):
            decode_snapshot_token("not-a-token!!!")
