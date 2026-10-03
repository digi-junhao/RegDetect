"""Tests for Thompson construction.

The important ones cross-check the generated NFA against `ast_matches` below --
a completely independent matcher that walks the AST by slicing strings and never
touches a state, an edge or an epsilon closure. Two implementations that agree
on every string over a small alphabet is far stronger evidence than any number
of hand-checked edge listings.
"""

import itertools

import pytest

from RegDetect.parser import Alt, Char, Concat, Node, Star, parse, state_estimate
from RegDetect.thompson import NFA, build, compile_pattern


# ============================================================== the oracle
def ast_matches(node: Node, text: str) -> bool:
    """Whether `node` matches the whole of `text`, computed straight from the AST.

    Deliberately naive and deliberately not an automaton: it returns the set of
    leftover suffixes after matching a prefix, and the string matches when the
    empty suffix is among them.
    """
    return "" in _remainders(node, text)


def _remainders(node: Node, text: str) -> set[str]:
    if isinstance(node, Char):
        return {text[1:]} if text and ord(text[0]) == node.code else set()
    if isinstance(node, Concat):
        out: set[str] = set()
        for middle in _remainders(node.left, text):
            out |= _remainders(node.right, middle)
        return out
    if isinstance(node, Alt):
        return _remainders(node.left, text) | _remainders(node.right, text)
    if isinstance(node, Star):
        # Reflexive-transitive closure. The `seen` set is what stops a body that
        # can match the empty string (`(a*)*`) from looping forever.
        seen = {text}
        frontier = [text]
        while frontier:
            current = frontier.pop()
            for rest in _remainders(node.child, current):
                if rest not in seen:
                    seen.add(rest)
                    frontier.append(rest)
        return seen
    raise TypeError(f"not an AST node: {node!r}")


# ====================================================== the shape of a fragment
def test_char_is_two_states_and_one_edge():
    nfa = compile_pattern("a")
    assert nfa.state_count == 2
    assert len(nfa.symbol_edges) == 1
    assert nfa.epsilon_edges == ()
    edge = nfa.symbol_edges[0]
    assert (edge.source, edge.symbol, edge.target) == (nfa.start, 97, nfa.accept)


def test_concat_adds_no_states_only_a_wire():
    one = compile_pattern("a")
    two = compile_pattern("ab")
    assert two.state_count == one.state_count * 2      # 4, not 5
    assert len(two.symbol_edges) == 2
    assert len(two.epsilon_edges) == 1                 # the single join


def test_alt_adds_two_states_and_four_epsilons():
    nfa = compile_pattern("a|b")
    assert nfa.state_count == 6                        # 2 + 2 + 2
    assert len(nfa.epsilon_edges) == 4                 # split x2, rejoin x2


def test_star_adds_two_states_and_four_epsilons():
    nfa = compile_pattern("a*")
    assert nfa.state_count == 4                        # 2 + 2
    assert len(nfa.epsilon_edges) == 4                 # enter, skip, loop, leave


def test_start_and_accept_are_distinct_and_in_range():
    for pattern in ("a", "ab", "a|b", "a*", "(a|b)*abb"):
        nfa = compile_pattern(pattern)
        assert nfa.start != nfa.accept
        assert 0 <= nfa.start < nfa.state_count
        assert 0 <= nfa.accept < nfa.state_count


def test_every_edge_endpoint_is_a_real_state():
    nfa = compile_pattern("((a|b)*c)*ab")
    for edge in nfa.symbol_edges:
        assert 0 <= edge.source < nfa.state_count
        assert 0 <= edge.target < nfa.state_count
        assert 0 <= edge.symbol <= 255
    for edge in nfa.epsilon_edges:
        assert 0 <= edge.source < nfa.state_count
        assert 0 <= edge.target < nfa.state_count


def test_one_symbol_edge_per_char_node():
    # Symbol edges come only from Char, so the count is the leaf count.
    assert len(compile_pattern("(a|b)*abb").symbol_edges) == 5


# ================================================== the memoisation trap
def test_identical_subtrees_get_separate_states():
    """The trap from the module docstring, pinned.

    `Concat(Char(97), Char(97))` holds two subtrees that are *equal and hash
    identically*, because AST nodes are frozen dataclasses and therefore values.
    A builder that memoised on node value would emit one 2-state fragment
    instead of two, and `aa` would wrongly become `a`.
    """
    tree = parse("aa")
    assert tree.left == tree.right          # equal as values...
    assert hash(tree.left) == hash(tree.right)

    nfa = build(tree)
    assert nfa.state_count == 4             # ...but four distinct states
    assert len(nfa.symbol_edges) == 2

    sources = {edge.source for edge in nfa.symbol_edges}
    targets = {edge.target for edge in nfa.symbol_edges}
    assert len(sources) == 2, "the two 'a' transitions were merged"
    assert len(targets) == 2

    assert nfa.matches("aa")
    assert not nfa.matches("a")             # the give-away if they merged


def test_repeated_group_does_not_collapse():
    nfa = compile_pattern("(ab)(ab)")
    assert nfa.state_count == 8
    assert nfa.matches("abab")
    assert not nfa.matches("ab")


# ============================================ agreement with the parser's guess
@pytest.mark.parametrize(
    "pattern",
    ["a", "ab", "abc", "a|b", "a*", "a**", "(ab)*", "(a|b)*", "(a|b)*abb",
     "((a|b)*c)*", "a(b|c)*d", "(a*b|c)*", "a(bc)", "a|(b|c)"],
)
def test_matches_the_parser_estimate(pattern):
    """`state_estimate` predicted the flip-flop count before this stage existed.

    Two independent formulas -- 2 x non-Concat nodes, versus what the builder
    actually allocated -- must agree, or one of them is wrong.
    """
    tree = parse(pattern)
    assert build(tree).state_count == state_estimate(tree)


