"""现场占用调整预演的单元测试（纯计算，无需数据库）。"""

import pytest

from app.adjustment import (AdjustmentValidationError, apply_change,
                            pending_id, solve_replacement, unsat_explanation,
                            validate_change, violation_detail)

FREQUENCIES = [1, 2, 3, 4, 5, 6]


def groups():
    return [{"id": 1, "name": "a", "start": 0, "end": 120},
            {"id": 2, "name": "b", "start": 60, "end": 180},
            {"id": 3, "name": "c", "start": 120, "end": 240}]


# a-b 时段相交、b-c 相交、a-c 端点相接不相交；间距均为 3
EDGES = [(1, 2, 3), (2, 3, 3)]
CURRENT = {1: 1, 2: 4, 3: 1}


class TestValidateChange:
    def test_all_problems_reported_together(self):
        occupations = [{"id": 7, "frequency": 1, "start": 0, "end": 10}]
        with pytest.raises(AdjustmentValidationError) as exc:
            validate_change(
                FREQUENCIES, occupations, revoke_ids=[9, 9],
                additions=[{"frequency": 99, "start": 5, "end": 5}])
        detail = exc.value.detail
        assert detail["error"] == "invalid_adjustment"
        assert detail["unknown_revoke"] == [9]
        assert detail["duplicate_revoke"] == [9]
        assert detail["unavailable_frequencies"] == [
            {"index": 0, "frequency": 99}]
        assert detail["invalid_intervals"] == [0]

    def test_valid_change_passes(self):
        occupations = [{"id": 7, "frequency": 1, "start": 0, "end": 10}]
        validate_change(FREQUENCIES, occupations, [7],
                        [{"frequency": 2, "start": 0, "end": 5}])

    def test_empty_change_passes(self):
        validate_change(FREQUENCIES, [], [], [])

    def test_index_preserved(self):
        with pytest.raises(AdjustmentValidationError) as exc:
            validate_change(FREQUENCIES, [], [],
                            [{"frequency": 1, "start": 0, "end": 10},
                             {"frequency": 99, "start": 0, "end": 10,
                              "index": 5}])
        assert exc.value.detail["unavailable_frequencies"] == [
            {"index": 5, "frequency": 99}]


class TestApplyChange:
    def test_revoke_and_pending_ids(self):
        occs = [{"id": 1, "frequency": 1, "start": 0, "end": 10},
                {"id": 2, "frequency": 2, "start": 0, "end": 10}]
        kept, pending = apply_change(
            occs, [1],
            [{"frequency": 3, "start": 0, "end": 10},
             {"frequency": 4, "start": 5, "end": 15}])
        assert [o["id"] for o in kept] == [2]
        assert pending == [
            {"id": pending_id(0), "frequency": 3, "start": 0, "end": 10},
            {"id": pending_id(1), "frequency": 4, "start": 5, "end": 15}]


class TestViolationDetail:
    def test_current_feasible_no_violations(self):
        occ_after = [{"id": 1, "frequency": 6, "start": 0, "end": 300}]
        detail = violation_detail(groups(), CURRENT, EDGES, FREQUENCIES,
                                  occ_after)
        assert detail["violating_groups"] == []
        assert detail["occupations"] == []

    def test_occupation_hit_on_group_2(self):
        # 频点4 的 [30,150) 与 a、b、c 都相交；当前版仅组2=4 撞占用
        occ_after = [{"id": -1, "frequency": 4, "start": 30, "end": 150}]
        detail = violation_detail(groups(), CURRENT, EDGES, FREQUENCIES,
                                  occ_after)
        assert detail["violating_groups"] == [2]
        assert detail["occupations"] == [{"group_id": 2, "frequency": 4}]
        assert detail["related_occupations"] == [
            {"id": None, "index": 0, "frequency": 4,
             "start": 30, "end": 150}]

    def test_half_open_boundary_not_violation(self):
        # [60,180) 与组1 [0,60)、组3 [120,240) 不相交；组2 [60,180) 全相交
        # 且组2 当前频点为 4，与占用频点 1 不同；组1=1 在端点相接下不冲突。
        occ_after = [{"id": 1, "frequency": 4, "start": 60, "end": 180}]
        detail = violation_detail(groups(), CURRENT, EDGES, FREQUENCIES,
                                  occ_after)
        assert detail["violating_groups"] == [2]
        assert detail["occupations"] == [{"group_id": 2, "frequency": 4}]

    def test_interference_violation_contributes_groups(self):
        # 仅保留 a-b 生效边；构造当前版违反 a-b 间距（1 与 2）
        edges = [(1, 2, 3)]
        bad_current = {1: 1, 2: 2, 3: 1}
        detail = violation_detail(groups(), bad_current, edges,
                                  FREQUENCIES, [])
        assert detail["interferences"] == [
            {"group_a": 1, "group_b": 2, "min_separation": 3}]
        assert detail["violating_groups"] == [1, 2]


