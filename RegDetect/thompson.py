"""Thompson construction: AST in, epsilon-NFA out.

Stage 2. Each of the four node types gets exactly one rule that builds a small
NFA fragment with **one entry state and one exit state**, and the rules compose
by wiring fragments together. That single-entry/single-exit invariant is the
whole trick: a rule never needs to know anything about the shape of its
children, only where to plug in.

    Char(c)        s --c--> a                              2 new states
    Concat(L, R)   L.accept --eps--> R.start               0 new states
    Alt(L, R)      s =eps=> both starts, both accepts =eps=> a    2 new states
    Star(B)        s --eps--> B.start,  B.accept --eps--> B.start
                   s --eps--> a,        B.accept --eps--> a       2 new states

So the state count is `2 x (number of nodes that are not Concat)`, which is
exactly what `parser.state_estimate` predicts. `test_matches_the_parser_estimate`
holds the two to each other.

**Why epsilon transitions are fine here and fatal later.** An epsilon edge means
"move without consuming a byte". That is what lets each rule stay local -- `Alt`
can join two branches without knowing how long either is. But an epsilon has no
clock cycle attached to it, so wiring one as combinational logic in stage 4
would make `a*` a zero-delay combinational feedback loop, which Verilator and
Quartus both reject. Stage 3 therefore eliminates them: it computes epsilon
closures at build time and rewrites the real transitions to skip the epsilon
edges. This module deliberately produces the loops that stage 3 removes.

**The trap this module has to avoid.** AST nodes are frozen dataclasses, so they
compare and hash *by value*: the two `Char(97)` subtrees inside
`Concat(Char(97), Char(97))` are indistinguishable as values. `_Builder.build`
therefore allocates fresh states on **every visit** and never memoises on node
value. Caching here would merge two independent transitions into one state and
silently produce a machine that accepts the wrong language.

Usage:

    python -m streamweave.thompson '(a|b)*abb'
    python -m streamweave.thompson '(a|b)*abb' aabb
"""

import sys
from dataclasses import dataclass
from typing import NamedTuple

from .parser import Alt, Char, Concat, Node, Star, parse
from .tokenizer import BYTE_MAX, ParseError, render_byte


class SymbolEdge(NamedTuple):
    """`source --symbol--> target`, consuming one byte."""

    source: int
    symbol: int
    target: int


class EpsilonEdge(NamedTuple):
    """`source --eps--> target`, consuming nothing."""

    source: int
    target: int


@dataclass(frozen=True)
class NFA:
    """A Thompson fragment promoted to a whole machine.

    States are integers `0 .. state_count - 1`. There is exactly one `start` and
    exactly one `accept`, which is an invariant of the construction rather than
    a convenience -- every rule below relies on its children having it too.
    """

    state_count: int
    start: int
    accept: int
    symbol_edges: tuple[SymbolEdge, ...]
    epsilon_edges: tuple[EpsilonEdge, ...]

    def epsilon_closure(self, states: frozenset[int] | set[int]) -> frozenset[int]:
        """Every state reachable from `states` without consuming a byte.

        A plain worklist. The `seen` set is what makes epsilon *cycles* safe --
        `a*` has one by construction, so this cannot be a naive recursion.
        """
        seen = set(states)
        frontier = list(seen)
        while frontier:
            state = frontier.pop()
            for edge in self.epsilon_edges:
                if edge.source == state and edge.target not in seen:
                    seen.add(edge.target)
                    frontier.append(edge.target)
        return frozenset(seen)

    def step(self, states: frozenset[int], byte: int) -> frozenset[int]:
        """The states reachable from `states` by consuming exactly `byte`."""
        moved = {
            edge.target
            for edge in self.symbol_edges
            if edge.source in states and edge.symbol == byte
        }
        return self.epsilon_closure(moved)

    def matches(self, data: str | bytes) -> bool:
        """Whether the machine accepts the whole of `data`.

        The reference semantics for stage 5: the generated RTL is compared
        against this, never against Python's `re`, which supports constructs
        that are not regular and that the hardware correctly cannot implement.
        """
        current = self.epsilon_closure({self.start})
        for byte in _as_bytes(data):
            current = self.step(current, byte)
            if not current:
                return False  # every thread died; nothing can revive them
        return self.accept in current

    def edge_table(self) -> list[tuple[int, str, int]]:
        """Every edge as `(source, label, target)`, epsilons labelled `eps`."""
        rows = [
            (edge.source, repr(render_byte(edge.symbol)), edge.target)
            for edge in self.symbol_edges
        ]
        rows += [(edge.source, "eps", edge.target) for edge in self.epsilon_edges]
        return sorted(rows)

    def pretty(self) -> str:
        """Render the machine as an aligned edge listing."""
        lines = [
            f"states: {self.state_count}   start: {self.start}   "
            f"accept: {self.accept}",
            f"symbol edges: {len(self.symbol_edges)}   "
            f"epsilon edges: {len(self.epsilon_edges)}",
            "",
            "  from      on    to",
            "  ----  ------  ----",
        ]
        for source, label, target in self.edge_table():
            mark = ""
            if source == self.start:
                mark = "   <- start"
            if target == self.accept:
                mark += "   -> accept"
            lines.append(f"  {source:>4}  {label:>6}  {target:>4}{mark}")
        return "\n".join(lines)


