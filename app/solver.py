"""频点分配求解器（回溯搜索 + 前向检查）。

求解目标（按优先级）：
1. 满足全部约束：时段相交的干扰组频点间距 >= 最小间距；锁定组使用指定频点；
   每个组全程只用一个频点，且频点属于场次可用集合；设备组时段与外部设备占用
   时段相交（左闭右开）时，被占用的频点不可选用。
2. 最小化与前一发布版相比“共有组”的改频数（无前一版时该目标恒为 0）。
3. 仍有并列时，按组编号升序排列的频点序列取字典序最小方案。

另提供无解分析：给出一个不可约的锁定组子集或冲突约束子集（干扰关系/占用），
用于向调用方说明导致无解的原因。
"""

from __future__ import annotations


class SolverLimitExceeded(Exception):
    """搜索节点数超过资源上限。"""


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """左闭右开时段 [start, end) 是否相交。"""
    return a_start < b_end and b_start < a_end


def build_forbidden(groups, occupations):
    """把占用记录展开为每个设备组被禁用的频点集合。

    :param groups:      组信息列表/字典 {id, start, end}（任一可按项取键）。
    :param occupations: 占用记录 [(id, frequency, occ_start, occ_end)] 或
                        字典列表（需含 frequency/start/end 键，可含 id）；
                        设备组时段与占用时段相交（左闭右开）时该频点禁用。
    :returns: {group_id: set(frequencies)}
    """
    forbidden: dict = {}
    for g in groups:
        gid = g["id"] if isinstance(g, dict) else g[0]
        g_start = g["start"] if isinstance(g, dict) else g[1]
        g_end = g["end"] if isinstance(g, dict) else g[2]
        blocked: set = set()
        for occ in occupations:
            if isinstance(occ, dict):
                freq = occ["frequency"]
                o_start, o_end = occ["start"], occ["end"]
            else:
                _, freq, o_start, o_end = occ
            if intervals_overlap(g_start, g_end, o_start, o_end):
                blocked.add(freq)
        forbidden[gid] = blocked
    return forbidden


class FrequencySolver:
    """对单个场次的一次规划求解。

    :param group_ids:   参与规划的组编号。
    :param frequencies: 可用整数频点集合。
    :param edges:       生效的干扰关系 [(group_a, group_b, min_separation)]，
                        调用方需已按“时段相交”过滤。
    :param previous:    前一发布版的分配 {group_id: frequency}，仅共有组参与
                        改频数统计；无前一版时传 None（改频数目标恒为 0）。
    :param forbidden:   每个组因外部设备占用而禁用的频点 {group_id: set}。
    """

    def __init__(self, group_ids, frequencies, edges, previous=None,
                 forbidden=None, max_nodes: int = 2_000_000):
        self.groups = sorted(set(group_ids))
        self.frequencies = sorted(set(frequencies))
        self.previous = dict(previous or {})
        self.forbidden = {g: set() for g in self.groups}
        for gid, freqs in (forbidden or {}).items():
            if gid in self.forbidden:
                self.forbidden[gid] = set(freqs)
        self.adj = {g: [] for g in self.groups}
        for g1, g2, sep in edges:
            if g1 in self.adj and g2 in self.adj:
                self.adj[g1].append((g2, sep))
                self.adj[g2].append((g1, sep))
        self.max_nodes = max_nodes
        self.nodes = 0

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _tick(self):
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise SolverLimitExceeded(
                f"search exceeded {self.max_nodes} nodes")

    def _initial_domains(self, locks):
        domains = {g: set(self.frequencies) - self.forbidden[g]
                   for g in self.groups}
        available = set(self.frequencies)
        for g, f in locks.items():
            # 锁定不可用/被占用频点时域为空，搜索会立即判无解（API 层已先行
            # 拒绝不可用频点；被占用频点在 422 无解分析中说明相关占用）
            if g in domains:
                domains[g] = {f} & available - self.forbidden[g]
        return domains

    def _change_cost(self, group, value):
        prev = self.previous.get(group)
        return 1 if (prev is not None and prev != value) else 0

    def _propagate(self, domains, assignment, group, value, trail):
        """赋值 group=value 后做前向检查；与既有赋值冲突或清空某域时失败。"""
        for other, sep in self.adj[group]:
            if other in assignment:
                if abs(assignment[other] - value) < sep:
                    return False
                continue
            removed = [f for f in domains[other] if abs(f - value) < sep]
            if removed:
                for f in removed:
                    domains[other].discard(f)
                trail.append((other, removed))
                if not domains[other]:
                    return False
        return True

    @staticmethod
    def _undo(domains, trail):
        for other, removed in trail:
            domains[other].update(removed)

    def _search(self, domains, assignment, budget, lex):
        """回溯搜索一个改频数 <= budget 的解。

        lex=True 时按组编号升序选变量、频点升序试值，找到的第一个完整解
        即为“按组编号升序的频点序列”字典序最小解；lex=False 时按 MRV
        启发式选变量，仅用于可行性判断。
        """
        self._tick()
        if len(assignment) == len(self.groups):
            return dict(assignment)
        if lex:
            group = next(g for g in self.groups if g not in assignment)
        else:
            group = min((g for g in self.groups if g not in assignment),
                        key=lambda g: len(domains[g]))
        if not domains[group]:
            return None
        for value in sorted(domains[group]):
            cost = self._change_cost(group, value)
            if cost > budget:
                continue
            trail = []
            assignment[group] = value
            if self._propagate(domains, assignment, group, value, trail):
                result = self._search(domains, assignment, budget - cost, lex)
                if result is not None:
                    return result
            self._undo(domains, trail)
            del assignment[group]
        return None

    @staticmethod
    def _clone(domains):
        return {g: set(d) for g, d in domains.items()}

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def solve(self, locks=None):
        """返回 (assignment, changes)；无解返回 None。

        assignment: {group_id: frequency}；changes: 共有组改频数。
        """
        locks = dict(locks or {})
        domains = self._initial_domains(locks)
        common = sum(1 for g in self.groups if g in self.previous)

        # 改频数最多为 common，故 budget=common 等价于不限制改频；
        # 先以此判断是否存在任何可行解（无解时只需这一次搜索）。
        if self._search(self._clone(domains), {}, common, lex=False) is None:
            return None

        # 线性上探最小可行改频数 k*
        best_k = 0
        while best_k < common and self._search(
                self._clone(domains), {}, best_k, lex=False) is None:
            best_k += 1

        # 在改频数 <= k* 的约束下取字典序最小解
        result = self._search(self._clone(domains), {}, best_k, lex=True)
        changes = sum(1 for g, f in result.items()
                      if g in self.previous and self.previous[g] != f)
        return result, changes


