"""Recursive-descent parser: token stream in, abstract syntax tree out.

Stage 1b. The tokeniser answered "what are the pieces?"; this module answers
"how do they fit together?" -- which needs the whole shape of the input, and so
needs recursion.

The grammar, used verbatim. Precedence and associativity are encoded in the
*shape* of the rules, which is what makes plain recursive descent work with no
precedence table anywhere in the code:

    regex          :=  alternation EOF
    alternation    :=  concatenation ( '|' concatenation )*
    concatenation  :=  repetition+
    repetition     :=  atom '*'*
    atom           :=  CHAR | '(' alternation ')'

Five rules, five functions, same names, same order. Precedence runs loosest to
tightest down the call chain: `|` < concatenation < `*`. Whoever is called last
reads the tokens first and so claims its operand while the least is on the
table, which is what makes it bind tightest.

Grouping produces no node. `(a)` parses to `Char(97)`, identical to `a` --
brackets redirect the parser back to the top of the ladder and are then fully
recorded in the shape of the tree.

Trap to remember in stage 2 (Thompson construction). Every node is a frozen
dataclass, which makes nodes *values*: they are hashable and compare
structurally. The two `Char(97)` subtrees inside `Concat(Char(97), Char(97))`
are equal to each other and hash identically. Thompson construction must
allocate a *fresh* NFA state on every visit and must never memoise on node
value, or those two independent transitions will silently collapse into one
state and the generated machine will be wrong. AST nodes are values, not
identities.

Usage:

    python -m streamweave.parser '(a|b)*abb'
"""

import sys
from dataclasses import dataclass

from .tokenizer import BYTE_MAX, ParseError, Token, TokenKind, render_byte, tokenize


# ==============================================================================
# THE AST
#
# Pure data, no parsing logic. Stage 2 imports the four node classes and the
# three walkers below, and can ignore the Parser class entirely.
# ==============================================================================
@dataclass(frozen=True)
class Node:
    """Base class for every AST node. Never instantiated directly."""


@dataclass(frozen=True, repr=False)
class Char(Node):
    """A single literal byte -- the only leaf in the v1 language."""

    code: int

    def __post_init__(self) -> None:
        if not 0 <= self.code <= BYTE_MAX:
            raise ValueError(f"byte {self.code} falls outside the 0..255 alphabet")

    def __repr__(self) -> str:
        if 32 <= self.code <= 126:
            return f"Char({chr(self.code)!r})"
        return f"Char(0x{self.code:02x})"


@dataclass(frozen=True, repr=False)
class Concat(Node):
    """`ab` -- match `left`, then `right`. Binary, folded left-associatively."""

    left: Node
    right: Node

    def __repr__(self) -> str:
        return f"Concat({self.left!r}, {self.right!r})"


@dataclass(frozen=True, repr=False)
class Alt(Node):
    """`a|b` -- match `left` or `right`. Binary, folded left-associatively."""

    left: Node
    right: Node

    def __repr__(self) -> str:
        return f"Alt({self.left!r}, {self.right!r})"


@dataclass(frozen=True, repr=False)
class Star(Node):
    """`a*` -- match `child` zero or more times."""

    child: Node

    def __repr__(self) -> str:
        return f"Star({self.child!r})"


# ==============================================================================
# THE PARSER
# ==============================================================================
#: Token kinds that can begin an `atom` -- the grammar's *first set* for that
#: rule. Concatenation has no operator token, so `parse_concatenation` cannot
#: stop by spotting a symbol; it stops when the next token is not in this set.
#: The other four kinds (PIPE, RPAREN, STAR, EOF) all end a concatenation, and
#: all four are handled by that one condition.
ATOM_START = frozenset({TokenKind.CHAR, TokenKind.LPAREN})

#: Why each token kind cannot begin an atom, phrased as the user's mistake
#: rather than as the parser's disappointment.
_NO_OPERAND = {
    TokenKind.STAR: "'*' has no operand to repeat",
    TokenKind.PIPE: "'|' has no left-hand alternative",
    TokenKind.RPAREN: "unmatched ')'",
    TokenKind.EOF: "expected a character or '('",
}


