"""Tests for the RegDetect regex parser.

Comparisons are exact AST equality -- frozen dataclasses give a structural
`__eq__` -- never repr strings, so changing a `__repr__` cannot break the suite.
"""

import itertools

import pytest

from RegDetect.parser import (
    Alt,
    Char,
    Concat,
    Star,
    parse,
    pretty,
    size,
    state_estimate,
    to_pattern,
)
from RegDetect.tokenizer import ParseError


def ch(char: str) -> Char:
    """`Char` for a one-character string, so tests read like the pattern."""
    return Char(ord(char))


A, B, C, D = ch("a"), ch("b"), ch("c"), ch("d")


# ----------------------------------------------------------- single constructs
@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("a", A),
        ("ab", Concat(A, B)),
        ("a|b", Alt(A, B)),
        ("a*", Star(A)),
        ("(a)", A),                      # grouping leaves no node
        ("((a))", A),
        ("(ab)", Concat(A, B)),
        ("(a|b)", Alt(A, B)),
    ],
)
def test_single_constructs(pattern, expected):
    assert parse(pattern) == expected


def test_grouping_produces_no_node():
    # `(a)` and `a` are the same tree, not merely equivalent ones.
    assert parse("(a)") == parse("a")
    assert size(parse("(((a)))")) == 1


# --------------------------------------------------------------- precedence
@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("ab|cd", Alt(Concat(A, B), Concat(C, D))),
        ("ab*", Concat(A, Star(B))),
        ("(ab)*", Star(Concat(A, B))),
        ("a|b*", Alt(A, Star(B))),
        ("a*b", Concat(Star(A), B)),
        ("a|bc", Alt(A, Concat(B, C))),
        ("(a|b)c", Concat(Alt(A, B), C)),
    ],
)
def test_precedence(pattern, expected):
    assert parse(pattern) == expected


def test_star_binds_tighter_than_concatenation():
    # `ab*` is a followed by (b starred), NOT (ab) starred.
    assert parse("ab*") == Concat(A, Star(B))
    assert parse("ab*") != Star(Concat(A, B))


def test_concatenation_binds_tighter_than_alternation():
    assert parse("ab|cd") == Alt(Concat(A, B), Concat(C, D))
    assert parse("ab|cd") != Concat(A, Alt(B, Concat(C, D)))


# ------------------------------------------------------------ associativity
def test_concatenation_folds_left():
    assert parse("abc") == Concat(Concat(A, B), C)


def test_alternation_folds_left():
    assert parse("a|b|c") == Alt(Alt(A, B), C)


def test_long_concatenation_folds_left():
    assert parse("abcd") == Concat(Concat(Concat(A, B), C), D)


# ------------------------------------------------------------ chained postfix
@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("a**", Star(Star(A))),
        ("a***", Star(Star(Star(A)))),
        ("(a|b)**", Star(Star(Alt(A, B)))),
    ],
)
def test_chained_star(pattern, expected):
    assert parse(pattern) == expected


# ------------------------------------------------------------------- nesting
def test_nested_groups():
    assert parse("((a|b)*c)*") == Star(Concat(Star(Alt(A, B)), C))


def test_nested_groups_with_trailing_concat():
    assert parse("(a(b|c))*d") == Concat(Star(Concat(A, Alt(B, C))), D)


def test_the_canonical_thompson_pattern():
    assert parse("(a|b)*abb") == Concat(
        Concat(Concat(Star(Alt(A, B)), A), B), B
    )


# ------------------------------------------------------------------- escapes
def test_escaped_star_is_a_literal():
    assert parse(r"a\*b") == Concat(Concat(A, Char(42)), B)


def test_escaped_pipe_is_a_literal():
    assert parse(r"a\|b") == Concat(Concat(A, Char(124)), B)


def test_control_escape():
    assert parse(r"a\nb") == Concat(Concat(A, Char(10)), B)


def test_hex_escape():
    assert parse(r"\x41") == Char(65)


# ---------------------------------------------------------------------- size
@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("a", 1),
        ("ab", 3),            # Concat + 2 Char
        ("a|b", 3),
        ("a*", 2),
        ("(a|b)*abb", 10),
        ("(a)", 1),           # grouping is free
    ],
)
def test_size(pattern, expected):
    assert size(parse(pattern)) == expected


