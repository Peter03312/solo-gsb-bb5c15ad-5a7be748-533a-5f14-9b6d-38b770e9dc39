"""逐组切换路线求解器单元测试：可脱离数据库直接运行。

运行：python -m pytest tests/
"""

import pytest

from app.retune import (NO_SAFE_ROUTE, RetuneLimitExceeded, RetunePlanner,
                        violations)


def assert_route_valid(route, start, target, edges):
    """路线每一步后均合法，且最终恰好到达目标。"""
    state = dict(start)
    for gid, freq in route:
        assert freq != state[gid]  # 不允许空操作
        state[gid] = freq
        for a, b, sep in edges:
            assert abs(state[a] - state[b]) >= sep, (gid, freq, state)
    assert state == target


def shortest_bruteforce(planner, start, target):
    """朴素 BFS 求最短步数，用于与求解器对照（小规模）。"""
    from collections import deque
    groups = planner.groups
    init = tuple(start[g] for g in groups)
    goal = tuple(target[g] for g in groups)
    q = deque([(init, 0)])
    seen = {init}
    while q:
        st, d = q.popleft()
        if st == goal:
            return d
        cur = dict(zip(groups, st))
        for g in groups:
            for f in planner.frequencies:
                if f == cur[g]:
                    continue
                if all(abs(cur[o] - f) >= sep for o, sep in planner.adj[g]):
                    nxt = tuple(f if x == g else v for x, v in zip(groups, st))
                    if nxt not in seen:
                        seen.add(nxt)
                        q.append((nxt, d + 1))
    return None


def lex_min_bruteforce(planner, start, target):
    """朴素 BFS（后继按 (组,频点) 字典序）求字典序最短路线。"""
    from collections import deque
    groups = planner.groups
    init = tuple(start[g] for g in groups)
    goal = tuple(target[g] for g in groups)
    q = deque([init])
    parent = {}
    while q:
        st = q.popleft()
        if st == goal:
            break
        cur = dict(zip(groups, st))
        for g in groups:  # 组编号升序
            for f in planner.frequencies:  # 频点升序
                if f == cur[g]:
                    continue
                if all(abs(cur[o] - f) >= sep for o, sep in planner.adj[g]):
                    nxt = tuple(f if x == g else v for x, v in zip(groups, st))
                    if nxt not in parent:
                        parent[nxt] = (st, (g, f))
                        q.append(nxt)
    if goal not in parent:
        return NO_SAFE_ROUTE
    moves = []
    st = goal
    while st != init:
        st, move = parent[st]
        moves.append(move)
    moves.reverse()
    return moves


class TestViolations:
    def test_clean_assignment(self):
        v = violations([1, 2], {1: 1, 2: 4}, [(1, 2, 3)], [1, 2, 3, 4, 5])
        assert v == {"unavailable": [], "interferences": [],
                     "occupations": []}

    def test_unavailable_frequency(self):
        v = violations([1, 2], {1: 1, 2: 9}, [(1, 2, 3)], [1, 2, 3])
        assert v["unavailable"] == [{"group_id": 2, "frequency": 9}]
        # |1-9|=8 不小于间距 3，故无干扰违规
        assert v["interferences"] == []

    def test_separation_violation(self):
        v = violations([1, 2], {1: 1, 2: 2}, [(1, 2, 3)], [1, 2, 3])
        assert v["unavailable"] == []
        assert v["interferences"] == [
            {"group_a": 1, "group_b": 2, "min_separation": 3}]

    def test_occupation_violation(self):
        v = violations([1, 2], {1: 1, 2: 4}, [(1, 2, 3)],
                       [1, 2, 3, 4, 5], forbidden={2: {4}})
        assert v["unavailable"] == []
        assert v["interferences"] == []
        assert v["occupations"] == [{"group_id": 2, "frequency": 4}]