class Parser:
    """A recursive-descent parser over a token list, with one method per rule."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern            # kept ONLY for error messages
        self.tokens = tokenize(pattern)   # the one seam with stage 1a
        self.pos = 0

    # -- cursor helpers. `peek` never bounds-checks, because the tokeniser
    # -- guarantees exactly one EOF token at the end of every stream.
    def peek(self) -> Token:
        """The token under the cursor."""
        return self.tokens[self.pos]

    def at(self, kind: TokenKind) -> bool:
        """Whether the token under the cursor has this kind."""
        return self.peek().kind is kind

    def advance(self) -> Token:
        """Consume and return the token under the cursor."""
        token = self.peek()
        self.pos += 1
        return token

    # -- one method per grammar rule, named to match
    def parse_alternation(self) -> Node:
        """`alternation := concatenation ( '|' concatenation )*` -- loosest.

        Loops rather than recursing on itself, which is what makes `|`
        left-associative: `a|b|c` becomes `Alt(Alt(a, b), c)`.
        """
        node = self.parse_concatenation()
        while self.at(TokenKind.PIPE):
            self.advance()
            node = Alt(node, self.parse_concatenation())
        return node

    def parse_concatenation(self) -> Node:
        """`concatenation := repetition+`

        The first-set test. Concatenation is the only operator with no token to
        look for -- `ab` is just two operands written next to each other -- so
        the loop stops by asking whether another operand *could* begin here.

        No emptiness check is needed: `parse_atom` already raises when the
        current token cannot start an atom, which is what turns `"|a"`, `"a|"`,
        `"()"` and `"*a"` into errors.
        """
        node = self.parse_repetition()
        while self.peek().kind in ATOM_START:
            node = Concat(node, self.parse_repetition())
        return node

    def parse_repetition(self) -> Node:
        """`repetition := atom '*'*` -- tightest.

        Postfix operators are consumed by a loop *after* the operand, never by
        recursing back into this rule. That is what lets `a**` parse, as
        `Star(Star(Char('a')))`. It is deliberately not collapsed to `Star(a)`:
        representing exactly what was written is this stage's whole contract,
        and simplification belongs to a later pass.
        """
        node = self.parse_atom()
        while self.at(TokenKind.STAR):
            self.advance()
            node = Star(node)
        return node

    def parse_atom(self) -> Node:
        """`atom := CHAR | '(' alternation ')'`

        The only rule that *demands* a token, and therefore the only one that
        can fail. Every other rule tolerates absence -- "zero or more `|`
        clauses" is satisfied by zero of them.
        """
        token = self.peek()

        if token.kind is TokenKind.CHAR:
            self.advance()
            return Char(token.value)

        if token.kind is TokenKind.LPAREN:
            opening = self.advance()
            if self.at(TokenKind.RPAREN):
                raise ParseError("empty group", self.pattern, opening.position)
            if self.at(TokenKind.EOF):
                # Checked here as well as after the group, so that a bare `(`
                # reports the same thing `(a` does instead of failing inside.
                raise self._unclosed(opening)

            node = self.parse_alternation()  # back to the top of the ladder

            if not self.at(TokenKind.RPAREN):
                raise self._unclosed(opening)
            self.advance()
            return node  # the brackets themselves produce no node

        raise self._no_operand(token)

    def _unclosed(self, opening: Token) -> ParseError:
        """Blame the bracket that was never closed, not where we noticed.

        For `((((((a` that is the innermost bracket, which is the one you
        actually need to look at. The position is not written into the message
        because `ParseError.__str__` already appends it and draws the caret.
        """
        return ParseError("unclosed '('", self.pattern, opening.position)

    def _no_operand(self, token: Token) -> ParseError:
        """Explain why the token under the cursor cannot begin an atom."""
        previous = self.tokens[self.pos - 1] if self.pos else None
        if previous is not None and previous.kind is TokenKind.PIPE:
            # `a|`, `a||b`, `(a|)` -- the branch after the bar is missing.
            return ParseError(
                "expected a character or '(' after '|'",
                self.pattern,
                token.position,
            )
        return ParseError(_NO_OPERAND[token.kind], self.pattern, token.position)


def parse(pattern: str) -> Node:
    """Parse a regex into an AST. The entire public API of stage 1.

    Stage 2 imports only this function and the four node classes.
    """
    parser = Parser(pattern)

    # The empty regex is a *grammar* fact -- `concatenation := repetition+`
    # needs at least one operand -- so it is caught here, not in the tokeniser,
    # where `tokenize("") == [EOF]` is the correct and complete answer.
    if parser.at(TokenKind.EOF):
        raise ParseError("empty pattern", pattern, 0)

    try:
        node = parser.parse_alternation()
    except RecursionError:
        # The ladder recurses once per bracket. Re-raised as ParseError so
        # callers only ever catch one type; raising Python's recursion limit
        # instead would just move the crash somewhere less explicable.
        raise ParseError("pattern nests too deeply to parse", pattern, 0) from None

    if not parser.at(TokenKind.EOF):
        # Nothing in the ladder is offended by a stray `)`: every rule simply
        # stops and returns happily. It is this final check that notices.
        left_over = parser.peek()
        message = (
            "unmatched ')'"
            if left_over.kind is TokenKind.RPAREN
            else f"unexpected {_describe(left_over)} after a complete pattern"
        )
        raise ParseError(message, pattern, left_over.position)

    return node


def _describe(token: Token) -> str:
    """Name a token as it reads after \"unexpected\", showing the actual byte."""
    if token.kind is TokenKind.CHAR:
        return f"character {render_byte(token.value)!r}"
    return {
        TokenKind.PIPE: "'|'",
        TokenKind.STAR: "'*'",
        TokenKind.LPAREN: "'('",
        TokenKind.RPAREN: "')'",
        TokenKind.EOF: "end of pattern",
    }[token.kind]


