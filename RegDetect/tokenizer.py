"""The StreamWeave regex tokeniser: pattern text in, flat token stream out.

v1 is the classic Thompson core -- literals, concatenation, `|`, `()` and `*`.
Everything else (`+`, `?`, `.`, character classes, the `\\d \\w \\s` shorthands,
`{n,m}`, anchors) is recognised only so that it can be rejected *by name*, which
is the difference between a useful error and "unexpected character".

One left-to-right pass, no backtracking, no lookahead beyond a single character.
The `re` module is deliberately not used anywhere: using a regex engine to build
a regex compiler is circular.

The alphabet is bytes. The hardware reads one byte per clock, so every symbol is
an `int` in 0..255, never a `str`.

What the tokeniser deliberately does *not* decide:

- Whether the pattern is empty. `tokenize("")` returns `[EOF]`, which is the
  correct token stream for empty input. The empty regex is rejected by the
  *grammar* (`concatenation := repetition+` needs at least one operand), so the
  parser raises that error, not this module.
- Whether the parentheses balance, or whether `*` has an operand. Those are
  shapes, and shapes are the parser's job.

Usage:

    python -m streamweave.tokenizer '(a|b)*abb'
"""

import enum
import sys
from dataclasses import dataclass

#: The hardware reads one byte per clock, so this is the whole alphabet.
BYTE_MAX = 255


class ParseError(Exception):
    """A regex the compiler cannot accept, with the offending source position.

    Every failure path in the tokeniser raises this, and the parser will too, so
    a caller only ever has one exception type to catch.
    """

    def __init__(self, message: str, pattern: str, position: int) -> None:
        super().__init__(message)
        self.message = message
        self.pattern = pattern
        self.position = position

    def __str__(self) -> str:
        """Render the message, the pattern, and a caret under the bad position."""
        return (
            f"{self.message} at position {self.position}\n"
            f"  {self.pattern}\n"
            f"  {' ' * self.position}^"
        )


class TokenKind(enum.Enum):
    """The lexical categories of the v1 regex language.

    These are not invented: they are the terminals of the grammar, which is
    `regex := alternation EOF`, `alternation := concatenation ('|' concatenation)*`,
    `concatenation := repetition+`, `repetition := atom '*'*`,
    `atom := CHAR | '(' alternation ')'`. Six terminals, six kinds.

    Concatenation has no kind because it has no syntax -- it is implicit in
    adjacency, and the parser recovers it with a first-set test.
    """

    CHAR = "CHAR"      # one literal byte; value is an int 0..255
    PIPE = "PIPE"      # `|`
    STAR = "STAR"      # `*`
    LPAREN = "LPAREN"  # `(`
    RPAREN = "RPAREN"  # `)`
    EOF = "EOF"        # exactly one, always last

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Token:
    """One lexical unit, tagged with where in the source it started.

    `value` is already decoded: a `CHAR` token carries the integer 97, not the
    string "a", and `\\n` arrives as the number 10. A token is a value, not a
    slice of the source.

    `position` is a 0-based index into the original pattern. It exists so that
    every error message downstream can point a caret at the right column; that
    is its only purpose, and it must survive into the parser's errors.
    """

    kind: TokenKind
    value: int | None
    position: int

    def __repr__(self) -> str:
        if self.kind is TokenKind.CHAR:
            return f"{self.kind.name}({_show_byte(self.value)})@{self.position}"
        return f"{self.kind.name}@{self.position}"


# --------------------------------------------------------------------- tables
# The main loop is these four buckets turned into an if/elif chain. Every input
# character lands in exactly one, and the last is the catch-all.

# Bucket 1: single characters that map straight to a kind and carry no value.
_SINGLES = {
    "|": TokenKind.PIPE,
    "*": TokenKind.STAR,
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
}

# Bucket 2 opens with a backslash. `\n` and friends name a control byte...
_CONTROL_ESCAPES = {"n": 10, "t": 9, "r": 13, "f": 12, "v": 11, "0": 0}

# ...and these strip a character's special meaning. Deliberately WIDER than the
# operator set: it covers every character in `_UNSUPPORTED` too, because `+` is
# rejected bare, so without `\+` the byte 0x2B would be unmatchable. The rule is
# that every byte the bare syntax refuses must still be writable escaped.
_META_ESCAPES = set("*+?()[]{}.|\\-^$")

# `\d \D \w \W \s \S`. Rejected with their own message, since "unknown escape"
# would wrongly suggest a typo.
_SHORTHAND_NAMES = frozenset("dws")

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# Bucket 3: real regex syntax that v1 does not implement. Naming each one buys a
# message that says what you tried to do, and offers a rewrite where one exists.
_UNSUPPORTED = {
    "+": "the '+' operator is not supported in v1 (write 'aa*' instead)",
    "?": "the '?' operator is not supported in v1",
    ".": "the wildcard '.' is not supported in v1",
    "[": "character classes '[...]' are not supported in v1",
    "{": "bounded repetition '{n,m}' is not supported in v1",
    "^": "the anchor '^' is not supported in v1",
    "$": "the anchor '$' is not supported in v1",
}