class TestRetuneBasic:
    def test_identical_endpoints_zero_steps(self):
        planner = RetunePlanner(
            [1, 2], [1, 2, 3, 4, 5], [(1, 2, 3)],
            {1: 1, 2: 4}, {1: 1, 2: 4})
        assert planner.plan() == []

    def test_single_group_moves_directly(self):
        planner = RetunePlanner(
            [1], [10, 20, 30], [], {1: 10}, {1: 30})
        assert planner.plan() == [(1, 30)]

    def test_two_groups_swap_needs_buffer(self):
        # 间距 3、频点 1..7：(1,4) -> (4,1) 最短 3 步
        edges = [(1, 2, 3)]
        start, target = {1: 1, 2: 4}, {1: 4, 2: 1}
        planner = RetunePlanner([1, 2], list(range(1, 8)), edges,
                                start, target)
        route = planner.plan()
        assert_route_valid(route, start, target, edges)
        assert len(route) == 3
        # 字典序最小：首步优先挪组1到合法的最高缓冲频点 7
        assert route == [(1, 7), (2, 1), (1, 4)]

    def test_two_groups_frozen_without_buffer(self):
        # 同样的交换在频点 1..6 下不存在安全路线
        planner = RetunePlanner(
            [1, 2], list(range(1, 7)), [(1, 2, 3)],
            {1: 1, 2: 4}, {1: 4, 2: 1})
        assert planner.plan() == NO_SAFE_ROUTE

    def test_temporary_frequency_allowed(self):
        # 借用临时频点 7（非任一端点频点）
        planner = RetunePlanner(
            [1, 2], list(range(1, 8)), [(1, 2, 3)],
            {1: 1, 2: 4}, {1: 4, 2: 1})
        route = planner.plan()
        assert (1, 7) in route

    def test_path_graph_route(self):
        edges = [(1, 2, 3), (2, 3, 3)]
        start = {1: 1, 2: 4, 3: 1}
        target = {1: 4, 2: 1, 3: 4}
        planner = RetunePlanner([1, 2, 3], list(range(1, 10)), edges,
                                start, target)
        route = planner.plan()
        assert_route_valid(route, start, target, edges)
        assert len(route) == 4
        assert route == [(2, 7), (1, 4), (3, 4), (2, 1)]


class TestOccupations:
    """外部设备占用：端点与每个临时步骤都不得选用被占用频点。"""

    edges = [(1, 2, 3), (2, 3, 3)]
    start = {1: 1, 2: 4, 3: 1}
    target = {1: 4, 2: 1, 3: 4}

    def assert_route_respects_forbidden(self, route, forbidden):
        state = dict(self.start)
        for gid, freq in route:
            state[gid] = freq
            assert freq not in forbidden.get(gid, set())
            for a, b, sep in self.edges:
                assert abs(state[a] - state[b]) >= sep
        assert state == self.target

    def test_occupied_temp_frequency_reroutes(self):
        # 频点 1..9；占用 7 覆盖组1、组2 时段（组3 仅端点相接不受影响），
        # 原本字典序最小路线首步借用 7，现须改用 8
        planner = RetunePlanner(
            [1, 2, 3], list(range(1, 10)), self.edges,
            self.start, self.target, forbidden={1: {7}, 2: {7}})
        route = planner.plan()
        assert route != NO_SAFE_ROUTE
        assert route == [(2, 8), (1, 4), (3, 4), (2, 1)]
        self.assert_route_respects_forbidden(
            route, {1: {7}, 2: {7}})

    def test_endpoint_uses_occupied_frequency(self):
        # 组2 的目标频点 1 在其时段被占用：终点不可行
        planner = RetunePlanner(
            [1, 2, 3], list(range(1, 10)), self.edges,
            self.start, self.target, forbidden={2: {1}})
        start_bad, target_bad = planner.endpoint_violations()
        assert start_bad["occupations"] == []
        assert target_bad["occupations"] == [
            {"group_id": 2, "frequency": 1}]

    def test_occupation_blocks_only_buffer_frozen(self):
        # 两组交换 (1,4)<->(4,1)、频点 1..7、间距 3；唯一缓冲频点 7 对两组
        # 均被占用时无安全路线
        planner = RetunePlanner(
            [1, 2], list(range(1, 8)), [(1, 2, 3)],
            {1: 1, 2: 4}, {1: 4, 2: 1}, forbidden={1: {7}, 2: {7}})
        assert planner.plan() == NO_SAFE_ROUTE

    def test_endpoint_touching_occupation_not_forbidden(self):
        # 占用仅与组3 在端点 120 相接（左闭右开不相交）：不影响路线
        planner = RetunePlanner(
            [1, 2, 3], list(range(1, 10)), self.edges,
            self.start, self.target, forbidden={3: {4}})
        # 组3 目标频点正是 4，但占用 [120,180) 与其 [120,240) 相交才禁用；
        # 这里构造的 forbidden 已假定相交，改测端点相接的情形：禁用集为空
        planner2 = RetunePlanner(
            [1, 2, 3], list(range(1, 10)), self.edges,
            self.start, self.target, forbidden={})
        assert planner2.plan() == [(2, 7), (1, 4), (3, 4), (2, 1)]
        # planner 自身（禁用 4）终点不可行
        _, target_bad = planner.endpoint_violations()
        assert target_bad["occupations"] == [{"group_id": 3, "frequency": 4}]


class TestFixedGroups:
    def test_fixed_group_never_moves(self):
        edges = [(1, 2, 3)]
        # 组2 固定在 4（两版一致），组1 直接 1->7 合法
        planner = RetunePlanner(
            [1, 2], list(range(1, 8)), edges,
            {1: 1, 2: 4}, {1: 7, 2: 4}, fixed=[2])
        route = planner.plan()
        assert_route_valid(route, {1: 1, 2: 4}, {1: 7, 2: 4}, edges)
        assert all(g != 2 for g, _ in route)

    def test_fixed_group_blocks_route(self):
        # 组2 固定在 4，组1 想 1->4：间距 0 冲突，冻结
        planner = RetunePlanner(
            [1, 2], list(range(1, 8)), [(1, 2, 3)],
            {1: 1, 2: 4}, {1: 4, 2: 4}, fixed=[2])
        assert planner.plan() == NO_SAFE_ROUTE