def _as_bytes(data: str | bytes) -> list[int]:
    """Normalise input to a list of byte values, rejecting anything above 255."""
    if isinstance(data, bytes):
        return list(data)
    codes = []
    for index, char in enumerate(data):
        code = ord(char)
        if code > BYTE_MAX:
            raise ValueError(
                f"{char!r} at index {index} is U+{code:04X}, "
                f"outside the 0..255 byte alphabet"
            )
        codes.append(code)
    return codes


class _Builder:
    """Allocates states and accumulates edges while walking the AST."""

    def __init__(self) -> None:
        self.next_state = 0
        self.symbol_edges: list[SymbolEdge] = []
        self.epsilon_edges: list[EpsilonEdge] = []

    def new_state(self) -> int:
        """Hand out the next unused state number."""
        state = self.next_state
        self.next_state += 1
        return state

    def build(self, node: Node) -> tuple[int, int]:
        """Build a fragment for `node`, returning `(start, accept)`.

        Fresh states on every visit, and no memoisation on `node` -- see the
        trap in the module docstring.
        """
        if isinstance(node, Char):
            start, accept = self.new_state(), self.new_state()
            self.symbol_edges.append(SymbolEdge(start, node.code, accept))
            return start, accept

        if isinstance(node, Concat):
            # No new states: concatenation is pure wiring. Join the left
            # fragment's exit to the right fragment's entry and keep the outer
            # ends. This is why `Concat` costs zero flip-flops.
            left_start, left_accept = self.build(node.left)
            right_start, right_accept = self.build(node.right)
            self.epsilon_edges.append(EpsilonEdge(left_accept, right_start))
            return left_start, right_accept

        if isinstance(node, Alt):
            start, accept = self.new_state(), self.new_state()
            left_start, left_accept = self.build(node.left)
            right_start, right_accept = self.build(node.right)
            self.epsilon_edges += [
                EpsilonEdge(start, left_start),    # split into either branch
                EpsilonEdge(start, right_start),
                EpsilonEdge(left_accept, accept),  # rejoin from either branch
                EpsilonEdge(right_accept, accept),
            ]
            return start, accept

        if isinstance(node, Star):
            start, accept = self.new_state(), self.new_state()
            body_start, body_accept = self.build(node.child)
            self.epsilon_edges += [
                EpsilonEdge(start, body_start),        # enter the body
                EpsilonEdge(start, accept),            # or skip it entirely
                EpsilonEdge(body_accept, body_start),  # go round again
                EpsilonEdge(body_accept, accept),      # or leave
            ]
            return start, accept

        raise TypeError(f"not an AST node: {node!r}")


def build(node: Node) -> NFA:
    """Compile an AST into an epsilon-NFA. The public API of stage 2."""
    builder = _Builder()
    start, accept = builder.build(node)
    return NFA(
        state_count=builder.next_state,
        start=start,
        accept=accept,
        symbol_edges=tuple(builder.symbol_edges),
        epsilon_edges=tuple(builder.epsilon_edges),
    )


def compile_pattern(pattern: str) -> NFA:
    """Regex source straight to an epsilon-NFA, for convenience and the CLI."""
    return build(parse(pattern))


def main(argv: list[str]) -> int:
    """CLI: print the machine, and optionally test one string against it."""
    if len(argv) not in (2, 3):
        print(
            "usage: python -m streamweave.thompson '<regex>' [text-to-test]",
            file=sys.stderr,
        )
        return 2
    try:
        nfa = compile_pattern(argv[1])
    except ParseError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"pattern: {argv[1]}")
    print(nfa.pretty())
    if len(argv) == 3:
        verdict = "MATCH" if nfa.matches(argv[2]) else "no match"
        print(f"\n{argv[2]!r}: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