# ---------------------------------------------------------------- round trip
ROUND_TRIP = [
    "a", "ab", "abc", "a|b", "a|b|c", "a*", "a**", "ab*", "a*b",
    "(ab)*", "(a|b)*", "(a|b)c", "ab|cd", "a|bc", "(a|b)*abb",
    "((a|b)*c)*", "(a(b|c))*d", r"a\*b", r"a\|b", r"a\nb", r"\x41",
    "x", "xy|z", "(x)*", "a|b*", "(a*b|c)*",
    # Right-nested trees: brackets are load-bearing here, not decoration.
    "a(bc)", "a|(b|c)", "a(b(cd))", "(ab)(cd)", "a|(b|(c|d))",
]


def test_right_nested_concat_keeps_its_brackets():
    # Brackets are the only reason this tree is right-nested; dropping them
    # would reshape it to Concat(Concat(a,b),c) on the way back in.
    tree = parse("a(bc)")
    assert tree == Concat(A, Concat(B, C))
    assert to_pattern(tree) == "a(bc)"
    assert parse(to_pattern(tree)) == tree


def test_right_nested_alt_keeps_its_brackets():
    tree = parse("a|(b|c)")
    assert tree == Alt(A, Alt(B, C))
    assert to_pattern(tree) == "a|(b|c)"
    assert parse(to_pattern(tree)) == tree


def test_left_nested_trees_need_no_brackets():
    # The mirror case: these are what bare source already produces.
    assert to_pattern(parse("abc")) == "abc"
    assert to_pattern(parse("a|b|c")) == "a|b|c"


@pytest.mark.parametrize("pattern", ROUND_TRIP)
def test_round_trip(pattern):
    tree = parse(pattern)
    assert parse(to_pattern(tree)) == tree


@pytest.mark.parametrize("pattern", ROUND_TRIP)
def test_unparsed_output_is_parseable(pattern):
    # A weaker but sharper check: whatever to_pattern emits must at least lex
    # and parse, which is what the escaping in render_byte guarantees.
    parse(to_pattern(parse(pattern)))


# -------------------------------------------------------------------- pretty
def test_pretty_renders_a_tree():
    assert pretty(parse("ab|c*")).splitlines() == [
        "Alt",
        "├── Concat",
        "│   ├── Char 'a'",
        "│   └── Char 'b'",
        "└── Star",
        "    └── Char 'c'",
    ]


def test_pretty_of_a_leaf():
    assert pretty(parse("a")) == "Char 'a'"


# -------------------------------------------------------------------- errors
@pytest.mark.parametrize(
    "pattern,position,fragment",
    [
        ("", 0, "empty pattern"),
        ("()", 0, "empty group"),
        ("ab()", 2, "empty group"),
        ("*a", 0, "'*' has no operand to repeat"),
        ("*", 0, "'*' has no operand to repeat"),
        ("|a", 0, "'|' has no left-hand alternative"),
        ("(|a)", 1, "'|' has no left-hand alternative"),
        ("a|", 2, "after '|'"),
        ("a||b", 2, "after '|'"),
        ("(a|)", 3, "after '|'"),
        ("a)", 1, "unmatched ')'"),
        (")", 0, "unmatched ')'"),
        ("(a))", 3, "unmatched ')'"),
        # The caret points at the bracket you forgot, not at where we noticed.
        ("(a", 0, "unclosed '('"),
        ("(", 0, "unclosed '('"),
        ("a(bc", 1, "unclosed '('"),
        ("((((((a", 5, "unclosed '('"),
    ],
)
def test_structural_errors(pattern, position, fragment):
    with pytest.raises(ParseError) as exc:
        parse(pattern)
    assert exc.value.position == position, str(exc.value)
    assert fragment in exc.value.message, str(exc.value)


