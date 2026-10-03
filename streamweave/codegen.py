"""Stage 4 - epsilon-free NFA -> synthesisable SystemVerilog (one-hot).

One flip-flop per NFA state. Every state's next value is computed in the same
clock cycle, in parallel, from the states pointing into it and a comparison on
the incoming byte. No DFA, no subset construction, no minimisation.

Input contract (stage 3, streamweave/epsilon.py):

    EpsilonFreeNFA
        .state_count   int
        .start         int                    single state (closure-before)
        .accepting     frozenset[int]         a SET, see epsilon walkthrough s4
        .symbol_edges  iterable[SymbolEdge]   SymbolEdge(source, symbol, target)

`symbol` is a byte value 0..255. Objects exposing `.ranges`/`.negated` are also
accepted, so this file keeps working if the parser later grows '.' or '[a-z]'.

Public API:
    generate(nfa, module_name=..., streaming=..., prune=...) -> str
    CLI:  python -m streamweave.codegen '(a|b)*abb' -o rtl/generated/pattern.sv
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

BYTE_MAX = 255

try:                                            # keep the error hierarchy shared
    from streamweave.errors import StreamWeaveError # type: ignore
except ImportError:                             # standalone use
    class StreamWeaveError(Exception):
        """Base class for every StreamWeave compiler error."""


class CodegenError(StreamWeaveError):
    """The NFA cannot be turned into hardware."""


# =============================================================================
# 1. Reading the stage 3 object
# =============================================================================


def _attr(obj: object, *names: str):
    """First attribute present, so a rename in stage 3 does not break stage 4."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise CodegenError(
        f"{type(obj).__name__} has none of {names!r}. "
        "Stage 3's interface has changed; fix the names in codegen._attr calls."
    )


def _as_state_set(obj: object, label: str) -> frozenset[int]:
    if isinstance(obj, int):
        return frozenset({obj})
    try:
        return frozenset(int(x) for x in obj)  # type: ignore[union-attr]
    except TypeError as exc:
        raise CodegenError(f"cannot read {label} from {obj!r}") from exc


@dataclass(frozen=True)
class Design:
    """Everything the emitter needs, with nothing left to decide."""

    state_count: int                                   # states in the source NFA
    start: int
    accepting: frozenset[int]
    edges: tuple[tuple[int, object, int], ...]         # (source, symbol, target)
    order: tuple[int, ...]                             # NFA ids, in bit order
    bit: dict[int, int]                                # NFA id -> bit position
    dead: tuple[int, ...]                              # unreachable NFA ids

    @property
    def width(self) -> int:
        return len(self.order)


def reachable(start: int, edges) -> frozenset[int]:
    """Forward BFS from the start state. Dead-code elimination, not minimisation.

    Worth being able to state the difference: this removes states nothing can
    ever enter. Minimisation (Hopcroft/Moore) merges states that are
    *behaviourally equivalent* - a different algorithm the project does not use.
    """
    out: dict[int, list[int]] = {}
    for src, _sym, dst in edges:
        out.setdefault(src, []).append(dst)
    seen = {start}
    work = [start]
    while work:
        s = work.pop()
        for d in out.get(s, ()):
            if d not in seen:
                seen.add(d)
                work.append(d)
    return frozenset(seen)


def read_nfa(nfa: object, *, prune: bool = False) -> Design:
    """Validate the stage 3 object and freeze it into a Design."""
    eps = None
    for name in ("epsilon_edges", "epsilon_transitions"):
        if hasattr(nfa, name):
            eps = getattr(nfa, name)
            break
    if eps:
        raise CodegenError(
            f"{len(eps)} epsilon edge(s) reached the code generator. An epsilon "
            "edge moves without consuming a byte, so in hardware it is a wire "
            "between two flip-flops - and around a '*' loop that is a zero-delay "
            "feedback path with no register to break it. Verilator rejects it "
            "(UNOPTFLAT) and Quartus cannot time it. Run stage 3 first."
        )

    state_count = int(_attr(nfa, "state_count", "num_states"))
    start = int(_attr(nfa, "start"))
    accepting = _as_state_set(_attr(nfa, "accepting", "accepts", "accept"), "accepting")

    edges: list[tuple[int, object, int]] = []
    for e in _attr(nfa, "symbol_edges", "symbol_transitions"):
        edges.append(
            (
                int(_attr(e, "source", "src")),
                _attr(e, "symbol", "symbols"),
                int(_attr(e, "target", "dst")),
            )
        )

    if state_count <= 0:
        raise CodegenError("NFA has no states")
    for label, group in (("start", {start}), ("accepting", accepting)):
        for s in group:
            if not 0 <= s < state_count:
                raise CodegenError(f"{label} state {s} outside 0..{state_count - 1}")
    for src, _sym, dst in edges:
        if not (0 <= src < state_count and 0 <= dst < state_count):
            raise CodegenError(f"edge ({src} -> {dst}) outside 0..{state_count - 1}")

    live = reachable(start, edges)
    dead = tuple(s for s in range(state_count) if s not in live)

    if prune:
        order = tuple(sorted(live))
        edges = [e for e in edges if e[0] in live and e[2] in live]
        accepting = frozenset(accepting & live)
    else:
        order = tuple(range(state_count))

    bit = {state: i for i, state in enumerate(order)}

    # Deterministic: identical NFAs must give byte-identical RTL, so CI can diff
    # generated files and a Quartus result stays attributable to a commit.
    edges.sort(key=lambda e: (bit[e[2]], bit[e[0]], _symbol_key(e[1])))

    return Design(
        state_count=state_count,
        start=start,
        accepting=accepting,
        edges=tuple(edges),
        order=order,
        bit=bit,
        dead=dead,
    )


