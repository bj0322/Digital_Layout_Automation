"""Word-level operators on vectors of AIG literals (LSB first): the bit-blasting toolkit.

    from dla.synth.words import Words
    w = Words(aig)
    s = w.add(a, b)            # len(a) == len(b); ripple carry, result has the same width
    lt = w.lt(a, b, signed=True)

Every function takes and returns lists of literals; constants are lists of 0/1.
"""


class Words:
    def __init__(self, g):
        self.g = g

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def const(value, width):
        return [(value >> i) & 1 for i in range(width)]

    @staticmethod
    def extend(bits, width, signed):
        if len(bits) >= width:
            return list(bits[:width])
        fill = bits[-1] if (signed and bits) else 0
        return list(bits) + [fill] * (width - len(bits))

    @staticmethod
    def is_const(bits):
        return all(b <= 1 for b in bits)

    @staticmethod
    def to_int(bits, signed=False):
        v = sum(b << i for i, b in enumerate(bits))
        if signed and bits and bits[-1]:
            v -= 1 << len(bits)
        return v

    def inv(self, a):
        return [x ^ 1 for x in a]

    def bitwise(self, op, a, b):
        g = self.g
        f = {"&": g.AND, "|": g.OR, "^": g.XOR, "~^": g.XNOR}[op]
        return [f(x, y) for x, y in zip(a, b)]

    def mux(self, s, a1, a0):
        return [self.g.MUX(s, x, y) for x, y in zip(a1, a0)]

    def reduce(self, op, a):
        g = self.g
        if op in ("&", "~&"):
            r = g.AND_N(a)
        elif op in ("|", "~|"):
            r = g.OR_N(a)
        else:
            r = g.XOR_N(a)
        return r ^ 1 if op.startswith("~") else r

    def bool(self, a):
        return self.g.OR_N(a)

    # ------------------------------------------------------------------ arithmetic
    def add(self, a, b, cin=0):
        g = self.g
        out = []
        c = cin
        for x, y in zip(a, b):
            t = g.XOR(x, y)
            out.append(g.XOR(t, c))
            c = g.OR(g.AND(x, y), g.AND(c, t))
        return out

    def add_carry(self, a, b, cin=0):
        """(sum, carry out)"""
        g = self.g
        out = []
        c = cin
        for x, y in zip(a, b):
            t = g.XOR(x, y)
            out.append(g.XOR(t, c))
            c = g.OR(g.AND(x, y), g.AND(c, t))
        return out, c

    def sub(self, a, b):
        return self.add(a, self.inv(b), 1)

    def neg(self, a):
        return self.add(self.inv(a), [0] * len(a), 1)

    def mul(self, a, b):
        """Low len(a) bits of a * b (shift-and-add)."""
        g = self.g
        w = len(a)
        acc = [0] * w
        for i in range(w):
            if b[i] == 0:
                continue
            pp = [0] * i + [g.AND(a[k], b[i]) for k in range(w - i)]
            acc = self.add(acc, pp)
        return acc

    def eq(self, a, b):
        return self.g.AND_N([self.g.XNOR(x, y) for x, y in zip(a, b)])

    def lt(self, a, b, signed=False):
        """a < b; carry out of a + ~b + 1 is 1 iff a >= b (unsigned)."""
        if signed and a:
            a = a[:-1] + [a[-1] ^ 1]
            b = b[:-1] + [b[-1] ^ 1]
        _, c = self.add_carry(a, self.inv(b), 1)
        return c ^ 1

    def compare(self, op, a, b, signed):
        if op in ("==", "==="):
            return self.eq(a, b)
        if op in ("!=", "!=="):
            return self.eq(a, b) ^ 1
        if op == "<":
            return self.lt(a, b, signed)
        if op == ">":
            return self.lt(b, a, signed)
        if op == "<=":
            return self.lt(b, a, signed) ^ 1
        if op == ">=":
            return self.lt(a, b, signed) ^ 1
        raise ValueError(op)

    # ------------------------------------------------------------------ shifts
    def shift(self, a, amt, left, fill=0):
        """Barrel shift of `a` by the unsigned vector `amt`; vacated bits get `fill`."""
        g = self.g
        w = len(a)
        cur = list(a)
        for k, s in enumerate(amt):
            if s == 0:
                continue
            d = 1 << k
            if d >= w:
                shifted = [0 if left else fill] * w
            elif left:
                shifted = [0] * d + cur[:w - d]
            else:
                shifted = cur[d:] + [fill] * d
            cur = [g.MUX(s, x, y) for x, y in zip(shifted, cur)]
        return cur

    def shift_const(self, a, n, left, fill=0):
        w = len(a)
        if n >= w:
            return [fill if not left else 0] * w
        return ([0] * n + a[:w - n]) if left else (a[n:] + [fill] * n)
