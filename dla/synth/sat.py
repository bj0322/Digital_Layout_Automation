"""Small CDCL SAT solver in pure Python (for equivalence checking and SAT sweeping).

    s = Solver()
    a, b = s.new_var(), s.new_var()
    s.add_clause([a, b]); s.add_clause([-a, b])
    s.solve()                    # True; s.value(b) == True
    s.solve(assumptions=[-b])    # False

Literals are non-zero ints (DIMACS style: v / -v). Features: two watched literals,
first-UIP clause learning with recursive minimisation, activity-based decisions
(VSIDS with a lazy heap), phase saving, Luby restarts, assumptions, and a conflict
limit (`solve` then returns None). Clauses added between calls are kept, so the
solver is incremental.
"""
import heapq


def _luby(i):
    k = 1
    while (1 << k) - 1 < i:
        k += 1
    while True:
        if i == (1 << k) - 1:
            return 1 << (k - 1)
        i -= (1 << (k - 1)) - 1
        k = 1
        while (1 << k) - 1 < i:
            k += 1


class Solver:
    def __init__(self):
        self.nv = 0
        self.clauses = []            # list of lists of internal literals (2v / 2v+1)
        self.watches = [[], []]      # per internal literal: clause indices watching it
        self.val = [None]            # per var: None / True / False
        self.level = [0]
        self.reason = [None]
        self.act = [0.0]
        self.phase = [False]
        self.trail = []
        self.lim = []
        self.qhead = 0
        self.inc = 1.0
        self.heap = []
        self.ok = True
        self.conflicts = 0

    # ------------------------------------------------------------------ setup
    def new_var(self):
        self.nv += 1
        self.val.append(None)
        self.level.append(0)
        self.reason.append(None)
        self.act.append(0.0)
        self.phase.append(False)
        self.watches += [[], []]
        heapq.heappush(self.heap, (0.0, self.nv))
        return self.nv

    @staticmethod
    def _i(l):
        return 2 * l if l > 0 else -2 * l + 1

    def _lval(self, il):
        v = self.val[il >> 1]
        if v is None:
            return None
        return v != bool(il & 1)

    def add_clause(self, lits):
        if not self.ok:
            return False
        if self.lim:
            self._cancel(0)
        seen = set()
        cl = []
        for l in lits:
            il = self._i(l)
            if il ^ 1 in seen:
                return True                       # tautology
            if il in seen:
                continue
            v = self._lval(il)
            if v is True and self.level[il >> 1] == 0:
                return True
            if v is False and self.level[il >> 1] == 0:
                continue
            seen.add(il)
            cl.append(il)
        if not cl:
            self.ok = False
            return False
        if len(cl) == 1:
            self._assign(cl[0], None)
            if self._propagate() is not None:
                self.ok = False
                return False
            return True
        ci = len(self.clauses)
        self.clauses.append(cl)
        self.watches[cl[0] ^ 1].append(ci)
        self.watches[cl[1] ^ 1].append(ci)
        return True

    # ------------------------------------------------------------------ core
    def _assign(self, il, reason):
        v = il >> 1
        self.val[v] = not (il & 1)
        self.level[v] = len(self.lim)
        self.reason[v] = reason
        self.trail.append(il)

    def _propagate(self):
        """Returns a conflicting clause index or None."""
        val, clauses, watches = self.val, self.clauses, self.watches
        while self.qhead < len(self.trail):
            p = self.trail[self.qhead]          # p became true; clauses watching ~p ... stored under p
            self.qhead += 1
            ws = watches[p]
            i = j = 0
            n = len(ws)
            false_lit = p ^ 1
            while i < n:
                ci = ws[i]
                i += 1
                c = clauses[ci]
                if c[0] == false_lit:
                    c[0], c[1] = c[1], c[0]
                first = c[0]
                fv = val[first >> 1]
                if fv is not None and fv != bool(first & 1):
                    ws[j] = ci
                    j += 1
                    continue
                found = False
                for k in range(2, len(c)):
                    l = c[k]
                    lv = val[l >> 1]
                    if lv is None or lv != bool(l & 1):
                        c[1], c[k] = l, false_lit
                        watches[l ^ 1].append(ci)
                        found = True
                        break
                if found:
                    continue
                ws[j] = ci
                j += 1
                if fv is None:
                    self._assign(first, ci)
                else:
                    while i < n:
                        ws[j] = ws[i]
                        j += 1
                        i += 1
                    del ws[j:]
                    return ci
            del ws[j:]
        return None

    def _bump(self, v):
        self.act[v] += self.inc
        if self.act[v] > 1e100:
            self.act = [a * 1e-100 for a in self.act]
            self.inc *= 1e-100
            self.heap = [(-self.act[x], x) for x in range(1, self.nv + 1) if self.val[x] is None]
            heapq.heapify(self.heap)
        elif self.val[v] is None:
            heapq.heappush(self.heap, (-self.act[v], v))

    def _analyze(self, ci):
        seen = set()
        learnt = [None]
        cur = len(self.lim)
        pathc = 0
        p = None
        idx = len(self.trail) - 1
        clause = self.clauses[ci]
        while True:
            for q in (clause if p is None else clause[1:]):
                v = q >> 1
                if v not in seen and self.level[v] > 0:
                    seen.add(v)
                    self._bump(v)
                    if self.level[v] >= cur:
                        pathc += 1
                    else:
                        learnt.append(q)
            while (self.trail[idx] >> 1) not in seen:
                idx -= 1
            p = self.trail[idx]
            idx -= 1
            v = p >> 1
            seen.discard(v)
            pathc -= 1
            if pathc == 0:
                break
            clause = self.clauses[self.reason[v]]
            if clause[0] != p:                    # reason clauses keep the implied literal first
                k = clause.index(p)
                clause[0], clause[k] = clause[k], clause[0]
        learnt[0] = p ^ 1
        # minimisation: drop literals implied by others in the clause
        inl = {l >> 1 for l in learnt}
        keep = [learnt[0]]
        for l in learnt[1:]:
            r = self.reason[l >> 1]
            if r is None or not all((q >> 1) in inl or self.level[q >> 1] == 0
                                    for q in self.clauses[r] if (q >> 1) != (l >> 1)):
                keep.append(l)
        learnt = keep
        if len(learnt) == 1:
            bl = 0
        else:
            mi = max(range(1, len(learnt)), key=lambda k: self.level[learnt[k] >> 1])
            learnt[1], learnt[mi] = learnt[mi], learnt[1]
            bl = self.level[learnt[1] >> 1]
        self.inc *= 1.05
        return learnt, bl

    def _cancel(self, lvl):
        if len(self.lim) <= lvl:
            return
        stop = self.lim[lvl]
        for il in self.trail[stop:]:
            v = il >> 1
            self.phase[v] = self.val[v]
            self.val[v] = None
            self.reason[v] = None
            heapq.heappush(self.heap, (-self.act[v], v))
        del self.trail[stop:]
        del self.lim[lvl:]
        self.qhead = len(self.trail)

    def _decide(self):
        while self.heap:
            _, v = heapq.heappop(self.heap)
            if self.val[v] is None:
                return 2 * v + (0 if self.phase[v] else 1)
        return None

    def solve(self, assumptions=(), conflict_limit=None):
        """True (model in value()), False (unsat under the assumptions) or None (limit)."""
        if not self.ok:
            return False
        self._cancel(0)
        if self._propagate() is not None:
            self.ok = False
            return False
        assume = [self._i(l) for l in assumptions]
        conflicts = 0
        restart_no = 1
        budget = 100 * _luby(restart_no)
        while True:
            ci = self._propagate()
            if ci is not None:
                conflicts += 1
                self.conflicts += 1
                if len(self.lim) == 0:
                    self.ok = False
                    return False
                if len(self.lim) <= len(assume):
                    # conflict among the assumptions only
                    learnt, bl = self._analyze(ci)
                    self._cancel(0)
                    return False
                learnt, bl = self._analyze(ci)
                self._cancel(bl)
                if len(learnt) == 1:
                    self._cancel(0)
                    self._assign(learnt[0], None)
                else:
                    cj = len(self.clauses)
                    self.clauses.append(learnt)
                    self.watches[learnt[0] ^ 1].append(cj)
                    self.watches[learnt[1] ^ 1].append(cj)
                    self._assign(learnt[0], cj)
                if conflict_limit is not None and conflicts >= conflict_limit:
                    self._cancel(0)
                    return None
                budget -= 1
                if budget <= 0:
                    restart_no += 1
                    budget = 100 * _luby(restart_no)
                    self._cancel(0)
                continue
            # next assumption or decision
            lvl = len(self.lim)
            if lvl < len(assume):
                il = assume[lvl]
                v = self._lval(il)
                if v is False:
                    self._cancel(0)
                    return False
                self.lim.append(len(self.trail))
                if v is None:
                    self._assign(il, None)
                continue
            il = self._decide()
            if il is None:
                self.model = list(self.val)
                self._cancel(0)
                return True
            self.lim.append(len(self.trail))
            self._assign(il, None)

    def value(self, lit):
        v = self.model[abs(lit)]
        if v is None:
            return None
        return v if lit > 0 else not v
