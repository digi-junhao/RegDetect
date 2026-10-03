"""Epsilon elimination: epsilon-NFA in, epsilon-free NFA out.

Stage 3, and it is **not optional**. An epsilon edge means "move without
consuming a byte", which in hardware means "without a clock edge". Wired as
combinational logic, `Star`'s loop edge becomes a zero-delay feedback path --
a wire whose value depends on itself with no register in between -- and both
Verilator (`UNOPTFLAT`) and Quartus reject it. So the closures are computed
here, at build time, and the real transitions are rewritten to skip the epsilon
edges. What reaches stage 4 has every edge consuming exactly one byte, and
therefore taking exactly one clock cycle.

**The state count does not change.** This is a rewrite, not a subset
construction: the machine that comes out is another NFA with the same states,
not a DFA. State numbers are preserved too, which means a state in stage 4's
SystemVerilog can be traced straight back to the same number in stage 2's
listing. There is no DFA anywhere in this project.

The rewrite
-----------

For every state `p` and byte `c`:

    new_delta(p, c)  =  { e.target : e.source in closure(p), e.symbol == c }

Read: *from `p`, drift for free wherever you can, then consume one `c`.* Note
the closure is taken **before** the byte and not after. That choice is what lets
the start stay a single state, because the next step's own `closure(p)` picks up
the drift that would otherwise have to happen after this one.

    accepting  =  { p : the original accept state is in closure(p) }

The accept set becomes a *set*, which is the price of that choice. In hardware
it costs one OR gate: `match = |state[accepting]`.

Why this formulation
--------------------

The alternative -- closure after the byte as well -- keeps a single accept state
but forces the machine to *start* in a whole set of states. That would mean
stage 4's reset has to load an arbitrary bit pattern into the flip-flops instead
of setting exactly one. Trading a multi-bit reset for a wider OR on the output
is the better deal: the OR is combinational and free, the reset is not.

Usage:

    python -m streamweave.epsilon '(a|b)*abb'
    python -m streamweave.epsilon '(a|b)*abb' aabb
"""

import sys
from dataclasses import dataclass

from .parser import parse
from .thompson import NFA, SymbolEdge, build
from .tokenizer import BYTE_MAX, ParseError, render_byte


@dataclass(frozen=True)
class EpsilonFreeNFA:
    """An NFA in which every edge consumes exactly one byte.

    Same states and same numbering as the epsilon-NFA it came from. `accepting`
    is a set rather than a single state -- see the module docstring.
    """

    state_count: int
    start: int
    accepting: frozenset[int]
    symbol_edges: tuple[SymbolEdge, ...]

    def step(self, states: frozenset[int] | set[int], byte: int) -> frozenset[int]:
        """The states reachable from `states` by consuming exactly `byte`.

        No closure afterwards: there is nothing left to close over.
        """
        return frozenset(
            edge.target
            for edge in self.symbol_edges
            if edge.source in states and edge.symbol == byte
        )

    def matches(self, data: str | bytes) -> bool:
        """Whether the machine accepts the whole of `data`."""
        current: frozenset[int] = frozenset({self.start})
        for byte in _as_bytes(data):
            current = self.step(current, byte)
            if not current:
                return False
        return bool(current & self.accepting)

    def incoming(self) -> dict[int, list[tuple[int, int]]]:
        """For each state, the `(source, symbol)` pairs that can enter it.

        This is precisely the shape of stage 4's next-state equation:

            next_state[i] = OR over (j, c) of ( state[j] AND in_byte == c )
        """
        table: dict[int, list[tuple[int, int]]] = {
            state: [] for state in range(self.state_count)
        }
        for edge in self.symbol_edges:
            table[edge.target].append((edge.source, edge.symbol))
        return table

    def reachable_states(self) -> frozenset[int]:
        """States actually reachable from `start`. Informational only.

        Elimination leaves some states with no incoming edges -- typically the
        pure-plumbing states that only ever had epsilon edges. They are kept, so
        numbering stays aligned with stage 2, and because the project does no
        state minimisation. Stage 4 may prune them; nothing here does.
        """
        seen = {self.start}
        frontier = [self.start]
        while frontier:
            state = frontier.pop()
            for edge in self.symbol_edges:
                if edge.source == state and edge.target not in seen:
                    seen.add(edge.target)
                    frontier.append(edge.target)
        return frozenset(seen)

    def pretty(self) -> str:
        """Render the machine as an aligned edge listing."""
        reachable = self.reachable_states()
        dead = self.state_count - len(reachable)
        lines = [
            f"states: {self.state_count}   start: {self.start}   "
            f"accepting: {sorted(self.accepting)}",
            f"symbol edges: {len(self.symbol_edges)}   epsilon edges: 0"
            f"   unreachable: {dead}",
            "",
            "  from      on    to",
            "  ----  ------  ----",
        ]
        for edge in sorted(self.symbol_edges):
            marks = []
            if edge.source == self.start:
                marks.append("<- start")
            if edge.target in self.accepting:
                marks.append("-> accepting")
            suffix = "   " + "  ".join(marks) if marks else ""
            lines.append(
                f"  {edge.source:>4}  {render_byte(edge.symbol)!r:>6}  "
                f"{edge.target:>4}{suffix}"
            )
        return "\n".join(lines)


def _as_bytes(data: str | bytes) -> list[int]:
    """Normalise input to byte values, rejecting anything above 255."""
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


def eliminate(nfa: NFA) -> EpsilonFreeNFA:
    """Rewrite an epsilon-NFA into an epsilon-free NFA of the same size."""
    # One closure per state, computed once. Every state needs its own because
    # the rewrite asks "where can I drift from here?" for each `p` separately.
    closures = [
        nfa.epsilon_closure({state}) for state in range(nfa.state_count)
    ]

    edges = {
        SymbolEdge(state, edge.symbol, edge.target)
        for state in range(nfa.state_count)
        for edge in nfa.symbol_edges
        if edge.source in closures[state]
    }

    accepting = frozenset(
        state
        for state in range(nfa.state_count)
        if nfa.accept in closures[state]
    )

    return EpsilonFreeNFA(
        state_count=nfa.state_count,
        start=nfa.start,
        accepting=accepting,
        symbol_edges=tuple(sorted(edges)),
    )


def compile_pattern(pattern: str) -> EpsilonFreeNFA:
    """Regex source straight to an epsilon-free NFA. The stage 4 entry point."""
    return eliminate(build(parse(pattern)))


def main(argv: list[str]) -> int:
    """CLI: print the epsilon-free machine, and optionally test one string."""
    if len(argv) not in (2, 3):
        print(
            "usage: python -m streamweave.epsilon '<regex>' [text-to-test]",
            file=sys.stderr,
        )
        return 2
    try:
        machine = compile_pattern(argv[1])
    except ParseError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"pattern: {argv[1]}")
    print(machine.pretty())
    if len(argv) == 3:
        verdict = "MATCH" if machine.matches(argv[2]) else "no match"
        print(f"\n{argv[2]!r}: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