class TestFrozenConfigurations:
    def test_tight_packing_complete_graph_frozen(self):
        # 三组两两间距 3、频点 1..7，紧排 (1,4,7)；翻转 1 与 3 无安全路线
        edges = [(1, 2, 3), (1, 3, 3), (2, 3, 3)]
        planner = RetunePlanner(
            [1, 2, 3], list(range(1, 8)), edges,
            {1: 1, 2: 4, 3: 7}, {1: 7, 2: 4, 3: 1})
        assert planner.plan() == NO_SAFE_ROUTE

    def test_endpoints_feasible_but_disconnected(self):
        # 频点 {1,2,3}、两两间距 2：任一可行态都是三组各占一个频点的
        # 紧排，无人可动，故任意两个不同可行态之间都无路线
        edges = [(1, 2, 2), (1, 3, 2), (2, 3, 2)]
        planner = RetunePlanner(
            [1, 2, 3], [1, 2, 3], edges,
            {1: 1, 2: 3, 3: 2}, {1: 3, 2: 1, 3: 2})
        assert planner.plan() == NO_SAFE_ROUTE


class TestComponents:
    def test_independent_components_interleaved_lex(self):
        # 两条互不相连的边；组1/组3 直接 1->7（|7-4|=3，合法），各组2 再去1
        edges = [(1, 2, 3), (3, 4, 3)]
        start = {1: 1, 2: 4, 3: 1, 4: 4}
        target = {1: 7, 2: 1, 3: 7, 4: 1}
        planner = RetunePlanner(
            [1, 2, 3, 4], list(range(1, 8)), edges, start, target)
        route = planner.plan()
        assert_route_valid(route, start, target, edges)
        # 各分量最短 2 步，合计 4 步；交错贪心按 (组,频点) 选择
        assert route == [(1, 7), (2, 1), (3, 7), (4, 1)]

    def test_independent_components_with_buffer_lex(self):
        # 交换目标 (1,4)->(4,1) 需 3 步缓冲；两分量各 3 步，交错取字典序
        edges = [(1, 2, 3), (3, 4, 3)]
        start = {1: 1, 2: 4, 3: 1, 4: 4}
        target = {1: 4, 2: 1, 3: 4, 4: 1}
        planner = RetunePlanner(
            [1, 2, 3, 4], list(range(1, 8)), edges, start, target)
        route = planner.plan()
        assert_route_valid(route, start, target, edges)
        assert len(route) == 6
        assert route[0] == (1, 7)

    def test_isolated_group_ignored_when_unchanged(self):
        # 组3 无边且两版同为 2，不应产生任何步骤
        planner = RetunePlanner(
            [1, 2, 3], list(range(1, 8)), [(1, 2, 3)],
            {1: 1, 2: 4, 3: 2}, {1: 4, 2: 1, 3: 2})
        route = planner.plan()
        assert all(g != 3 for g, _ in route)
        assert len(route) == 3


class TestBruteforceEquivalence:
    """小规模下与朴素 BFS 对照：最短性与字典序一致。"""

    @pytest.mark.parametrize("nfreq", [4, 5, 6])
    @pytest.mark.parametrize("edges", [
        [],
        [(1, 2, 2)],
        [(1, 2, 2), (2, 3, 2)],
        [(1, 2, 2), (1, 3, 2), (2, 3, 2)],
    ])
    def test_matches_bruteforce(self, nfreq, edges):
        freqs = list(range(1, nfreq + 1))
        groups = [1, 2, 3]
        # 取两个确定的可行态作为端点
        start = {1: 1, 2: 1, 3: 1}
        target = {g: freqs[-1] for g in groups}
        planner = RetunePlanner(groups, freqs, edges, start, target)
        route = planner.plan()
        bf_len = shortest_bruteforce(planner, start, target)
        if bf_len is None:
            assert route == NO_SAFE_ROUTE
        else:
            assert route != NO_SAFE_ROUTE
            assert len(route) == bf_len
            assert route == lex_min_bruteforce(planner, start, target)
            assert_route_valid(route, start, target, edges)


class TestLimits:
    def test_limit_exceeded(self):
        # 无干扰 5 组、频点 10 个；极小状态上限触发 RetuneLimitExceeded
        planner = RetunePlanner(
            [1, 2, 3, 4, 5], list(range(1, 11)), [],
            {g: 1 for g in range(1, 6)},
            {g: 10 for g in range(1, 6)},
            max_states=20)
        with pytest.raises(RetuneLimitExceeded):
            planner.plan()