# =============================================================================
# 2. Symbols -> comparators
# =============================================================================


def _symbol_key(symbol: object) -> str:
    return f"{symbol:03d}" if isinstance(symbol, int) else repr(symbol)


def symbol_expr(symbol: object) -> str:
    """SystemVerilog boolean that is 1 when `data` matches this edge's label."""
    if isinstance(symbol, int):
        if not 0 <= symbol <= BYTE_MAX:
            raise CodegenError(f"symbol {symbol} outside the 0..255 byte alphabet")
        return f"(data == 8'h{symbol:02x})"          # one comparator

    ranges = getattr(symbol, "ranges", None)
    if ranges is None:
        raise CodegenError(
            f"cannot turn {symbol!r} into a comparator: expected an int byte or "
            "an object with .ranges"
        )
    ranges = tuple(ranges)
    negated = bool(getattr(symbol, "negated", False))

    multi = False
    if not ranges:
        base = "1'b0"
    elif ranges == ((0, BYTE_MAX),):
        base = "1'b1"                                # '.' costs no logic at all
    else:
        terms = []
        for lo, hi in ranges:
            if lo == hi:
                terms.append(f"(data == 8'h{lo:02x})")
            elif lo == 0:
                terms.append(f"(data <= 8'h{hi:02x})")     # data >= 0 is a tautology
            elif hi == BYTE_MAX:
                terms.append(f"(data >= 8'h{lo:02x})")     # data <= 255 likewise
            else:
                terms.append(f"((data >= 8'h{lo:02x}) && (data <= 8'h{hi:02x}))")
        base = " || ".join(terms)
        multi = len(terms) > 1

    if not negated:
        return base
    if base == "1'b1":
        return "1'b0"
    if base == "1'b0":
        return "1'b1"
    # negation is one inverter, not a re-expansion into complementary ranges
    return f"!({base})" if multi else f"!{base}"


def _show_byte(code: int) -> str:
    named = {0: "\\0", 9: "\\t", 10: "\\n", 13: "\\r"}
    if code in named:
        return named[code]
    if 32 <= code <= 126:
        return chr(code)
    return f"\\x{code:02x}"


def symbol_label(symbol: object) -> str:
    """Human-readable label for the generated comment."""
    if isinstance(symbol, int):
        return f"'{_show_byte(symbol)}'"
    ranges = tuple(getattr(symbol, "ranges", ()))
    negated = bool(getattr(symbol, "negated", False))
    if ranges == ((0, BYTE_MAX),) and not negated:
        return "."
    body = "".join(
        _show_byte(lo) if lo == hi else f"{_show_byte(lo)}-{_show_byte(hi)}"
        for lo, hi in ranges
    )
    if len(ranges) == 1 and ranges[0][0] == ranges[0][1] and not negated:
        return f"'{body}'"
    return f"[{'^' if negated else ''}{body}]"


def collect_classes(design: Design) -> tuple[list[object], dict[object, int]]:
    """One comparator per DISTINCT symbol, in first-appearance order.

    Byte ints (and frozen SymbolSet-style objects) hash structurally, so every
    edge labelled 'a' collapses onto one wire. '(a|b)*abb' has 22 edges but only
    two distinct labels, so it builds two comparators, not twenty-two.
    """
    seen: dict[object, None] = {}
    for _src, sym, _dst in design.edges:
        seen.setdefault(sym, None)
    classes = list(seen)
    return classes, {s: i for i, s in enumerate(classes)}


# =============================================================================
# 3. Edges -> next-state equations
# =============================================================================


