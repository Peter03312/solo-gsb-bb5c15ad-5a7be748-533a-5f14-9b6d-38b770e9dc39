"""逐组切换路线求解器（重配置序列规划）。

现场设备无法把新发布版的频点一次切换到位，必须沿一条逐组调频的路线完成
切换。本模块在两个均已可行的端点分配之间，寻找满足以下条件的操作序列：

1. 每一步只把一个组改到本场次的可用频点（可借用临时频点），不可调频组
   全程不动；设备组时段与外部设备占用时段相交（左闭右开）时，被占用频点
   在起点、终点及每一个临时步骤中都不可选用（占用取一次一致快照）；
2. 每一步之后（含起点与终点）的完整分配都满足当前按时段相交生效的干扰
   关系及最小频点间距；
3. 先最小化步骤数，再按各步的 (组编号, 新频点) 序列取字典序最小。

若两端点本身可行、但配置空间中不存在这样的序列（冻结组件），返回
``NO_SAFE_ROUTE``。

算法：按约束连通分量分解，各分量相互独立、可分别求最优后按字典序贪心
交错；单分量在贪心构造的步数上界内做按字典序枚举后继的广度优先搜索，
保证最少步数且字典序最小。
"""

from __future__ import annotations

from collections import deque

NO_SAFE_ROUTE = "no_safe_route"


class RetuneLimitExceeded(Exception):
    """搜索状态数超过资源上限。"""


def violations(groups, assignment, edges, frequencies, forbidden=None):
    """返回端点分配的不可行之处：不可用频点、违反的干扰关系与占用冲突。

    :param groups:      全部组编号。
    :param assignment:  {group_id: frequency}。
    :param edges:       调用方已按“时段相交”过滤的生效干扰关系。
    :param frequencies: 场次可用频点集合。
    :param forbidden:   {group_id: set(frequencies)}，外部设备占用导致该组
                        时段内禁用的频点（一次一致的当前占用快照）。
    """
    available = set(frequencies)
    forbidden = forbidden or {}
    unavailable = [
        {"group_id": g, "frequency": assignment[g]}
        for g in sorted(groups) if assignment[g] not in available]
    bad_edges = [
        {"group_a": a, "group_b": b, "min_separation": sep}
        for a, b, sep in edges if abs(assignment[a] - assignment[b]) < sep]
    bad_occupations = [
        {"group_id": g, "frequency": assignment[g]}
        for g in sorted(groups) if assignment[g] in forbidden.get(g, ())]
    return {"unavailable": unavailable, "interferences": bad_edges,
            "occupations": bad_occupations}


