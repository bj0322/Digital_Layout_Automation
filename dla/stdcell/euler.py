"""Gate ordering: common Euler trails of the NMOS and PMOS diffusion graphs.

Transistors are edges between their source/drain nets. A *column* holds one NMOS and
one PMOS transistor with the same gate signal (one vertical poly line). Consecutive
columns share their diffusion when the nets match; otherwise both rows get a
diffusion break. The search minimises the number of breaks (dynamic programming over
the sets of placed transistors) and then enumerates the optimal orders.
"""
from functools import lru_cache


def orders(cell, limit=400):
    """Up to `limit` optimal column sequences.

    Each sequence is a list of (gate, (n_left, n_right), (p_left, p_right), brk, (n_dev, p_dev)),
    brk = True if a diffusion break precedes the column.
    """
    N = [d for d in cell.devices if d.kind == "n"]
    P = [d for d in cell.devices if d.kind == "p"]
    if len(N) != len(P) or len(N) > 12:
        raise ValueError("%s: unsupported network (%d n / %d p devices)" % (cell.name, len(N), len(P)))
    FULL_N, FULL_P = (1 << len(N)) - 1, (1 << len(P)) - 1

    def moves(mn, mp, en, ep):
        """(cost, new state, column) for every distinct next column."""
        seen = set()
        out = []
        for i, dn in enumerate(N):
            if mn >> i & 1:
                continue
            for k, dp in enumerate(P):
                if mp >> k & 1 or dp.g != dn.g:
                    continue
                for (nl, nr) in ((dn.a, dn.b), (dn.b, dn.a)):
                    for (pl, pr) in ((dp.a, dp.b), (dp.b, dp.a)):
                        brk = en is not None and (nl != en or pl != ep)
                        key = (dn.g, nl, nr, pl, pr, brk, _sig(N, mn | 1 << i), _sig(P, mp | 1 << k))
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append((int(brk), (mn | 1 << i, mp | 1 << k, nr, pr),
                                    (dn.g, (nl, nr), (pl, pr), brk, (dn.name, dp.name))))
        return out

    @lru_cache(maxsize=None)
    def best(mn, mp, en, ep):
        if mn == FULL_N and mp == FULL_P:
            return 0
        return min(c + best(*st) for c, st, _ in moves(mn, mp, en, ep))

    target = best(0, 0, None, None)
    res = []

    def walk(state, acc, cost):
        if len(res) >= limit:
            return
        mn, mp, en, ep = state
        if mn == FULL_N and mp == FULL_P:
            res.append(list(acc))
            return
        for c, st, col in sorted(moves(mn, mp, en, ep), key=lambda m: m[0]):
            if cost + c + best(*st) == target:
                acc.append(col)
                walk(st, acc, cost + c)
                acc.pop()

    walk((0, 0, None, None), [], 0)
    best.cache_clear()
    return target, res


def _sig(devs, mask):
    """Multiset of placed devices (so identical parallel fingers are not enumerated twice)."""
    return tuple(sorted((d.g, min(d.a, d.b), max(d.a, d.b)) for i, d in enumerate(devs) if mask >> i & 1))