def _minimize_core(unsat, items):
    """删除式最小化：返回移除任一元素都会变为可解的不可约子集。"""
    core = list(items)
    for item in list(core):
        trial = [x for x in core if x != item]
        if unsat(trial):
            core = trial
    return core


def analyze_unsat(group_ids, frequencies, edges, locks, previous=None,
                  occupations=(), group_intervals=None):
    """分析无解原因，返回结构化说明。

    - 若去掉锁定后仍无解：在“干扰关系 + 占用”并集上求不可约约束子集，
      含占用记录时 ``cause`` 为 ``"occupations"``，否则为 ``"interferences"``；
    - 否则：返回不可约的锁定子集，并附上涉及这些锁定组的干扰关系与占用。

    :param occupations: 占用记录字典列表（含 ``id``/``frequency``/``start``/
                        ``end``）；与设备组时段相交的占用构成该组的禁用频点。
    :param group_intervals: {group_id: (start, end)}，用于判定占用与哪些组相交；
                        提供占用时必须给出。
    """
    locks = dict(locks or {})
    occupations = list(occupations or [])
    groups = sorted(set(group_ids))
    intervals = group_intervals or {}

    # 每条占用对哪些组禁用哪个频点
    occ_blocks: list[dict[int, int]] = []
    for occ in occupations:
        blocked = {}
        for gid in groups:
            g_start, g_end = intervals.get(gid, (None, None))
            if g_start is None:
                continue
            if intervals_overlap(g_start, g_end, occ["start"], occ["end"]):
                blocked[gid] = occ["frequency"]
        occ_blocks.append(blocked)

    def make_forbidden(occ_indices):
        forbidden = {g: set() for g in groups}
        for idx in occ_indices:
            for gid, freq in occ_blocks[idx].items():
                forbidden[gid].add(freq)
        return forbidden

    def unsat_with(lock_list, edge_list, occ_indices=frozenset()):
        solver = FrequencySolver(
            groups, frequencies, edge_list, previous,
            forbidden=make_forbidden(occ_indices))
        return solver.solve(dict(lock_list)) is None

    if not unsat_with([], edges, range(len(occupations))):
        # 基础约束可满足：无解由锁定引入，最小化锁定子集
        core_locks = _minimize_core(
            lambda ls: unsat_with(ls, edges, range(len(occupations))),
            sorted(locks.items()))
        core_groups = {g for g, _ in core_locks}
        related_edges = [e for e in edges if e[0] in core_groups
                         or e[1] in core_groups]
        related_occ = []
        seen_occ: set = set()
        for idx, occ in enumerate(occupations):
            if occ["id"] in seen_occ:
                continue
            if set(occ_blocks[idx]) & core_groups:
                seen_occ.add(occ["id"])
                related_occ.append(occ)
        return {
            "cause": "locks",
            "locks": [{"group_id": g, "frequency": f}
                      for g, f in core_locks],
            "interferences": [
                {"group_a": a, "group_b": b, "min_separation": s}
                for a, b, s in related_edges],
            "occupations": [_occ_public(o) for o in related_occ],
        }

    # 基础约束本身不可满足：在 干扰关系 + 占用 上联合最小化
    items: list[tuple] = [("e", e) for e in edges]
    items += [("o", idx) for idx in range(len(occupations)) if occ_blocks[idx]]

    def unsat_items(trial):
        edge_list = [payload for kind, payload in trial if kind == "e"]
        occ_indices = [payload for kind, payload in trial if kind == "o"]
        return unsat_with([], edge_list, occ_indices)

    core_items = _minimize_core(unsat_items, items)
    core_edges = [payload for kind, payload in core_items if kind == "e"]
    core_occ_indices = sorted(payload for kind, payload in core_items
                              if kind == "o")
    cause = "occupations" if core_occ_indices else "interferences"
    return {
        "cause": cause,
        "locks": [],
        "interferences": [
            {"group_a": a, "group_b": b, "min_separation": s}
            for a, b, s in core_edges],
        "occupations": [_occ_public(occupations[i]) for i in core_occ_indices],
    }


def _occ_public(occ: dict) -> dict:
    """占用记录的对外结构（无内部字段时同样适用）。"""
    return {"id": occ.get("id"), "frequency": occ["frequency"],
            "start": occ["start"], "end": occ["end"]}
