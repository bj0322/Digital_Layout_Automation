"""Tokenizer for LEF and DEF: whitespace separated, ';' '(' ')' are tokens of their own,
double-quoted strings stay one token, '#' starts a comment. Every token knows its line.
"""


class ParseError(ValueError):
    pass


def tokenize(text):
    """[(token, line)]"""
    out = []
    for ln, line in enumerate(text.splitlines(), 1):
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if ch.isspace():
                i += 1
            elif ch == "#":
                break
            elif ch == '"':
                j = line.find('"', i + 1)
                if j < 0:
                    raise ParseError("line %d: unterminated string" % ln)
                out.append((line[i:j + 1], ln))
                i = j + 1
            elif ch in ";()":
                out.append((ch, ln))
                i += 1
            else:
                j = i
                while j < n and not line[j].isspace() and line[j] not in ";()":
                    if line[j] == "\\" and j + 1 < n:      # escaped character (DEF names)
                        j += 1
                    j += 1
                out.append((line[i:j], ln))
                i = j
    return out


class Tokens:
    """Cursor over tokens with error messages that carry file and line."""

    def __init__(self, text, fname="<string>"):
        self.toks = tokenize(text)
        self.i = 0
        self.fname = fname

    def eof(self):
        return self.i >= len(self.toks)

    def line(self):
        return self.toks[min(self.i, len(self.toks) - 1)][1] if self.toks else 0

    def error(self, msg):
        return ParseError("%s:%d: %s" % (self.fname, self.line(), msg))

    def peek(self, k=0):
        j = self.i + k
        return self.toks[j][0] if j < len(self.toks) else None

    def next(self):
        if self.i >= len(self.toks):
            raise self.error("unexpected end of file")
        t = self.toks[self.i][0]
        self.i += 1
        return t

    def expect(self, *want):
        t = self.next()
        if t.upper() not in want:
            self.i -= 1
            raise self.error("expected %s, got %r" % (" or ".join(want), t))
        return t

    def num(self):
        t = self.next()
        try:
            return float(t)
        except ValueError:
            self.i -= 1
            raise self.error("expected a number, got %r" % t)

    def int(self):
        t = self.next()
        try:
            return int(t)
        except ValueError:
            self.i -= 1
            raise self.error("expected an integer, got %r" % t)

    def statement(self):
        """Tokens up to (not including) the next ';', which is consumed."""
        out = []
        while True:
            t = self.next()
            if t == ";":
                return out
            out.append(t)