# ==============================================================================
# WALKERS
# ==============================================================================
def size(node: Node) -> int:
    """Count nodes. The raw box count, not the hardware cost."""
    if isinstance(node, Char):
        return 1
    if isinstance(node, Star):
        return 1 + size(node.child)
    if isinstance(node, (Concat, Alt)):
        return 1 + size(node.left) + size(node.right)
    raise TypeError(f"not an AST node: {node!r}")


def state_estimate(node: Node) -> int:
    """Flip-flops the one-hot backend will need: 2 per non-`Concat` node.

    `Concat` is free. Concatenation in Thompson's construction is pure wiring --
    connect the left fragment's exit to the right fragment's entrance -- so no
    new state represents "and then". However deeply the `Concat` nodes stack up,
    the flip-flop count does not move.

    Available now, before a line of SystemVerilog exists. When stage 2 lands and
    reports its own number, the two must agree.
    """
    if isinstance(node, Char):
        return 2
    if isinstance(node, Star):
        return 2 + state_estimate(node.child)
    if isinstance(node, Alt):
        return 2 + state_estimate(node.left) + state_estimate(node.right)
    if isinstance(node, Concat):
        return state_estimate(node.left) + state_estimate(node.right)
    raise TypeError(f"not an AST node: {node!r}")


def to_pattern(node: Node) -> str:
    """Unparse an AST back to regex source, bracketing only where needed.

    Round-trips: `parse(to_pattern(parse(p))) == parse(p)` for every valid `p`.
    That property is the proof that dropping the group nodes lost nothing.
    """
    if isinstance(node, Char):
        return render_byte(node.code)
    if isinstance(node, Alt):
        # `|` is the loosest operator, so the left side never needs brackets.
        # The RIGHT side does when it is itself an Alt: both operators fold
        # left-associatively, so bare `a|b|c` comes back as Alt(Alt(a,b),c) and
        # printing Alt(a,Alt(b,c)) bare would silently reshape it.
        return f"{to_pattern(node.left)}|{_wrap(node.right, Alt)}"
    if isinstance(node, Concat):
        return _wrap(node.left, Alt) + _wrap(node.right, Alt, Concat)
    if isinstance(node, Star):
        # `a**` is fine, so a Star child needs no brackets; Alt and Concat bind
        # looser than `*` and do.
        return _wrap(node.child, Alt, Concat) + "*"
    raise TypeError(f"not an AST node: {node!r}")


def _wrap(node: Node, *needs_brackets: type) -> str:
    """Unparse `node`, bracketing it if it is one of the given node types."""
    text = to_pattern(node)
    return f"({text})" if isinstance(node, needs_brackets) else text


def pretty(node: Node) -> str:
    """Render an AST as an indented tree, for reading and for the CLI."""
    lines: list[str] = []

    def walk(current: Node, prefix: str, is_last: bool, is_root: bool) -> None:
        if is_root:
            lines.append(_label(current))
            child_prefix = ""
        else:
            lines.append(f"{prefix}{'└── ' if is_last else '├── '}{_label(current)}")
            child_prefix = prefix + ("    " if is_last else "│   ")
        kids = _children(current)
        for index, kid in enumerate(kids):
            walk(kid, child_prefix, index == len(kids) - 1, False)

    walk(node, "", True, True)
    return "\n".join(lines)


def _label(node: Node) -> str:
    if isinstance(node, Char):
        shown = (
            repr(chr(node.code)) if 32 <= node.code <= 126 else render_byte(node.code)
        )
        return f"Char {shown}"
    return type(node).__name__


def _children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, (Concat, Alt)):
        return (node.left, node.right)
    if isinstance(node, Star):
        return (node.child,)
    return ()


def main(argv: list[str]) -> int:
    """CLI: print the tree, the node count and the flip-flop estimate."""
    if len(argv) != 2:
        print("usage: python -m streamweave.parser '<regex>'", file=sys.stderr)
        return 2
    try:
        node = parse(argv[1])
    except ParseError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(pretty(node))
    print(f"nodes: {size(node)}")
    print(f"estimated NFA states: {state_estimate(node)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