class RetunePlanner:
    """一次逐组切换路线规划。

    :param group_ids:   本场次当前组编号集合（两端点的组集合须与之相同）。
    :param frequencies: 本场次可用整数频点集合。
    :param edges:       生效的干扰关系 [(group_a, group_b, min_separation)]，
                        调用方需已按“时段相交”过滤。
    :param start:       起始版分配 {group_id: frequency}。
    :param target:      目标版分配 {group_id: frequency}。
    :param fixed:       切换期间不可调频的组集合。
    :param forbidden:   {group_id: set(frequencies)}，外部设备占用导致该组
                        时段内禁用的频点；起点、终点与每个临时步骤都受其约束。
    """

    def __init__(self, group_ids, frequencies, edges, start, target,
                 fixed=(), forbidden=None, max_states: int = 200_000):
        self.groups = sorted(set(group_ids))
        self.frequencies = sorted(set(frequencies))
        self.start = dict(start)
        self.target = dict(target)
        self.fixed = set(fixed)
        self.forbidden = {g: set() for g in self.groups}
        for gid, freqs in (forbidden or {}).items():
            if gid in self.forbidden:
                self.forbidden[gid] = set(freqs)
        self.edges = [(a, b, s) for a, b, s in edges]
        self.adj = {g: [] for g in self.groups}
        for g1, g2, sep in self.edges:
            if g1 in self.adj and g2 in self.adj:
                self.adj[g1].append((g2, sep))
                self.adj[g2].append((g1, sep))
        self.max_states = max_states
        self._used = 0

    def endpoint_violations(self):
        """检查两个端点是否可行；返回 (起点问题, 终点问题)，均为空才可行。"""
        return (violations(self.groups, self.start, self.edges,
                           self.frequencies, self.forbidden),
                violations(self.groups, self.target, self.edges,
                           self.frequencies, self.forbidden))

    # ------------------------------------------------------------------ #
    # 贪心构造路线长度上界（可达时供 BFS 限深；不可达时不可信）
    # ------------------------------------------------------------------ #
    def _greedy(self, members, lidx, initial, goal, freq_desc, group_desc,
                max_len: int = 10_000):
        """逐步直接改位；无法直接改位时把某个挡住的邻居临时挪开。

        四种变体（临时频点升/降序 × 挡路组编号升/降序）任一成功即可，
        成功路径长度作为 BFS 的深度上界。
        """
        state = initial
        path = []
        seen = {state}
        ordered = sorted(members, reverse=group_desc)
        for _ in range(max_len):
            if state == goal:
                return path
            moved = False
            for g in ordered:
                pos = lidx[g]
                if state[pos] == goal[pos] or g in self.fixed:
                    continue
                tgt = goal[pos]
                blockers = [o for o, sep in self.adj[g]
                            if abs(state[lidx[o]] - tgt) < sep]
                if blockers:
                    candidates = []
                    for b in blockers:
                        if b in self.fixed:
                            continue
                        bpos = lidx[b]
                        vals = [f for f in self.frequencies
                                if f != state[bpos]
                                and f not in self.forbidden[b]
                                and all(
                                    abs(state[lidx[o]] - f) >= sep2
                                    for o, sep2 in self.adj[b])]
                        if vals:
                            candidates.append((b, sorted(vals)))
                    if not candidates:
                        continue
                    b, vals = min(
                        candidates,
                        key=lambda cv: (-cv[0] if group_desc else cv[0]))
                    f = vals[-1] if freq_desc else vals[0]
                    bpos = lidx[b]
                    state = state[:bpos] + (f,) + state[bpos + 1:]
                    path.append((b, f))
                elif all(abs(state[lidx[o]] - tgt) >= sep
                         for o, sep in self.adj[g]):
                    state = state[:pos] + (tgt,) + state[pos + 1:]
                    path.append((g, tgt))
                else:
                    continue
                if state in seen:
                    return None
                seen.add(state)
                moved = True
                break
            if not moved:
                return None
        return None

    # ------------------------------------------------------------------ #
    # 单连通分量：限深 BFS，后继按 (组编号, 新频点) 字典序枚举
    # ------------------------------------------------------------------ #
    def _plan_component(self, component):
        members = sorted(component)
        lidx = {g: i for i, g in enumerate(members)}
        initial = tuple(self.start[g] for g in members)
        goal = tuple(self.target[g] for g in members)
        if initial == goal:
            return []

        depth_limit = None
        for freq_desc in (False, True):
            for group_desc in (False, True):
                upper = self._greedy(members, lidx, initial, goal,
                                     freq_desc, group_desc)
                if upper is not None:
                    depth_limit = (len(upper) if depth_limit is None
                                   else min(depth_limit, len(upper)))
        if depth_limit is None:
            # 贪心未找到路线：退化为全状态空间深度（受 max_states 保护）
            total_states = 1
            for _ in members:
                total_states *= len(self.frequencies)
                if total_states > self.max_states:
                    total_states = self.max_states
                    break
            depth_limit = total_states

        queue = deque([(initial, 0)])
        depth = {initial: 0}
        parent: dict[tuple, tuple] = {}

        def register(st: tuple):
            self._used += 1
            if self._used > self.max_states:
                raise RetuneLimitExceeded(
                    f"retune search exceeded {self.max_states} states")

        register(initial)
        freqs = self.frequencies
        fixed_members = self.fixed

        while queue:
            state, d = queue.popleft()
            if state == goal:
                break
            if d >= depth_limit:
                continue
            for g in members:
                if g in fixed_members:
                    continue
                pos = lidx[g]
                cur = state[pos]
                base = state[:pos]
                tail = state[pos + 1:]
                for value in freqs:  # 频点升序；跳过当前频点（空操作不优）
                    if value == cur or value in self.forbidden[g]:
                        continue
                    for o, sep in self.adj[g]:
                        if abs(state[lidx[o]] - value) < sep:
                            break
                    else:
                        # for 正常结束 = 与全部邻居间距满足，移动合法
                        nxt = base + (value,) + tail
                        if nxt not in depth:
                            register(nxt)
                            depth[nxt] = d + 1
                            parent[nxt] = (state, (g, value))
                            queue.append((nxt, d + 1))
        if goal not in depth:
            return NO_SAFE_ROUTE
        moves = []
        st = goal
        while st != initial:
            st, move = parent[st]
            moves.append(move)
        moves.reverse()
        return moves

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def plan(self):
        """返回切换路线 [(group_id, new_frequency), ...]；无安全路线返回
        ``NO_SAFE_ROUTE``。端点可行性由调用方先行校验。"""
        # 约束连通分量（固定组同样作为障碍参与连通性）
        remaining = set(self.groups)
        components = []
        while remaining:
            seed = remaining.pop()
            comp = [seed]
            stack = [seed]
            while stack:
                g = stack.pop()
                for o, _sep in self.adj[g]:
                    if o in remaining:
                        remaining.discard(o)
                        stack.append(o)
                        comp.append(o)
            components.append(comp)

        # 各分量独立求最优；分量之间没有约束，顺序可任意交错
        component_paths = []
        for comp in components:
            path = self._plan_component(comp)
            if path is NO_SAFE_ROUTE:
                return NO_SAFE_ROUTE
            if path:
                component_paths.append(deque(path))

        # 最短长度 = 各分量长度之和；全局字典序最小通过逐步取各队首最小
        # (组编号, 新频点) 的贪心交错得到
        merged = []
        while component_paths:
            best_i = min(range(len(component_paths)),
                         key=lambda i: component_paths[i][0])
            merged.append(component_paths[best_i].popleft())
            if not component_paths[best_i]:
                component_paths.pop(best_i)
        return merged
