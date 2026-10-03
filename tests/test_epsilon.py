"""Tests for epsilon elimination.

The load-bearing test is `test_three_implementations_agree`: the AST oracle, the
epsilon-NFA and the epsilon-free NFA must decide every string identically. The
oracle is reused from `test_thompson` rather than copied -- two copies of an
oracle can drift, and then neither is trustworthy.
"""

import itertools

import pytest
from test_thompson import ast_matches

from RegDetect.epsilon import EpsilonFreeNFA, compile_pattern, eliminate
from RegDetect.parser import parse
from RegDetect.thompson import SymbolEdge, build


# ================================================== what the rewrite preserves
@pytest.mark.parametrize(
    "pattern",
    ["a", "ab", "a|b", "a*", "a**", "(a*)*", "(ab)*", "(a|b)*abb",
     "((a|b)*c)*", "a(b|c)*d"],
)
def test_state_count_and_numbering_are_preserved(pattern):
    """A rewrite, not a subset construction: same states, same numbers."""
    original = build(parse(pattern))
    flattened = eliminate(original)
    assert flattened.state_count == original.state_count
    assert flattened.start == original.start


def test_no_epsilon_edges_survive():
    # Structural by construction -- EpsilonFreeNFA has nowhere to put one --
    # but worth pinning, because it is the entire reason the stage exists.
    machine = compile_pattern("(a*)*")
    assert not hasattr(machine, "epsilon_edges")
    assert all(0 <= edge.symbol <= 255 for edge in machine.symbol_edges)


def test_every_edge_consumes_exactly_one_byte():
    """The hardware property: every edge is one clock cycle.

    `(a*)*` is the pathological case -- it has epsilon *cycles* in stage 2, which
    would be zero-delay combinational feedback if wired directly. After
    elimination the loop survives only as a registered self-loop.
    """
    machine = compile_pattern("(a*)*")
    self_loops = [e for e in machine.symbol_edges if e.source == e.target]
    assert self_loops, "the star loop should survive as a registered self-loop"


# ============================================================ worked by hand
def test_star_worked_by_hand():
    """`a*` is small enough to check every closure on paper.

    Stage 2 gives states 0..3, start 0, accept 1, with
    `0->1`, `0->2`, `2 --a--> 3`, `3->1`, `3->2` (epsilons unlabelled).

        closure(0) = {0,1,2}    closure(1) = {1}
        closure(2) = {2}        closure(3) = {1,2,3}

    The only symbol edge leaves state 2, so a state gets an outgoing `a`
    exactly when its closure contains 2: states 0, 2 and 3.
    A state accepts exactly when its closure contains 1: states 0, 1 and 3.
    """
    machine = compile_pattern("a*")
    assert machine.state_count == 4
    assert machine.start == 0
    assert machine.accepting == frozenset({0, 1, 3})
    assert set(machine.symbol_edges) == {
        SymbolEdge(0, 97, 3),
        SymbolEdge(2, 97, 3),
        SymbolEdge(3, 97, 3),
    }


def test_star_leaves_dead_states_behind():
    """Elimination strips the plumbing's *edges*, not the plumbing's *states*.

    States 1 and 2 of `a*` were pure epsilon scaffolding; nothing can enter them
    any more. They are kept anyway: the project does no state minimisation, and
    stable numbering across stages is worth more than two flip-flops here.
    """
    machine = compile_pattern("a*")
    assert machine.reachable_states() == frozenset({0, 3})
    assert machine.state_count == 4


def test_single_char_is_unchanged_in_substance():
    machine = compile_pattern("a")
    assert machine.symbol_edges == (SymbolEdge(0, 97, 1),)
    assert machine.accepting == frozenset({1})
    assert machine.start not in machine.accepting   # "" must not match