# ---------------------------------------------------------------- the scanner
def tokenize(pattern: str) -> list[Token]:
    """Scan a regex into a token list ending in exactly one `EOF` token."""
    tokens: list[Token] = []
    i = 0

    while i < len(pattern):
        start = i  # captured before anything moves: this is the token's position
        char = pattern[i]

        if char in _SINGLES:
            tokens.append(Token(_SINGLES[char], None, start))
            i += 1
        elif char == "\\":
            value, i = _scan_escape(pattern, start)
            tokens.append(Token(TokenKind.CHAR, value, start))
        elif char in _UNSUPPORTED:
            raise ParseError(_UNSUPPORTED[char], pattern, start)
        else:
            tokens.append(Token(TokenKind.CHAR, _byte(char, pattern, start), start))
            i += 1

    # One EOF, always, so the parser can peek without ever bounds-checking.
    tokens.append(Token(TokenKind.EOF, None, len(pattern)))
    return tokens


def _byte(char: str, pattern: str, position: int) -> int:
    """Convert one source character to a byte, rejecting anything above 255."""
    code = ord(char)
    if code > BYTE_MAX:
        raise ParseError(
            f"{char!r} is U+{code:04X}, outside the 0..255 byte alphabet",
            pattern,
            position,
        )
    return code


def _scan_escape(pattern: str, i: int) -> tuple[int, int]:
    """Scan the escape whose backslash sits at `i`. Returns `(byte, next_index)`.

    Sub-scanners return the new cursor rather than mutating shared state, so the
    caller cannot forget to advance -- the advance *is* the return value.
    """
    if i + 1 >= len(pattern):
        raise ParseError("trailing '\\' with nothing to escape", pattern, i)

    char = pattern[i + 1]
    if char in _CONTROL_ESCAPES:
        return _CONTROL_ESCAPES[char], i + 2
    if char in _META_ESCAPES:
        return _byte(char, pattern, i + 1), i + 2
    if char.lower() in _SHORTHAND_NAMES:
        raise ParseError(
            f"the shorthand class '\\{char}' is not supported in v1", pattern, i
        )
    if char == "x":
        return _scan_hex(pattern, i)

    raise ParseError(f"unknown escape sequence '\\{char}'", pattern, i)


def _scan_hex(pattern: str, i: int) -> tuple[int, int]:
    """Scan `\\xHH` whose backslash sits at `i`. Returns `(byte, next_index)`."""
    digits = pattern[i + 2 : i + 4]
    if len(digits) < 2 or any(d not in _HEX_DIGITS for d in digits):
        raise ParseError(
            r"'\x' needs exactly two hex digits, as in '\x1f'", pattern, i
        )
    return int(digits, 16), i + 4


# ------------------------------------------------------------ display and CLI
_NAMED_BYTES = {0: r"\0", 9: r"\t", 10: r"\n", 11: r"\v", 12: r"\f", 13: r"\r"}

# The mirror of `_META_ESCAPES`: every character that cannot appear bare in v1
# source, because it is an operator or opens a construct v1 rejects. Escaping
# exactly this set is what makes `to_pattern` output always parse back.
_MUST_ESCAPE = "|*()\\+?.[^${"


def render_byte(code: int) -> str:
    """Render one byte as it would be written in regex source.

    Used by the parser's `to_pattern`. Unlike `_show_byte`, this escapes
    metacharacters, so the result is valid source rather than a readable label.
    """
    if code in _NAMED_BYTES:
        return _NAMED_BYTES[code]
    if 32 <= code <= 126:
        char = chr(code)
        return "\\" + char if char in _MUST_ESCAPE else char
    return f"\\x{code:02x}"


def _show_byte(code: int) -> str:
    """Show a byte for human eyes: the character, or a conventional escape."""
    if 32 <= code <= 126:
        return repr(chr(code))
    escape = _NAMED_BYTES.get(code) or f"\\x{code:02x}"
    return f"'{escape}'"


def dump(pattern: str) -> str:
    """Render the token stream for `pattern` as an aligned table."""
    lines = [f"{'pos':>4}  {'kind':<7}  value", f"{'':->4}  {'':-<7}  {'':-<8}"]
    for token in tokenize(pattern):
        shown = _show_byte(token.value) if token.kind is TokenKind.CHAR else ""
        lines.append(f"{token.position:>4}  {token.kind.name:<7}  {shown}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """CLI: print the token table for one pattern. Returns a process exit code."""
    if len(argv) != 2:
        print("usage: python -m streamweave.tokenizer '<regex>'", file=sys.stderr)
        return 2
    try:
        print(dump(argv[1]))
    except ParseError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