class TestSolveReplacement:
    def _solve(self, current, locks, occ_after):
        return solve_replacement(groups(), FREQUENCIES, EDGES, current,
                                 locks, occ_after)

    def test_feasible_current_zero_changes(self):
        assignment, changes = self._solve(CURRENT, {}, [])
        assert assignment == CURRENT
        assert changes == 0

    def test_minimal_change_then_lex_minimum(self):
        # 频点4 在 [30,150) 被占：组2 必须离开 4；组1=1 不动，组3 不受影响，
        # 最小改频数为 1；组2 可行值 5、6 中取字典序最小 -> 5
        occ = [{"id": -1, "frequency": 4, "start": 30, "end": 150}]
        assignment, changes = self._solve(CURRENT, {}, occ)
        assert assignment == {1: 1, 2: 5, 3: 1}
        assert changes == 1

    def test_locks_from_current_plan_are_kept(self):
        # 当前版锁定组2=4；占用使 4 对组2不可用 -> 无解（不允许改锁定组）
        occ = [{"id": -1, "frequency": 4, "start": 30, "end": 150}]
        assert self._solve(CURRENT, {2: 4}, occ) is None

    def test_lex_tie_break_across_groups(self):
        # 占用 4 与 1 覆盖全场：组1 离开1、组2 离开4、组3 离开1；
        # 最小改频数为 3（全部必改），字典序最小解即各组可选最小值。
        occs = [{"id": -1, "frequency": 4, "start": 0, "end": 300},
                {"id": -2, "frequency": 1, "start": 0, "end": 300}]
        assignment, changes = self._solve(CURRENT, {}, occs)
        # 组1 最小可取 2（与组2 间距 >=3），组3 取 2（b-c 间距约束组2）
        assert changes == 3
        assert assignment[1] == 2

    def test_pending_and_existing_occ_both_forbidden(self):
        occs = [{"id": 5, "frequency": 5, "start": 60, "end": 180},
                {"id": -1, "frequency": 4, "start": 60, "end": 180}]
        assignment, changes = self._solve(CURRENT, {}, occs)
        # 组2 的 4、5 均不可用 -> 改到 6，仅组2 改频
        assert assignment == {1: 1, 2: 6, 3: 1}
        assert changes == 1


class TestUnsatExplanation:
    def test_locked_group_explained_with_pending_occupations(self):
        occs = [{"id": -1, "frequency": 1, "start": 0, "end": 60},
                {"id": -2, "frequency": 2, "start": 0, "end": 60}]
        one_group = [groups()[0]]
        explanation = unsat_explanation(
            one_group, [1, 2], [], {1: 1}, {1: 1}, occs)
        assert explanation["cause"] == "occupations"
        assert explanation["locks"] == [{"group_id": 1, "frequency": 1}]
        refs = explanation["occupations"]
        assert refs == [
            {"id": None, "index": 0, "frequency": 1, "start": 0, "end": 60},
            {"id": None, "index": 1, "frequency": 2, "start": 0, "end": 60}]

    def test_mixed_existing_and_pending_references(self):
        occs = [{"id": 8, "frequency": 1, "start": 0, "end": 60},
                {"id": -1, "frequency": 2, "start": 0, "end": 60}]
        one_group = [groups()[0]]
        explanation = unsat_explanation(
            one_group, [1, 2], [], {1: 1}, {1: 1}, occs)
        assert explanation["cause"] == "occupations"
        ids = {(o["id"], o.get("index")) for o in explanation["occupations"]}
        assert ids == {(8, None), (None, 0)}