def incoming_terms(design: Design, index: dict[object, int]) -> dict[int, list[tuple[int, int]]]:
    """bit(dst) -> [(bit(src), class index), ...] - the AND terms feeding a state.

    Stage 3's edge list is organised by SOURCE. Hardware needs it by
    DESTINATION, because what you are writing is one flip-flop's D input at a
    time. Inverting that mapping is the entire compiler.
    """
    terms: dict[int, list[tuple[int, int]]] = {}
    for src, sym, dst in design.edges:
        terms.setdefault(design.bit[dst], []).append((design.bit[src], index[sym]))
    for dst in terms:
        terms[dst] = sorted(set(terms[dst]))
    return terms


# =============================================================================
# 4. The emitter
# =============================================================================


def _mask(states, design: Design) -> str:
    bits = "".join(
        "1" if design.order[i] in states else "0" for i in range(design.width - 1, -1, -1)
    )
    return f"{design.width}'b{bits}"


def generate(
    nfa: object,
    *,
    module_name: str = "pattern_match",
    pattern: str | None = None,
    streaming: bool = True,
    prune: bool = False,
) -> str:
    """Emit a synthesisable SystemVerilog pattern detector.

    streaming=True  : the start state is held active every cycle, so a match may
                      begin at any byte offset. Equivalent to a '.*' prefix, and
                      what a real stream scanner needs.
    streaming=False : anchored. The start state is active only out of reset, so
                      `match` after byte i means "bytes 0..i are a complete
                      match" - exactly `nfa.matches()` semantics. Use this to
                      compare RTL against the Python model with no fudging.
    prune=True      : emit only reachable states, renumbered, with a map in the
                      header. Off by default - synthesis removes constant-zero
                      registers anyway, and stable numbering is worth more when
                      you are debugging on the board.
    """
    design = read_nfa(nfa, prune=prune)
    classes, index = collect_classes(design)
    terms = incoming_terms(design, index)

    n = design.width
    start_bit = design.bit[design.start]
    empty_match = design.start in design.accepting
    renumbered = bool(prune and design.dead)

    out: list[str] = []
    w = out.append

    # ---- header ------------------------------------------------------------
    w("// " + "=" * 74)
    w("// Generated by StreamWeave stage 4 (codegen.py). DO NOT EDIT BY HAND.")
    if pattern is not None:
        w(f"//   pattern        : {pattern}")
    w(f"//   mode           : {'streaming search' if streaming else 'anchored'}")
    w(f"//   flip-flops     : {n}   (one per NFA state)")
    w(f"//   AND terms      : {len(design.edges)}   (one per symbol edge)")
    w(f"//   comparators    : {len(classes)}   (one per distinct symbol)")
    w(f"//   start state    : {design.start}  -> bit {start_bit}")
    w(f"//   accepting      : {sorted(design.accepting)}")
    if design.dead and not prune:
        w(f"//   unreachable    : {list(design.dead)}")
        w("//                    kept for stable numbering across stages; their D")
        w("//                    inputs are constant 0, so synthesis deletes them.")
    elif design.dead:
        w(f"//   pruned         : {list(design.dead)}  ({design.state_count} -> {n} states)")
    if renumbered:
        w("// " + "-" * 74)
        w("//   bit -> NFA state (pruned, so numbering no longer matches stage 3)")
        for i in range(0, n, 8):
            chunk = ", ".join(f"{b}={design.order[b]}" for b in range(i, min(i + 8, n)))
            w(f"//     {chunk}")
    w("// " + "-" * 74)
    w("// TIMING: `match` is registered. It asserts one clock AFTER the final byte")
    w("//         of a match is presented with `valid` high. The cocotb testbench")
    w("//         must account for that one cycle or it will report 100% mismatch.")
    if empty_match:
        w("// NOTE:   this pattern accepts the empty string (the start state is")
        w("//         accepting). In streaming mode `match` is then high on every")
        w("//         cycle - correct, and useless. The empty match at reset is")
        w("//         deliberately NOT reported: match_q resets to 0.")
    w("// " + "=" * 74)
    w("")
    w("`default_nettype none")
    w("")

    # ---- ports -------------------------------------------------------------
    w(f"module {module_name} (")
    w("    input  wire        clk,")
    w("    input  wire        rst_n,   // active-low reset")
    w("    input  wire        valid,   // a byte is present on `data` this cycle")
    w("    input  wire [7:0]  data,")
    w("    output wire        match")
    w(");")
    w("")
    w(f"    localparam int NUM_STATES = {n};")
    w(f"    localparam logic [{n - 1}:0] START_MASK  = {_mask({design.start}, design)};")
    w(f"    localparam logic [{n - 1}:0] ACCEPT_MASK = {_mask(design.accepting, design)};")
    w("")

    # ---- registers ---------------------------------------------------------
    w("    // ---- one flip-flop per NFA state ---------------------------------")
    vec = f"[{n - 1}:0]"
    w(f"    logic {vec} state_q;   // states live after the bytes seen so far")
    w(f"    logic {vec} state_d;   // states live after this cycle's byte")
    w(f"    logic {' ' * len(vec)} match_q;")
    w("")

    # ---- comparators -------------------------------------------------------
    w("    // ---- symbol decode: one comparator per DISTINCT symbol -----------")
    if classes:
        w(f"    logic [{len(classes) - 1}:0] cls;")
        w("    always_comb begin")
        pad = len(f"cls[{len(classes) - 1}]")
        for i, sym in enumerate(classes):
            lhs = f"cls[{i}]".ljust(pad)
            w(f"        {lhs} = {symbol_expr(sym)};   // {symbol_label(sym)}")
        w("    end")
    else:
        w("    logic [0:0] cls;")
        w("    always_comb cls = 1'b0;   // no symbol edges in this NFA")
    w("")

    # ---- next-state --------------------------------------------------------
    w("    // ---- next-state logic: ALL states evaluated in parallel ----------")
    w("    always_comb begin")
    w("        state_d = '0;              // default assignment: prevents latches")
    if streaming:
        w("        // streaming: start held active every cycle, so a match may begin")
        w("        // at any byte offset (equivalent to a '.*' prefix)")
        w(f"        state_d[{start_bit}] = 1'b1;")
    for dst_bit in sorted(terms):
        if streaming and dst_bit == start_bit:
            w(f"        // state_d[{start_bit}] forced above; its incoming terms are redundant")
            continue
        expr = " | ".join(f"(state_q[{s}] & cls[{c}])" for s, c in terms[dst_bit])
        note = f"   // NFA state {design.order[dst_bit]}" if renumbered else ""
        w(f"        state_d[{dst_bit}] = {expr};{note}")
    w("    end")
    w("")

    # ---- accept ------------------------------------------------------------
    w("    // ---- accept detection: reduction OR over the accepting bits ------")
    w("    wire accept_now = |(state_d & ACCEPT_MASK);")
    w("")

    # ---- sequential --------------------------------------------------------
    w("    always_ff @(posedge clk) begin")
    w("        if (!rst_n) begin")
    w("            state_q <= START_MASK;   // ONE bit, because stage 3 took the")
    w("            match_q <= 1'b0;         // closure BEFORE the byte")
    w("        end else if (valid) begin")
    w("            state_q <= state_d;")
    w("            match_q <= accept_now;")
    w("        end else begin")
    w("            match_q <= 1'b0;         // no byte consumed, no match reported")
    w("        end")
    w("    end")
    w("")
    w("    assign match = match_q;")
    w("")
    w("endmodule")
    w("")
    w("`default_nettype wire")

    return "\n".join(out) + "\n"