# ================================================== the accepting set is a set
@pytest.mark.parametrize(
    "pattern,matches_empty",
    [
        ("a", False),
        ("ab", False),
        ("a*", True),
        ("a**", True),
        ("(a*)*", True),
        ("(a|b)*", True),
        ("a*b*", True),
        ("(a|b)*abb", False),
        ("a*b", False),
    ],
)
def test_start_is_accepting_exactly_when_the_empty_string_matches(
    pattern, matches_empty
):
    machine = compile_pattern(pattern)
    assert (machine.start in machine.accepting) is matches_empty
    assert machine.matches("") is matches_empty


def test_accepting_can_hold_several_states():
    # The cost of taking the closure before the byte rather than after.
    assert len(compile_pattern("a*").accepting) > 1


def test_accepting_is_never_empty():
    for pattern in ("a", "a*", "(a|b)*abb", "((a|b)*c)*"):
        assert compile_pattern(pattern).accepting


# ================================================ the shape stage 4 will use
def test_incoming_covers_every_state_and_every_edge():
    machine = compile_pattern("(a|b)*abb")
    table = machine.incoming()
    assert set(table) == set(range(machine.state_count))
    assert sum(len(v) for v in table.values()) == len(machine.symbol_edges)


def test_incoming_is_the_next_state_equation():
    """Each entry is one OR term: `state[j] AND in_byte == c`."""
    machine = compile_pattern("a*")
    # State 3 is entered from 0, 2 and 3, always on 'a'.
    assert sorted(machine.incoming()[3]) == [(0, 97), (2, 97), (3, 97)]
    # Nothing enters the dead scaffolding states.
    assert machine.incoming()[1] == []
    assert machine.incoming()[2] == []


# ============================================== the cross-check that matters
CROSS_CHECK_PATTERNS = [
    "a", "b", "ab", "ba", "aab", "a|b", "a|ab", "ab|ba",
    "a*", "b*", "a**", "(a*)*", "a*b", "ab*", "a*b*", "(ab)*", "(ba)*",
    "(a|b)*", "(a|b)*a", "(a|b)*abb", "a(a|b)*b", "((a|b)*a)*b",
    "(a|ab)*", "(aa)*", "(aa|b)*", "a(ba)*", "((ab)*|b)*", "(a*b)*",
]


@pytest.mark.parametrize("pattern", CROSS_CHECK_PATTERNS)
def test_three_implementations_agree(pattern):
    """AST oracle, epsilon-NFA and epsilon-free NFA, over all of {a,b}* to length 6.

    Elimination is a language-preserving rewrite, so any disagreement between
    the second and third means the rewrite is wrong. Keeping the oracle in the
    comparison catches the case where stages 2 and 3 are wrong *together*.
    """
    tree = parse(pattern)
    nfa = build(tree)
    flattened = eliminate(nfa)

    for length in range(7):
        for combo in itertools.product("ab", repeat=length):
            text = "".join(combo)
            expected = ast_matches(tree, text)
            assert nfa.matches(text) == expected, f"stage 2 wrong on {text!r}"
            assert flattened.matches(text) == expected, (
                f"{pattern!r}: elimination changed the language on {text!r}"
            )


def test_matches_accepts_bytes_as_well_as_str():
    machine = compile_pattern("ab")
    assert machine.matches(b"ab")
    assert machine.matches("ab")


def test_high_bytes_survive_elimination():
    machine = compile_pattern(r"\xff*")
    assert machine.matches(b"\xff\xff")
    assert not machine.matches(b"\x00")


def test_codepoint_above_255_is_rejected_at_the_input():
    with pytest.raises(ValueError, match="outside the 0..255"):
        compile_pattern("a").matches("€")


# ==================================================================== hygiene
def test_elimination_is_deterministic():
    nfa = build(parse("(a|b)*abb"))
    assert eliminate(nfa) == eliminate(nfa)


def test_result_is_hashable_and_frozen():
    machine = compile_pattern("a|b")
    assert isinstance(machine, EpsilonFreeNFA)
    hash(machine)
    with pytest.raises(Exception):
        machine.start = 99


def test_pretty_reports_the_headline_numbers():
    rendered = compile_pattern("a*").pretty()
    assert "states: 4" in rendered
    assert "epsilon edges: 0" in rendered
    assert "unreachable: 2" in rendered
