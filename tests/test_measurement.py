"""实测批次核对的单元测试：可脱离数据库直接运行。"""

from app.measurement import analyze_measurement


def _groups():
    # 组1[0,120)、组2[60,180)、组3[120,240)
    return [{"id": 1, "name": "a", "start": 0, "end": 120},
            {"id": 2, "name": "b", "start": 60, "end": 180},
            {"id": 3, "name": "c", "start": 120, "end": 240}]


class TestAnalyzeMeasurement:
    def test_all_clean(self):
        result = analyze_measurement(
            _groups(), {1: 1, 2: 4, 3: 1},
            available=set(range(1, 7)),
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[{"group_a": 1, "group_b": 2,
                            "min_separation": 3}],
            occupations=[])
        assert result["readings"] == [
            {"group_id": 1, "frequency": 1},
            {"group_id": 2, "frequency": 4},
            {"group_id": 3, "frequency": 1}]
        assert result["deviations"] == []
        assert result["unavailable_frequencies"] == []
        assert result["interference_violations"] == []
        assert result["occupation_conflicts"] == []
        assert result["anomalies"] == []

    def test_deviations_from_plan(self):
        result = analyze_measurement(
            _groups(), {1: 2, 2: 4, 3: 5},
            available=set(range(1, 7)),
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[], occupations=[])
        assert result["deviations"] == [
            {"group_id": 1, "plan_frequency": 1, "measured_frequency": 2},
            {"group_id": 3, "plan_frequency": 1, "measured_frequency": 5}]
        assert result["anomalies"] == ["deviation"]

    def test_unavailable_frequency_recorded_as_anomaly(self):
        # 实测频点不在可用集合内也要记录并标明异常
        result = analyze_measurement(
            _groups(), {1: 99, 2: 4, 3: 1},
            available={1, 2, 3, 4, 5, 6},
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[], occupations=[])
        assert result["unavailable_frequencies"] == [
            {"group_id": 1, "frequency": 99}]
        assert set(result["anomalies"]) == {
            "deviation", "unavailable_frequency"}

    def test_unavailable_frequency_matching_plan_still_flagged(self):
        # 与发布版相同但不在当前可用集合内：不算偏离，仍是不可用异常
        result = analyze_measurement(
            [{"id": 1, "start": 0, "end": 60}], {1: 4},
            available={1, 2, 3},
            plan_assignment={1: 4},
            interferences=[], occupations=[])
        assert result["deviations"] == []
        assert result["unavailable_frequencies"] == [
            {"group_id": 1, "frequency": 4}]

    def test_interference_violation_overlapping_periods(self):
        result = analyze_measurement(
            _groups(), {1: 4, 2: 4, 3: 1},
            available=set(range(1, 7)),
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[{"group_a": 1, "group_b": 2,
                            "min_separation": 3},
                           {"group_a": 2, "group_b": 3,
                            "min_separation": 3}],
            occupations=[])
        # 1-2 时段相交且同频（间距 0 < 3）违规；2-3 时段相交但间距 3 满足
        assert result["interference_violations"] == [{
            "group_a": 1, "group_b": 2, "min_separation": 3,
            "frequency_a": 4, "frequency_b": 4, "actual_separation": 0}]
        assert "interference_violation" in result["anomalies"]

    def test_interference_ignored_when_periods_meet_at_endpoint(self):
        # 组1[0,120) 与组3[120,240) 端点相接，即使同频也不构成违规
        result = analyze_measurement(
            _groups(), {1: 1, 2: 4, 3: 1},
            available=set(range(1, 7)),
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[{"group_a": 1, "group_b": 3,
                            "min_separation": 3}],
            occupations=[])
        assert result["interference_violations"] == []

    def test_edge_pair_normalized_and_sorted(self):
        # 反向录入的干扰关系输出按 (group_a, group_b) 升序归一
        result = analyze_measurement(
            _groups(), {1: 4, 2: 4, 3: 1},
            available=set(range(1, 7)),
            plan_assignment={1: 4, 2: 4, 3: 1},
            interferences=[{"group_a": 2, "group_b": 1,
                            "min_separation": 3}],
            occupations=[])
        assert result["interference_violations"][0]["group_a"] == 1
        assert result["interference_violations"][0]["group_b"] == 2

    def test_occupation_conflict_lists_all_matching_ids(self):
        # 组2 实测频点 4：两条占用同频且时段相交都应列出；端点相接不算
        occupations = [
            {"id": 10, "frequency": 4, "start": 0, "end": 100},   # 与组2相交
            {"id": 11, "frequency": 4, "start": 180, "end": 200},  # 组2端点相接
            {"id": 12, "frequency": 4, "start": 70, "end": 90},   # 与组2相交
        ]
        result = analyze_measurement(
            _groups(), {1: 1, 2: 4, 3: 1},
            available=set(range(1, 7)),
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[], occupations=occupations)
        assert result["occupation_conflicts"] == [
            {"group_id": 2, "frequency": 4, "occupation_ids": [10, 12]}]
        assert result["anomalies"] == ["occupation_conflict"]

    def test_occupation_conflict_requires_same_frequency(self):
        # 占用频点与实测频点不同：不冲突
        result = analyze_measurement(
            _groups(), {1: 1, 2: 4, 3: 1},
            available=set(range(1, 7)),
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[],
            occupations=[{"id": 5, "frequency": 5,
                          "start": 0, "end": 200}])
        assert result["occupation_conflicts"] == []

    def test_all_anomaly_kinds_together(self):
        occupations = [{"id": 3, "frequency": 99, "start": 0, "end": 120}]
        result = analyze_measurement(
            _groups(), {1: 99, 2: 99, 3: 1},
            available={1, 2, 3},
            plan_assignment={1: 1, 2: 4, 3: 1},
            interferences=[{"group_a": 1, "group_b": 2,
                            "min_separation": 5}],
            occupations=occupations)
        assert {d["group_id"] for d in result["deviations"]} == {1, 2}
        assert {u["group_id"] for u in
                result["unavailable_frequencies"]} == {1, 2}
        assert len(result["interference_violations"]) == 1
        # 组1[0,120) 与占用[0,120) 相交、组2[60,180) 也相交，均冲突
        assert {c["group_id"] for c in
                result["occupation_conflicts"]} == {1, 2}
        assert result["anomalies"] == [
            "deviation", "unavailable_frequency",
            "interference_violation", "occupation_conflict"]