def report(nfa: object, *, prune: bool = False) -> str:
    """One-line resource summary. These are Python numbers, not Quartus numbers."""
    design = read_nfa(nfa, prune=prune)
    classes, _ = collect_classes(design)
    return (
        f"flip-flops={design.width}  "
        f"and_terms={len(design.edges)}  "
        f"comparators={len(classes)}  "
        f"unreachable={len(design.dead)}"
        f"{' (pruned)' if prune else ''}"
    )


# =============================================================================
# 5. CLI
# =============================================================================


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m streamweave.codegen")
    ap.add_argument("pattern", help="regular expression")
    ap.add_argument("-o", "--output", help="write SystemVerilog here (default: stdout)")
    ap.add_argument("-m", "--module", default="pattern_match", help="module name")
    ap.add_argument(
        "--anchored",
        action="store_true",
        help="anchored mode: match only from byte 0 (matches nfa.matches() semantics)",
    )
    ap.add_argument(
        "--prune",
        action="store_true",
        help="emit only reachable states, renumbered (default: keep stage 3 numbering)",
    )
    args = ap.parse_args(argv[1:])

    try:
        from streamweave.epsilon import compile_pattern

        nfa = compile_pattern(args.pattern)
        sv = generate(
            nfa,
            module_name=args.module,
            pattern=args.pattern,
            streaming=not args.anchored,
            prune=args.prune,
        )
        summary = report(nfa, prune=args.prune)
    except StreamWeaveError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(sv)
        print(f"{args.output}: {summary}", file=sys.stderr)
    else:
        sys.stdout.write(sv)
        print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))