@pytest.mark.parametrize(
    "pattern,position,fragment",
    [
        ("a+", 1, "'+' operator"),
        ("a?", 1, "'?' operator"),
        ("[a-z]", 0, "character classes"),
        ("a\\", 1, "trailing"),
        (r"\d", 0, "shorthand class"),
    ],
)
def test_lexical_errors_still_surface_through_parse(pattern, position, fragment):
    # The parser does not swallow or reword what the tokeniser rejected.
    with pytest.raises(ParseError) as exc:
        parse(pattern)
    assert exc.value.position == position, str(exc.value)
    assert fragment in exc.value.message


def test_error_renders_a_caret():
    with pytest.raises(ParseError) as exc:
        parse("ab()")
    assert str(exc.value).splitlines() == [
        "empty group at position 2",
        "  ab()",
        "    ^",
    ]


def test_unclosed_bracket_caret_points_at_the_opening_bracket():
    with pytest.raises(ParseError) as exc:
        parse("ab(cd")
    assert str(exc.value).splitlines() == [
        "unclosed '(' at position 2",
        "  ab(cd",
        "    ^",
    ]


# ------------------------------------------------- the seam with the tokeniser
def test_parser_does_not_revalidate_bytes():
    """Byte-range validation belongs to the tokeniser, and only there.

    Fails if someone later adds a range check to the parser. Duplicated
    validation is worse than none: it drifts, and then two parts of the
    compiler disagree about what is legal.
    """
    # Every byte the tokeniser accepts must reach the AST untouched.
    for code in (0, 1, 65, 127, 128, 254, 255):
        assert parse(f"\\x{code:02x}") == Char(code)


def test_parser_trusts_the_eof_sentinel():
    # Every pattern's token stream ends in exactly one EOF, which is why the
    # parser's peek() is a bare list index with no bounds check anywhere.
    from RegDetect.parser import Parser
    from RegDetect.tokenizer import TokenKind

    parser = Parser("(a|b)*")
    assert parser.tokens[-1].kind is TokenKind.EOF
    assert [t.kind for t in parser.tokens].count(TokenKind.EOF) == 1


# -------------------------------------------------------- the hardware number
def test_demo_pattern_state_estimate():
    """Pins the flip-flop count so stage 2 disagreeing is immediately visible.

    `(a|b)*abb` is 10 nodes, 3 of them Concat. Concat is free -- it is pure
    wiring in Thompson's construction -- so 7 x 2 = 14 flip-flops.
    """
    tree = parse("(a|b)*abb")
    assert size(tree) == 10
    assert state_estimate(tree) == 14


@pytest.mark.parametrize(
    "pattern,nodes,states",
    [
        ("a", 1, 2),
        ("ab", 3, 4),        # the Concat costs nothing
        ("abc", 5, 6),       # nor do two of them
        ("a|b", 3, 6),       # Alt costs 2 of its own
        ("a*", 2, 4),
        ("(a|b)*abb", 10, 14),
    ],
)
def test_state_estimate_charges_nothing_for_concat(pattern, nodes, states):
    tree = parse(pattern)
    assert size(tree) == nodes
    assert state_estimate(tree) == states


# ------------------------------------------------------------ exhaustive sweep
def test_every_short_pattern_either_parses_or_raises_parse_error():
    """The contract, checked exhaustively rather than by example.

    For ANY input the tokeniser accepts, `parse` must either return an AST that
    round-trips exactly, or raise `ParseError`. Never `TypeError`, never
    `AttributeError`, never `None`. This sweep is what caught `to_pattern`
    silently reshaping right-nested trees, which 100+ hand-written cases missed.
    """
    alphabet = "ab|*()"
    failures = []

    for length in range(7):
        for combo in itertools.product(alphabet, repeat=length):
            pattern = "".join(combo)
            try:
                tree = parse(pattern)
            except ParseError:
                continue
            except Exception as exc:  # noqa: BLE001 - the point of the sweep
                failures.append(f"{pattern!r}: {type(exc).__name__}: {exc}")
                continue

            unparsed = to_pattern(tree)
            if parse(unparsed) != tree:
                failures.append(f"{pattern!r}: round trip gave {unparsed!r}")

    assert not failures, "\n".join(failures[:15])


def test_deep_nesting_raises_parse_error_not_recursion_error():
    with pytest.raises(ParseError) as exc:
        parse("(" * 5000 + "a" + ")" * 5000)
    assert "deeply" in exc.value.message
