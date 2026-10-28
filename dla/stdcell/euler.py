"""Gate ordering: common Euler trails of the NMOS and PMOS diffusion graphs.

Transistors are edges between their source/drain nets. A *column* holds one NMOS and
one PMOS transistor with the same gate signal (one vertical poly line). Consecutive
columns share their diffusion when the nets match; otherwise that row gets a diffusion
break. The two rows break independently (Uehara & vanCleemput, 1981): a column may
break only the NMOS row, only the PMOS row, or both.

The search minimises the number of broken column transitions, then the number of broken
rows (a single-row break keeps the other row's diffusion continuous), by dynamic
programming over the sets of placed transistors, and then enumerates the optimal
orders. Identical devices (same gate and source/drain nets, e.g. parallel fingers) are
interchangeable, so only the lowest-index unplaced one of each class is tried.
"""
from functools import lru_cache

MAX_DEVICES = 24        # per row
_W = 64                 # cost weight of a broken transition (> broken rows of any order)


def same_device(a, b):
    return (a.g, min(a.a, a.b), max(a.a, a.b)) == (b.g, min(b.a, b.b), max(b.a, b.b))


def orders(cell, limit=400):
    """(breaks, sequences): up to `limit` optimal column sequences.

    Each sequence is a list of (gate, (n_left, n_right), (p_left, p_right), brk, (n_dev, p_dev)).
    brk tells which rows break before the column: False (none), "n" (NMOS only),
    "p" (PMOS only) or True (both); ``bool(brk)`` is True for any break. `breaks` is the
    number of broken column transitions (0 = one continuous diffusion per row).
    """
    N = [d for d in cell.devices if d.kind == "n"]
    P = [d for d in cell.devices if d.kind == "p"]
    if len(N) != len(P) or len(N) > MAX_DEVICES:
        raise ValueError("%s: unsupported network (%d n / %d p devices)" % (cell.name, len(N), len(P)))
    FULL_N, FULL_P = (1 << len(N)) - 1, (1 << len(P)) - 1
    twin_n, twin_p = _twins(N), _twins(P)

    def moves(mn, mp, en, ep):
        """(cost, new state, column) for every distinct next column."""
        seen = set()
        out = []
        for i, dn in enumerate(N):
            if mn >> i & 1 or (twin_n[i] >= 0 and not mn >> twin_n[i] & 1):
                continue                        # placed, or an identical lower device is still free
            for k, dp in enumerate(P):
                if mp >> k & 1 or dp.g != dn.g or (twin_p[k] >= 0 and not mp >> twin_p[k] & 1):
                    continue
                for (nl, nr) in ((dn.a, dn.b), (dn.b, dn.a)):
                    brk_n = en is not None and nl != en
                    for (pl, pr) in ((dp.a, dp.b), (dp.b, dp.a)):
                        brk_p = ep is not None and pl != ep
                        brk = (brk_n and brk_p) or ("n" if brk_n else "p" if brk_p else False)
                        key = (dn.g, nl, nr, pl, pr)    # with the twin rule this fixes the new masks
                        if key in seen:
                            continue
                        seen.add(key)
                        cost = _W * (brk_n or brk_p) + brk_n + brk_p
                        out.append((cost, (mn | 1 << i, mp | 1 << k, nr, pr),
                                    (dn.g, (nl, nr), (pl, pr), brk, (dn.name, dp.name))))
        return out

    @lru_cache(maxsize=None)
    def best(mn, mp, en, ep):
        if mn == FULL_N and mp == FULL_P:
            return 0
        return min((c + best(*st) for c, st, _ in moves(mn, mp, en, ep)), default=_INF)

    total = best(0, 0, None, None)
    if total >= _INF:
        best.cache_clear()
        raise ValueError("%s: NMOS and PMOS gate signals cannot be paired" % cell.name)
    res = []

    def walk(state, acc, cost):
        if len(res) >= limit:
            return
        mn, mp, en, ep = state
        if mn == FULL_N and mp == FULL_P:
            res.append(list(acc))
            return
        for c, st, col in sorted(moves(mn, mp, en, ep), key=lambda m: m[0]):
            if cost + c + best(*st) == total:
                acc.append(col)
                walk(st, acc, cost + c)
                acc.pop()

    walk((0, 0, None, None), [], 0)
    best.cache_clear()
    return total // _W, res


_INF = 1 << 30


def _twins(devs):
    """Index of the nearest lower identical device (or -1) for every device."""
    out = []
    for i, d in enumerate(devs):
        out.append(next((j for j in range(i - 1, -1, -1) if same_device(devs[j], d)), -1))
    return out


def _sig(devs, mask):
    """Multiset of placed devices (so identical parallel fingers are not enumerated twice)."""
    return tuple(sorted((d.g, min(d.a, d.b), max(d.a, d.b)) for i, d in enumerate(devs) if mask >> i & 1))