def test_the_demo_pattern_is_fourteen_states():
    # The number the parser doc has been quoting all along.
    assert compile_pattern("(a|b)*abb").state_count == 14


# ============================================================ epsilon closures
def test_closure_includes_the_state_itself():
    nfa = compile_pattern("a")
    assert nfa.start in nfa.epsilon_closure({nfa.start})


def test_star_can_reach_accept_without_consuming():
    # `a*` matches the empty string, so accept is in the start's closure.
    nfa = compile_pattern("a*")
    assert nfa.accept in nfa.epsilon_closure({nfa.start})


def test_char_cannot_reach_accept_without_consuming():
    nfa = compile_pattern("a")
    assert nfa.accept not in nfa.epsilon_closure({nfa.start})


def test_closure_terminates_on_an_epsilon_cycle():
    # `(a*)*` has epsilon loops by construction. A naive recursion hangs here;
    # the worklist's `seen` set is what makes it safe.
    nfa = compile_pattern("(a*)*")
    closure = nfa.epsilon_closure({nfa.start})
    assert nfa.accept in closure
    assert nfa.matches("")
    assert nfa.matches("aaa")


# ================================================================== matching
@pytest.mark.parametrize(
    "pattern,accepts,rejects",
    [
        ("a", ["a"], ["", "b", "aa"]),
        ("ab", ["ab"], ["", "a", "b", "abc"]),
        ("a|b", ["a", "b"], ["", "ab", "c"]),
        ("a*", ["", "a", "aa", "aaaa"], ["b", "ab"]),
        ("a**", ["", "a", "aaa"], ["b"]),
        ("(a|b)*", ["", "a", "b", "abab"], ["c", "abc"]),
        ("(a|b)*abb", ["abb", "aabb", "babb", "ababb"], ["", "ab", "abba"]),
        ("a*b*", ["", "a", "b", "aabb"], ["ba", "aba"]),
        ("(ab)*", ["", "ab", "abab"], ["a", "aba", "ba"]),
        ("a(bc)*d", ["ad", "abcd", "abcbcd"], ["abc", "abd", "abcd d"]),
    ],
)
def test_matching(pattern, accepts, rejects):
    nfa = compile_pattern(pattern)
    for text in accepts:
        assert nfa.matches(text), f"{pattern!r} should accept {text!r}"
    for text in rejects:
        assert not nfa.matches(text), f"{pattern!r} should reject {text!r}"


def test_matches_accepts_bytes_as_well_as_str():
    nfa = compile_pattern("ab")
    assert nfa.matches(b"ab")
    assert nfa.matches("ab")


def test_high_bytes_round_trip_through_the_machine():
    nfa = compile_pattern(r"\xff")
    assert nfa.matches(b"\xff")
    assert not nfa.matches(b"\x00")


def test_codepoint_above_255_is_rejected_at_the_input():
    nfa = compile_pattern("a")
    with pytest.raises(ValueError, match="outside the 0..255"):
        nfa.matches("€")


# ================================================ the cross-check that matters
CROSS_CHECK_PATTERNS = [
    "a", "b", "ab", "ba", "aab", "a|b", "a|ab", "ab|ba",
    "a*", "b*", "a**", "(a*)*", "a*b", "ab*", "a*b*", "(ab)*", "(ba)*",
    "(a|b)*", "(a|b)*a", "(a|b)*abb", "a(a|b)*b", "((a|b)*a)*b",
    "(a|ab)*", "(aa)*", "(aa|b)*", "a(ba)*", "((ab)*|b)*", "(a*b)*",
]


@pytest.mark.parametrize("pattern", CROSS_CHECK_PATTERNS)
def test_nfa_agrees_with_the_ast_oracle(pattern):
    """Exhaustive agreement over every string in {a,b}* up to length 6.

    The NFA walks states and epsilon closures; the oracle slices strings and
    knows nothing about automata. Where they disagree, the construction is wrong.
    """
    tree = parse(pattern)
    nfa = build(tree)

    for length in range(7):
        for combo in itertools.product("ab", repeat=length):
            text = "".join(combo)
            assert nfa.matches(text) == ast_matches(tree, text), (
                f"{pattern!r} disagreed on {text!r}: "
                f"nfa={nfa.matches(text)} oracle={ast_matches(tree, text)}"
            )


# ==================================================================== hygiene
def test_construction_is_deterministic():
    # Frozen dataclass of tuples, so two builds of the same tree are equal.
    tree = parse("(a|b)*abb")
    assert build(tree) == build(tree)


def test_nfa_is_hashable_and_frozen():
    nfa = compile_pattern("a|b")
    assert isinstance(nfa, NFA)
    hash(nfa)
    with pytest.raises(Exception):
        nfa.start = 99


def test_edge_table_covers_every_edge():
    nfa = compile_pattern("(a|b)*abb")
    rows = nfa.edge_table()
    assert len(rows) == len(nfa.symbol_edges) + len(nfa.epsilon_edges)
    assert sum(1 for _, label, _ in rows if label == "eps") == len(nfa.epsilon_edges)


def test_pretty_reports_the_header_numbers():
    rendered = compile_pattern("a|b").pretty()
    assert "states: 6" in rendered
    assert "symbol edges: 2" in rendered
    assert "epsilon edges: 4" in rendered
