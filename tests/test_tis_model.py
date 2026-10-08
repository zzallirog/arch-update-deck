"""Model check of control.tis plus interpreter semantics tests for tis.py.

Part 1 enumerates every scripted world up to a depth, runs the shipped program
against each and checks invariants on the ordered log of world I/O.  The case
set is generated from `spec`, a plain-Python statement of what the program is
meant to do, so the enumeration does not come from the program under test.

Why a depth bound is enough.  Let p be the number of APPLY answers > 0 in a run.
A WATCH read happens at the start and after each APPLY > 0 or CENSUS > 0, and
at most 3 CENSUS reads can answer, so WATCH reads <= 1 + p + 3.  Each WATCH
read is followed by at most one APPLY or one CENSUS exchange, so world reads
<= 2 * (p + 4) + 3 = 2p + 11.  Only APPLY > 0 loops are unbounded: the program
alone does not bound p, the world does (see test_endless_green_world_...).  A
world with p positive APPLY answers therefore halts within 2p + 11 reads, and
the enumeration below covers every answer sequence of up to DEPTH reads.

The mutation tests re-run the checker against edited copies of the program text
and assert that the named invariant fires, i.e. that the checker can fail.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from arch_updater import tis

PROGRAM_TEXT = Path(tis.__file__).with_name("control.tis").read_text(encoding="utf-8")

FULL = {"WATCH": (1, 0, -1, -4, -999), "APPLY": (1, 0, -1, -999), "CENSUS": (1, 0)}
REDUCED = {"WATCH": (1, 0, -1), "APPLY": (1, 0, -1), "CENSUS": (1, 0)}
FULL_DEPTH = 10  # every sequence of up to 10 world reads over the full alphabets
REDUCED_DEPTH = 14  # deeper, over a smaller alphabet, so the repair budget runs out inside it
BUDGET = 3


# ── the specification, independent of control.tis ───────────────────────────


class _Need(Exception):
    def __init__(self, port: str) -> None:
        super().__init__(port)
        self.port = port


def spec(read, write) -> int:
    """What the program is meant to do, written as ordinary code."""
    budget = BUDGET
    while True:
        verdict = read("WATCH")
        if verdict > 0:
            update = read("APPLY")
            if update > 0:
                continue
            if update == 0:
                return 1
            verdict = update
        elif verdict == 0:
            return 0
        if budget == 0:
            return 0
        budget -= 1
        write("CENSUS", verdict)
        if read("CENSUS") > 0:
            continue
        return 0


def _enumerate(alphabet, depth):
    found: list[tuple[tuple[str, int], ...]] = []

    def extend(prefix):
        queue = iter(prefix)

        def read(port):
            try:
                return next(queue)[1]
            except StopIteration:
                raise _Need(port) from None

        try:
            spec(read, lambda port, value: None)
        except _Need as need:
            if len(prefix) < depth:
                for answer in alphabet[need.port]:
                    extend((*prefix, (need.port, answer)))
            return
        found.append(prefix)

    extend(())
    return found


def _cases():
    seen = dict.fromkeys(_enumerate(FULL, FULL_DEPTH))
    seen.update(dict.fromkeys(_enumerate(REDUCED, REDUCED_DEPTH)))
    return list(seen)


CASES = _cases()


def case_id(path) -> str:
    return ".".join(f"{port[0]}{answer}" for port, answer in path)


def expected_events(path):
    """The spec's own log for this world: reads as scripted, writes where the spec makes them."""
    script = iter(path)
    events: list[tuple] = []

    def read(port):
        got_port, answer = next(script)
        assert got_port == port
        events.append(("R", port, answer))
        return answer

    def write(port, value):
        events.append(("W", port, value))

    return events, spec(read, write)


# ── a world that follows one script and logs everything in order ────────────


class Overread(Exception):
    """The program asked for something the script does not have next."""


class Scripted:
    def __init__(self, path) -> None:
        self.script = list(path)
        self.events: list[tuple] = []

    def read(self, port):
        if not self.script or self.script[0][0] != port:
            self.events.append(("R", port, None))
            raise Overread(port)
        answer = self.script.pop(0)[1]
        self.events.append(("R", port, answer))
        return answer

    def write(self, port, value):
        self.events.append(("W", port, value))


def play(nodes, path):
    world = Scripted(path)
    result = error = None
    try:
        result = tis.run(nodes, world, halt="OUT")
    except Exception as exc:  # noqa: BLE001 - every way of not halting cleanly is a finding
        error = exc
    return world.events, result, error


def _is(event, kind, port):
    return event is not None and event[0] == kind and event[1] == port


def violations(events, result, error, path) -> set[str]:
    """Names of the invariants this run breaks. Empty means the run is sound."""
    bad: set[str] = set()
    at = lambda i: events[i] if 0 <= i < len(events) else None  # noqa: E731
    positive = lambda event: event is not None and event[2] is not None and event[2] > 0  # noqa: E731

    # I1: terminates, returns 0 or 1
    if error is not None or result not in (0, 1):
        bad.add("I1-terminates-with-0-or-1")

    repair_reads = 0
    for i, event in enumerate(events):
        nxt = at(i + 1)
        if _is(event, "R", "APPLY"):  # I2
            if not (_is(at(i - 1), "R", "WATCH") and positive(at(i - 1))):
                bad.add("I2-apply-only-after-green-census")
            if positive(event) and not _is(nxt, "R", "WATCH"):
                bad.add("I7-update-is-followed-by-census")
        if _is(event, "R", "CENSUS"):
            repair_reads += 1
            if repair_reads > BUDGET:
                bad.add("I3a-at-most-3-repair-reads")
            before, verdict = at(i - 1), at(i - 2)
            echo = (
                _is(before, "W", "CENSUS")
                and before[2] < 0
                and verdict is not None
                and verdict[0] == "R"
                and verdict[1] in ("WATCH", "APPLY")
                and verdict[2] == before[2]
            )
            if not echo:
                bad.add("I3b-repair-read-follows-echo-of-last-failure")
            if positive(event) and not _is(nxt, "R", "WATCH"):
                bad.add("I4a-repaired-then-census")
            if not positive(event) and (nxt is not None or result != 0):
                bad.add("I4b-no-repair-halts-0-silently")
        if event[0] == "W" and not (event[1] == "CENSUS" and _is(nxt, "R", "CENSUS")):
            bad.add("I3b-repair-read-follows-echo-of-last-failure")  # a write nothing reads, or to another port
        if event[0] == "R" and event[1] in ("WATCH", "APPLY") and event[2] is not None and event[2] < 0:
            if repair_reads >= BUDGET:  # I4c: budget spent
                if nxt is not None or result != 0:
                    bad.add("I4c-spent-budget-halts-0-silently")
            elif not (_is(nxt, "W", "CENSUS") and nxt[2] == event[2]):  # I4d
                bad.add("I4d-failure-with-budget-left-goes-to-repair")
        if _is(event, "R", "WATCH") and event[2] == 0 and (nxt is not None or result != 0):
            bad.add("I6-unknown-census-halts-0")

    # I5: a 1 needs a green census then an empty queue
    if result == 1 and not (
        len(events) >= 2 and _is(events[-2], "R", "WATCH") and events[-2][2] is not None and events[-2][2] > 0
        and _is(events[-1], "R", "APPLY") and events[-1][2] == 0
    ):
        bad.add("I5-one-only-after-green-then-empty")
    if (
        error is None
        and len(events) >= 2
        and _is(events[-2], "R", "WATCH") and events[-2][2] is not None and events[-2][2] > 0
        and _is(events[-1], "R", "APPLY") and events[-1][2] == 0
        and result != 1
    ):
        bad.add("I5b-green-then-empty-is-one")

    # I8: the log is the spec's log; nothing of the script is left over, nothing extra was asked
    expected, expected_result = expected_events(path)
    if events != expected or (error is None and result != expected_result):
        bad.add("I8-matches-spec")

    # I9: the proven bound on world reads
    updates = sum(1 for e in events if _is(e, "R", "APPLY") and positive(e))
    if sum(1 for e in events if e[0] == "R") > 2 * updates + 11:
        bad.add("I9-read-bound-2p+11")
    return bad


NODES = tis.parse(PROGRAM_TEXT)


@pytest.mark.parametrize("path", CASES, ids=[case_id(p) for p in CASES])
def test_scripted_world_keeps_every_invariant(path) -> None:
    events, result, error = play(NODES, path)
    assert error is None, f"{type(error).__name__}: {error} after {events}"
    assert violations(events, result, error, path) == set()
    assert result == expected_events(path)[1]


@pytest.mark.parametrize("path", CASES, ids=[case_id(p) for p in CASES])
def test_scripted_world_does_not_depend_on_node_declaration_order(path) -> None:
    flipped = dict(reversed(list(NODES.items())))
    assert list(flipped) == ["FIX", "CTL"]
    straight = play(NODES, path)
    events, result, error = play(flipped, path)
    assert error is None
    assert (events, result) == straight[:2]


def test_the_enumeration_is_large_distinct_and_reaches_the_corners() -> None:
    assert len(CASES) >= 1000
    assert len(set(CASES)) == len(CASES)
    ids = [case_id(p) for p in CASES]
    assert len(set(ids)) == len(ids)
    runs = [expected_events(p) for p in CASES]
    assert any(sum(e[0] == "R" and e[1] == "CENSUS" for e in ev) == 3 for ev, _ in runs)  # budget fully used
    spent = [
        ev for ev, _ in runs
        if sum(e[0] == "R" and e[1] == "CENSUS" for e in ev) == 3 and ev[-1][2] is not None and ev[-1][2] < 0
    ]
    assert spent  # a fourth failure after three repairs
    assert {r for _, r in runs} == {0, 1}
    assert max(len(ev) for ev, _ in runs) >= 9


def test_endless_green_world_is_stopped_by_the_step_budget_not_by_the_program() -> None:
    class Endless:
        def read(self, port):
            return 1

        def write(self, port, value):
            raise AssertionError

    with pytest.raises(RuntimeError, match="step budget"):
        tis.run(tis.parse(PROGRAM_TEXT), Endless(), halt="OUT")


def test_a_repair_answer_outside_the_contract_leaks_to_out() -> None:
    """Documents: a CENSUS answer < 0 is passed straight to OUT. The real World only answers 0 or 1."""
    events, result, error = play(NODES, (("WATCH", -1), ("CENSUS", -1)))
    assert error is None and result == -1
    assert "I1-terminates-with-0-or-1" in violations(events, result, error, (("WATCH", -1), ("CENSUS", -1)))


def test_token_trail_of_a_repair_and_of_a_refused_repair() -> None:
    def trail(path):
        trace = []
        world = Scripted(path)
        tis.run(NODES, world, halt="OUT", trace=lambda s, t, v: trace.append((s, t, v)))
        return trace

    assert trail((("WATCH", -4), ("CENSUS", 1), ("WATCH", 1), ("APPLY", 0))) == [
        ("WATCH", "CTL", -4), ("CTL", "FIX", -4), ("FIX", "CENSUS", -4), ("CENSUS", "FIX", 1), ("FIX", "CTL", 1),
        ("WATCH", "CTL", 1), ("APPLY", "CTL", 0), ("CTL", "OUT", 1),
    ]
    spent = (("WATCH", -1), ("CENSUS", 1)) * 3 + (("WATCH", -7),)
    tail = trail(spent)[-4:]
    assert tail == [("WATCH", "CTL", -7), ("CTL", "FIX", -7), ("FIX", "CTL", 0), ("CTL", "OUT", 0)]


# ── mutation experiments: every invariant must be able to fail ──────────────


def mutate(text: str, old: str, new: str, nth: int = 0) -> str:
    starts = [i for i in range(len(text)) if text.startswith(old, i)]
    assert len(starts) > nth, f"mutation target {old!r} #{nth} not in control.tis"
    at = starts[nth]
    return text[:at] + new + text[at + len(old):]


MUTATIONS = {
    "answer-2-for-idle": (("MOV 1 ACC", "MOV 2 ACC", 0), {"I1-terminates-with-0-or-1"}),
    "no-halt-write-wraps-forever": (("HALT:   MOV ACC DOWN", "HALT:   NOP", 0), {"I1-terminates-with-0-or-1"}),
    "unknown-census-reads-apply": (("JEZ HALT", "JEZ GREEN", 0), {"I2-apply-only-after-green-census", "I6-unknown-census-halts-0"}),
    "budget-of-four": (("MOV 3 ACC", "MOV 4 ACC", 0), {"I3a-at-most-3-repair-reads", "I4c-spent-budget-halts-0-silently"}),
    "budget-never-spent": (("SUB 1", "NOP", 0), {"I3a-at-most-3-repair-reads"}),
    "budget-of-two": (("MOV 3 ACC", "MOV 2 ACC", 0), {"I4d-failure-with-budget-left-goes-to-repair"}),
    "repair-gets-wrong-value": (("MOV ACC LEFT", "MOV -1 LEFT", 0), {"I3b-repair-read-follows-echo-of-last-failure"}),
    "repaired-skips-census": (("JGZ WATCH", "JGZ GREEN", 0), {"I4a-repaired-then-census"}),
    "no-repair-loops-back": (("JMP HALT", "JMP WATCH", 0), {"I4b-no-repair-halts-0-silently"}),
    "spent-lane-says-yes": (("MOV 0 RIGHT", "MOV 1 RIGHT", 0), {"I4c-spent-budget-halts-0-silently"}),
    "apply-failure-not-repaired": (("JLZ RED", "JLZ HALT", 0), {"I4d-failure-with-budget-left-goes-to-repair"}),
    "update-halts-with-its-own-answer": (("JGZ WATCH", "JGZ HALT", 1), {"I5-one-only-after-green-then-empty", "I7-update-is-followed-by-census"}),
    "empty-queue-answers-zero": (("MOV 1 ACC", "MOV 0 ACC", 0), {"I5b-green-then-empty-is-one"}),
    "unknown-census-retries": (("JEZ HALT", "JEZ WATCH", 0), {"I6-unknown-census-halts-0"}),
    "always-answers-one": (("HALT:   MOV ACC DOWN", "HALT:   MOV 1 DOWN", 0), {"I5-one-only-after-green-then-empty"}),
    "fix-answers-from-wrong-place": (("MOV LEFT RIGHT", "MOV 1 RIGHT", 0), {"I8-matches-spec"}),
}


def _hunt(text: str, wanted: set[str]) -> set[str]:
    """Run the checker over the case set against `text` until every wanted invariant has fired."""
    nodes = tis.parse(text)
    caught: set[str] = set()
    for path in CASES:
        events, result, error = play(nodes, path)
        caught |= violations(events, result, error, path)
        if wanted <= caught:
            break
    return caught


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutation_is_caught_by_the_named_invariant(name) -> None:
    (old, new, nth), wanted = MUTATIONS[name]
    caught = _hunt(mutate(PROGRAM_TEXT, old, new, nth), wanted)
    assert wanted <= caught, f"{name}: wanted {wanted - caught} to fire, saw {caught}"


def test_every_invariant_has_a_mutation_that_trips_it() -> None:
    every = {name for (_, wanted) in MUTATIONS.values() for name in wanted}
    assert every >= {
        "I1-terminates-with-0-or-1", "I2-apply-only-after-green-census", "I3a-at-most-3-repair-reads",
        "I3b-repair-read-follows-echo-of-last-failure", "I4a-repaired-then-census", "I4b-no-repair-halts-0-silently",
        "I4c-spent-budget-halts-0-silently", "I4d-failure-with-budget-left-goes-to-repair",
        "I5-one-only-after-green-then-empty", "I5b-green-then-empty-is-one", "I6-unknown-census-halts-0",
        "I7-update-is-followed-by-census", "I8-matches-spec",
    }


def test_the_checker_is_silent_on_the_unmutated_program() -> None:
    assert _hunt(PROGRAM_TEXT, {"never"}) == set()


def test_the_read_bound_check_fires_on_an_overlong_log() -> None:
    path = (("WATCH", 1), ("APPLY", 0))
    assert "I9-read-bound-2p+11" in violations([("R", "WATCH", 1)] * 12, 1, None, path)


# ── interpreter semantics ───────────────────────────────────────────────────


class Exhausted(Exception):
    pass


class IO:
    def __init__(self, **ports) -> None:
        self.ports = {k: list(v) for k, v in ports.items()}
        self.log: list[tuple] = []

    def read(self, port):
        self.log.append(("r", port))
        if not self.ports.get(port):
            raise Exhausted(port)
        return self.ports[port].pop(0)

    def write(self, port, value):
        self.log.append(("w", port, value))


HEAD = "@N UP=IN DOWN=OUT LEFT=L RIGHT=R\n"


def solo(body, *ins, halt="OUT", head=HEAD):
    """Run one node; returns (halt value or None when the input ran dry, world log, writes to non-halt ports)."""
    world = IO(IN=ins)
    try:
        out = tis.run(tis.parse(head + body), world, halt=halt)
    except Exhausted:
        out = None
    return out, world.log, [w[1:] for w in world.log if w[0] == "w"]


def refused(text, match=None):
    with pytest.raises(ValueError, match=match):
        tis.parse(text)


# parsing

def test_a_bare_label_and_a_label_with_an_instruction_name_the_same_instruction() -> None:
    bare = tis.parse("@N UP=IN\nA:\nNOP\nJMP A")["N"]
    inline = tis.parse("@N UP=IN\nA: NOP\nJMP A")["N"]
    glued = tis.parse("@N UP=IN\nA:NOP\nJMP A")["N"]
    assert bare.labels == inline.labels == glued.labels == {"A": 0}
    assert bare.code == inline.code == glued.code


def test_blank_comment_and_label_only_lines_are_not_instructions() -> None:
    node = tis.parse("@N UP=IN DOWN=OUT\n\n# only a comment\nA:\n\nMOV 1 ACC # tail\n   \nB: # label then comment\nMOV ACC DOWN\n")["N"]
    assert node.code == [("MOV", "1", "ACC"), ("MOV", "ACC", "DOWN")]
    assert node.labels == {"A": 0, "B": 1}


def test_fifteen_instructions_fit_and_sixteen_do_not() -> None:
    assert len(tis.parse("@N UP=IN\n" + "NOP\n" * 15)["N"].code) == 15
    refused("@N UP=IN\n" + "NOP\n" * 16, "split the state")


def test_bare_labels_and_comments_do_not_count_towards_the_cap() -> None:
    """Documents a looseness: the cap counts instructions; bare labels and comments are free."""
    text = "@N UP=IN\n" + "".join(f"L{i}:\n# c\n" for i in range(5)) + "NOP\n" * 15
    assert len(tis.parse(text)["N"].code) == 15


@pytest.mark.parametrize("line", ["MOV 1,ACC", "MOV 1 , ACC", "MOV 1 ,ACC", "MOV\t1\tACC", "MOV   1   ACC"])
def test_commas_and_runs_of_whitespace_separate_operands(line) -> None:
    assert tis.parse(f"@N UP=IN\n{line}")["N"].code == [("MOV", "1", "ACC")]


@pytest.mark.parametrize("line", ["MOV ,1 ACC", "MOV 1,,ACC", "MOV 1 ,, ACC"])
def test_doubled_or_leading_commas_inside_a_line_are_accepted(line) -> None:
    """Documents a looseness: stray and doubled separators collapse instead of being refused."""
    assert tis.parse(f"@N UP=IN\n{line}")["N"].code == [("MOV", "1", "ACC")]


@pytest.mark.parametrize("line", ["MOV 1 ACC,", ",MOV 1 ACC", "MOV 1 ACC extra", "MOV 1"])
def test_stray_commas_and_wrong_arity_are_refused(line) -> None:
    refused(f"@N UP=IN\n{line}", "bad instruction")


@pytest.mark.parametrize("line", ["mov 1 acc", "Mov 1 ACC", "MOV 1 acc", "mov 1 ACC"])
def test_lowercase_is_refused(line) -> None:
    refused(f"@N UP=IN\n{line}")


def test_labels_are_case_sensitive() -> None:
    refused("@N UP=IN\nJMP a\nA: NOP", "unknown label")


@pytest.mark.parametrize("operand", ["-5", "0", "-0", "999", "-999", "007"])
def test_integer_literals_including_negative_parse(operand) -> None:
    assert tis.parse(f"@N UP=IN\nMOV {operand} ACC")["N"].code == [("MOV", operand, "ACC")]


@pytest.mark.parametrize("operand", ["+1", "1.5", "0x10", "1_0", "--1", "- 1", "ANY", "LAST", "any", "UP:", "a1"])
def test_other_operands_are_refused(operand) -> None:
    refused(f"@N UP=IN\nMOV {operand} ACC")


def test_any_and_last_are_not_supported_as_destinations_either() -> None:
    refused("@N UP=IN\nMOV 1 ANY", "bad operand")
    refused("@N UP=IN\nMOV 1 LAST", "bad operand")


def test_mov_into_a_literal_and_unwired_ports_are_refused() -> None:
    refused("@N UP=IN\nMOV 1 2", "literal")
    refused("@N UP=IN\nMOV ACC DOWN", "not wired")
    refused("@N UP=IN\nMOV LEFT ACC", "not wired")
    refused("@N UP=IN\nADD RIGHT", "not wired")
    refused("@N UP=IN\nSUB DOWN", "not wired")
    refused("@N UP=IN\nJRO LEFT", "not wired")


@pytest.mark.parametrize("line", ["NOP 1", "SWP ACC", "SAV ACC", "NEG ACC", "ADD", "SUB", "JRO", "JMP", "JEZ", "MOV", "NOP NOP", "HCF", "JRO A B"])
def test_wrong_arity_and_unknown_opcodes_are_refused(line) -> None:
    refused(f"@N UP=IN\nA: NOP\n{line}", "bad instruction")


@pytest.mark.parametrize("op", ["JMP", "JEZ", "JNZ", "JGZ", "JLZ"])
def test_jump_to_unknown_label_is_refused(op) -> None:
    refused(f"@N UP=IN\n{op} NOWHERE", "unknown label")


def test_jro_takes_numbers_not_labels() -> None:
    refused("@N UP=IN\nJRO A\nA: NOP", "bad operand")


def test_empty_node_and_instruction_outside_a_node_are_refused() -> None:
    refused("@N UP=IN\n", "empty node")
    refused("@N UP=IN\nA:\n# nothing\n", "empty node")
    refused("NOP", "outside a node")
    refused("A: NOP", "outside a node")


def test_duplicate_node_and_duplicate_label_are_refused_even_when_bare() -> None:
    refused("@N UP=IN\nNOP\n@N UP=IN\nNOP", "defined twice")
    refused("@N UP=IN\nA:\nNOP\nA:\nNOP", "defined twice")
    refused("@N UP=IN\nA: NOP\nA: NOP", "defined twice")
    tis.parse("@N UP=IN\nA: NOP\n@M UP=IN\nA: NOP")  # the same label in two nodes is fine


def test_two_labels_on_one_line_are_refused() -> None:
    refused("@N UP=IN\nA: B: NOP", "bad instruction")
    refused("@N UP=IN\nA:B: NOP", "bad instruction")


def test_a_label_may_follow_the_last_instruction_and_points_past_the_end() -> None:
    node = tis.parse("@N UP=IN\nJMP END\nEND:")["N"]
    assert node.labels["END"] == len(node.code) == 1


def test_a_label_may_share_a_name_with_a_register_or_port() -> None:
    """Documents: labels named ACC/UP/NIL are accepted, a JMP to them resolves to the label."""
    node = tis.parse("@N UP=IN\nJMP ACC\nACC: NOP")["N"]
    assert node.labels == {"ACC": 1}


def test_header_without_equals_or_name_is_refused_with_a_value_error() -> None:
    refused("@N UP\nNOP")
    refused("@\nNOP")


def test_nodes_come_back_in_source_order() -> None:
    nodes = tis.parse("@Z UP=IN\nNOP\n@A UP=IN\nNOP\n@M UP=IN\nNOP")
    assert list(nodes) == ["Z", "A", "M"]


def test_comment_after_header_is_ignored() -> None:
    assert tis.parse("@N UP=IN DOWN=OUT # wiring\nNOP")["N"].wires == {"UP": "IN", "DOWN": "OUT"}


def test_a_direction_wired_twice_is_refused() -> None:
    refused("@N UP=IN UP=OUT\nNOP")


def test_a_wire_on_a_made_up_direction_is_refused() -> None:
    refused("@N FOO=BAR\nNOP")


@pytest.mark.parametrize("literal", ["1000", "-1000", "99999", "9" * 5000], ids=["1000", "-1000", "99999", "5000-digits"])
def test_an_out_of_range_literal_is_refused_at_parse(literal) -> None:
    refused(f"@N UP=IN\nMOV {literal} ACC")


def test_a_node_wired_to_itself_is_refused() -> None:
    refused("@A UP=A\nMOV 1 UP")


def test_an_unreciprocated_node_wire_is_refused() -> None:
    refused("@A RIGHT=B\nMOV 7 RIGHT\n@B DOWN=OUT\nMOV 5 DOWN")


def test_a_wire_whose_two_ends_use_non_opposite_directions_is_refused() -> None:
    refused("@A RIGHT=B\nMOV 7 RIGHT\n@B UP=A DOWN=OUT\nMOV UP DOWN")


# execution

def test_pc_wraps_after_the_last_instruction() -> None:
    out, log, writes = solo("MOV UP ACC\nADD 1\nMOV ACC RIGHT", 1, 2, 3)
    assert out is None
    assert writes == [("R", 2), ("R", 3), ("R", 4)]


def test_jump_to_a_trailing_label_wraps_to_the_first_instruction() -> None:
    out, _, writes = solo("MOV UP ACC\nJMP END\nMOV 99 RIGHT\nEND:", 5, 6)
    assert out is None and writes == []  # the label points past the end; pc wraps and the loop reads again


@pytest.mark.parametrize(
    "op,acc,taken",
    [(op, acc, want) for op, test in {"JEZ": lambda a: a == 0, "JNZ": lambda a: a != 0, "JGZ": lambda a: a > 0, "JLZ": lambda a: a < 0, "JMP": lambda a: True}.items() for acc, want in [(-1, test(-1)), (0, test(0)), (1, test(1))]],
)
def test_conditional_jumps_follow_the_sign_of_acc(op, acc, taken) -> None:
    out, _, _ = solo(f"MOV {acc} ACC\n{op} T\nMOV 100 DOWN\nT: MOV 200 DOWN")
    assert out == (200 if taken else 100)


def test_a_not_taken_jump_falls_through_to_the_next_instruction_not_the_label() -> None:
    assert solo("MOV 1 ACC\nJEZ T\nMOV 1 DOWN\nT: MOV 2 DOWN")[0] == 1


def test_acc_starts_at_zero_and_bak_starts_at_zero() -> None:
    assert solo("MOV ACC DOWN")[0] == 0
    assert solo("MOV 5 ACC\nSWP\nMOV ACC DOWN")[0] == 0


def test_swp_and_sav() -> None:
    assert solo("MOV 5 ACC\nSAV\nMOV 6 ACC\nSWP\nMOV ACC DOWN")[0] == 5
    assert solo("MOV 5 ACC\nSAV\nMOV 6 ACC\nSWP\nSWP\nMOV ACC DOWN")[0] == 6
    assert solo("MOV 5 ACC\nSWP\nMOV 7 ACC\nSWP\nMOV ACC DOWN")[0] == 5  # SWP alone moves ACC into BAK


def test_sav_copies_and_leaves_acc() -> None:
    assert solo("MOV 5 ACC\nSAV\nMOV ACC DOWN")[0] == 5


def test_neg_and_double_neg() -> None:
    assert solo("MOV 5 ACC\nNEG\nMOV ACC DOWN")[0] == -5
    assert solo("MOV -5 ACC\nNEG\nMOV ACC DOWN")[0] == 5
    assert solo("NEG\nMOV ACC DOWN")[0] == 0


@pytest.mark.parametrize(
    "start,op,arg,want",
    [(999, "ADD", 1, 999), (998, "ADD", 5, 999), (-999, "SUB", 1, -999), (-998, "SUB", 5, -999),
     (10, "ADD", -20, -10), (10, "SUB", 20, -10), (500, "ADD", 499, 999), (-500, "SUB", 499, -999),
     (0, "ADD", 999, 999), (0, "SUB", 999, -999)],
)
def test_add_and_sub_clamp_at_999(start, op, arg, want) -> None:
    assert solo(f"MOV {start} ACC\n{op} {arg}\nMOV ACC DOWN")[0] == want


@pytest.mark.parametrize(
    "start,first,second,want",
    [(999, "ADD 5", "SUB 5", 994), (-999, "SUB 5", "ADD 5", -994), (900, "ADD 900", "SUB 900", 99), (-900, "SUB 900", "ADD 900", -99)],
)
def test_acc_itself_saturates_it_is_not_only_clamped_when_written_out(start, first, second, want) -> None:
    """MOV clamps what it writes, so overflow of ACC itself is only visible through a second operation."""
    assert solo(f"MOV {start} ACC\n{first}\n{second}\nMOV ACC DOWN")[0] == want


def test_add_and_sub_take_a_port_and_acc_and_nil_operands() -> None:
    assert solo("MOV 10 ACC\nADD UP\nMOV ACC DOWN", 5)[0] == 15
    assert solo("MOV 10 ACC\nSUB UP\nMOV ACC DOWN", 5)[0] == 5
    assert solo("MOV 10 ACC\nADD ACC\nMOV ACC DOWN")[0] == 20
    assert solo("MOV 10 ACC\nSUB ACC\nMOV ACC DOWN")[0] == 0
    assert solo("MOV 10 ACC\nADD NIL\nMOV ACC DOWN")[0] == 10
    assert solo("MOV 600 ACC\nADD ACC\nMOV ACC DOWN")[0] == 999


def test_a_port_value_is_clamped_before_it_is_added() -> None:
    assert solo("MOV 10 ACC\nADD UP\nMOV ACC DOWN", 5000)[0] == 999
    assert solo("MOV 10 ACC\nADD UP\nMOV ACC DOWN", -5000)[0] == -989


def test_world_tokens_are_clamped_on_arrival() -> None:
    assert solo("MOV UP DOWN", 12345)[0] == 999
    assert solo("MOV UP DOWN", -12345)[0] == -999


def test_mov_nil_source_zeroes_and_nil_destination_discards() -> None:
    assert solo("MOV 10 ACC\nMOV NIL ACC\nMOV ACC DOWN")[0] == 0
    assert solo("MOV 10 ACC\nMOV ACC NIL\nMOV ACC DOWN")[0] == 10
    assert solo("MOV NIL DOWN")[0] == 0
    assert solo("MOV NIL NIL\nMOV 3 DOWN")[0] == 3


def test_mov_port_to_nil_still_consumes_the_token() -> None:
    out, log, _ = solo("MOV UP NIL\nMOV UP ACC\nMOV ACC DOWN", 1, 2)
    assert out == 2 and log.count(("r", "IN")) == 2


def test_mov_acc_to_acc_is_a_no_op() -> None:
    assert solo("MOV 4 ACC\nMOV ACC ACC\nMOV ACC DOWN")[0] == 4


def test_mov_reads_before_it_writes_and_port_to_port_needs_no_acc() -> None:
    out, log, writes = solo("MOV UP RIGHT\nMOV 0 DOWN", 42)
    assert out == 0
    assert log == [("r", "IN"), ("w", "R", 42)]
    assert solo("MOV UP DOWN", 42)[0] == 42


def test_mov_port_to_port_does_not_touch_acc() -> None:
    assert solo("MOV 7 ACC\nMOV UP RIGHT\nMOV ACC DOWN", 1)[0] == 7


@pytest.mark.parametrize("line", ["MOV 1000 DOWN", "MOV -1000 DOWN", "ADD 5000", "JRO 5000", "MOV " + "9" * 5000 + " ACC"])
def test_a_literal_beyond_999_is_refused_like_in_the_game(line) -> None:
    with pytest.raises(ValueError, match="outside -999..999"):
        tis.parse(f"@N UP=IN DOWN=OUT\n{line}\nMOV ACC DOWN")


def test_arithmetic_still_clamps_at_999() -> None:
    assert solo("MOV 999 ACC\nADD 999\nMOV ACC DOWN")[0] == 999
    assert solo("MOV -999 ACC\nSUB 999\nMOV ACC DOWN")[0] == -999


def test_jro_counts_instructions_not_text_lines() -> None:
    body = "MOV 1 ACC\n\n# comment\nJRO 2\n\nL:\nMOV 100 DOWN\n# gap\nMOV 200 DOWN\nMOV 300 DOWN"
    assert solo(body)[0] == 200


def test_jro_forward_backward_and_one() -> None:
    assert solo("JRO 1\nMOV 4 DOWN")[0] == 4
    assert solo("JRO 2\nMOV 1 DOWN\nMOV 2 DOWN")[0] == 2
    assert solo("MOV UP ACC\nJRO 2\nJRO -1\nMOV ACC DOWN", 9)[0] == 9
    assert solo("JRO 3\nMOV 1 DOWN\nMOV 2 DOWN\nMOV 3 DOWN")[0] == 3  # lands exactly on the last instruction


@pytest.mark.parametrize("offset,want", [(99, 3), (4, 3), (3, 3), (2, 2)])
def test_jro_past_the_end_lands_on_the_last_instruction(offset, want) -> None:
    assert solo(f"JRO {offset}\nMOV 1 DOWN\nMOV 2 DOWN\nMOV 3 DOWN")[0] == want


@pytest.mark.parametrize("offset", [-1, -2, -99])
def test_jro_before_the_start_lands_on_the_first_instruction(offset) -> None:
    out, _, writes = solo(f"MOV UP RIGHT\nJRO {offset}", 1, 2)
    assert out is None and writes == [("R", 1), ("R", 2)]


def test_jro_before_the_start_clamps_to_the_first_not_the_wrapped_last() -> None:
    out, log, _ = solo("MOV UP ACC\nJRO -99", 1, 2)  # first instruction again, not a wrap to the end
    assert out is None and log.count(("r", "IN")) == 3  # two tokens, then the third read finds the script dry


def test_jro_zero_is_an_endless_loop_and_stops_at_the_step_budget() -> None:
    with pytest.raises(RuntimeError, match="step budget"):
        solo("JRO 0")
    with pytest.raises(RuntimeError, match="step budget"):
        solo("MOV 0 ACC\nJRO ACC")
    with pytest.raises(RuntimeError, match="step budget"):
        solo("JRO NIL")


def test_jro_acc_uses_the_current_acc_each_time() -> None:
    body = "MOV UP ACC\nJRO ACC\nMOV 1 DOWN\nMOV 2 DOWN\nMOV 3 DOWN"
    assert solo(body, 1)[0] == 1
    assert solo(body, 2)[0] == 2
    assert solo(body, 3)[0] == 3
    assert solo(body, 50)[0] == 3  # clamped to the last instruction
    out, log, _ = solo(body, -1, 2)  # JRO -1 re-reads, then 2
    assert out == 2 and log.count(("r", "IN")) == 2


def test_jro_with_a_port_operand_reads_the_port() -> None:
    assert solo("JRO UP\nMOV 1 DOWN\nMOV 2 DOWN", 2)[0] == 2


def test_jro_offset_is_clamped_to_the_node() -> None:
    assert solo("JRO 999\nMOV 1 DOWN\nMOV 2 DOWN")[0] == 2


def test_a_one_instruction_node_wraps_onto_itself() -> None:
    out, _, writes = solo("MOV UP RIGHT", 1, 2)
    assert out is None and writes == [("R", 1), ("R", 2)]


# the scheduler

def two(a_body, b_body, a_head="@A RIGHT=B DOWN=OUT UP=X", b_head="@B LEFT=A DOWN=OUT UP=Y RIGHT=Z"):
    return tis.parse(f"{a_head}\n{a_body}\n{b_head}\n{b_body}\n")


def test_values_cross_a_wire_in_order() -> None:
    nodes = two("MOV 1 RIGHT\nMOV 2 RIGHT\nMOV 3 RIGHT", "MOV LEFT UP\nMOV LEFT UP\nMOV LEFT UP\nMOV 0 DOWN")
    world = IO()
    assert tis.run(nodes, world, halt="OUT") == 0
    assert world.log == [("w", "Y", 1), ("w", "Y", 2), ("w", "Y", 3)]


def test_a_writer_is_held_until_the_reader_arrives() -> None:
    nodes = two("MOV 1 RIGHT\nMOV 2 DOWN", "MOV UP ACC\nMOV LEFT NIL")
    world = IO(Y=[0, 0])
    trace = []
    assert tis.run(nodes, world, halt="OUT", trace=lambda s, t, v: trace.append((s, t, v))) == 2
    assert trace.index(("A", "B", 1)) < trace.index(("A", "OUT", 2))


def test_a_write_is_not_delivered_to_a_node_that_is_reading_someone_else() -> None:
    nodes = tis.parse("@A RIGHT=B\nMOV 1 RIGHT\n@B LEFT=A UP=C DOWN=OUT\nMOV UP DOWN\n@C DOWN=B\nMOV 5 DOWN")
    assert tis.run(nodes, IO(), halt="OUT") == 5


def test_two_nodes_that_write_to_each_other_deadlock() -> None:
    nodes = tis.parse("@A RIGHT=B\nMOV 1 RIGHT\n@B LEFT=A\nMOV 2 LEFT")
    with pytest.raises(RuntimeError, match="deadlock"):
        tis.run(nodes, IO(), halt="OUT")


def test_a_node_blocked_on_a_silent_peer_deadlocks_even_if_another_node_is_free() -> None:
    nodes = tis.parse("@A RIGHT=B\nMOV RIGHT ACC\n@B LEFT=A RIGHT=C\nMOV LEFT ACC\n@C LEFT=B\nMOV LEFT ACC")
    with pytest.raises(RuntimeError, match="deadlock"):
        tis.run(nodes, IO(), halt="OUT")


def test_an_empty_grid_deadlocks_instead_of_returning() -> None:
    with pytest.raises(RuntimeError, match="deadlock"):
        tis.run({}, IO(), halt="OUT")


def test_a_node_wired_to_itself_is_refused_before_it_can_deadlock() -> None:
    with pytest.raises(ValueError, match="not returned"):
        tis.parse("@A UP=A DOWN=OUT\nMOV 1 UP\nMOV UP DOWN")


CHAIN = [
    "@C LEFT=B DOWN=OUT\nMOV LEFT ACC\nADD 1\nMOV ACC DOWN",
    "@B LEFT=A RIGHT=C\nMOV LEFT ACC\nADD 10\nMOV ACC RIGHT",
    "@A RIGHT=B UP=IN\nMOV UP ACC\nADD 100\nMOV ACC RIGHT",
]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))), ids=lambda o: "".join("CBA"[i] for i in o))
def test_a_pure_rendezvous_chain_gives_the_same_answer_in_any_declaration_order(order) -> None:
    text = "\n".join(CHAIN[i] for i in order)
    world = IO(IN=[7, 8, 9])
    assert tis.run(tis.parse(text), world, halt="OUT") == 118
    assert world.log[0] == ("r", "IN")


def test_two_nodes_sharing_a_world_port_get_tokens_by_declaration_order() -> None:
    """Documents a hazard: with no clock, who reads the shared port first is decided by source order only."""
    a = "@A UP=IN DOWN=X\nMOV UP DOWN"
    b = "@B UP=IN DOWN=OUT\nMOV UP DOWN"
    first = IO(IN=[10, 20])
    second = IO(IN=[10, 20])
    assert tis.run(tis.parse(a + "\n" + b), first, halt="OUT") == 20
    assert tis.run(tis.parse(b + "\n" + a), second, halt="OUT") == 10


def test_when_two_nodes_could_halt_in_the_same_pass_the_earlier_declared_wins() -> None:
    a = "@A DOWN=OUT\nMOV 1 DOWN"
    b = "@B DOWN=OUT\nMOV 2 DOWN"
    assert tis.run(tis.parse(a + "\n" + b), IO(), halt="OUT") == 1
    assert tis.run(tis.parse(b + "\n" + a), IO(), halt="OUT") == 2


def test_the_halt_token_is_returned_not_written_but_it_is_traced() -> None:
    world = IO()
    trace = []
    assert tis.run(tis.parse("@N DOWN=OUT RIGHT=X\nMOV 4 RIGHT\nMOV 5 DOWN"), world, halt="OUT", trace=lambda *t: trace.append(t)) == 5
    assert world.log == [("w", "X", 4)]
    assert trace == [("N", "X", 4), ("N", "OUT", 5)]


def test_a_read_of_the_halt_port_name_still_asks_the_world() -> None:
    world = IO(OUT=[7])
    assert tis.run(tis.parse("@N DOWN=OUT\nMOV DOWN ACC\nMOV ACC DOWN"), world, halt="OUT") == 7
    assert world.log == [("r", "OUT")]


def test_trace_sees_world_reads_after_clamping_and_every_rendezvous() -> None:
    trace = []
    nodes = two("MOV UP ACC\nMOV ACC RIGHT", "MOV LEFT DOWN", a_head="@A RIGHT=B UP=IN")
    assert tis.run(nodes, IO(IN=[5000]), halt="OUT", trace=lambda *t: trace.append(t)) == 999
    assert trace == [("IN", "A", 999), ("A", "B", 999), ("B", "OUT", 999)]


def test_a_world_read_that_raises_propagates_and_is_not_traced() -> None:
    trace = []
    with pytest.raises(Exhausted):
        tis.run(tis.parse(HEAD + "MOV UP ACC"), IO(), halt="OUT", trace=lambda *t: trace.append(t))
    assert trace == []


def test_a_world_write_that_raises_propagates() -> None:
    class Bad(IO):
        def write(self, port, value):
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        tis.run(tis.parse(HEAD + "MOV 1 RIGHT"), Bad(), halt="OUT")


def test_a_trace_that_raises_stops_the_run() -> None:
    def trace(*_):
        raise KeyError("trace")

    with pytest.raises(KeyError):
        tis.run(tis.parse(HEAD + "MOV 1 DOWN"), IO(), halt="OUT", trace=trace)


def test_a_world_that_raises_stopiteration_is_not_swallowed() -> None:
    class Done(IO):
        def read(self, port):
            raise StopIteration

    with pytest.raises(StopIteration):
        tis.run(tis.parse(HEAD + "MOV UP ACC"), Done(), halt="OUT")


def test_a_runtime_error_from_the_step_budget_names_the_node() -> None:
    with pytest.raises(RuntimeError, match=r"@SPIN: step budget spent"):
        tis.run(tis.parse("@SPIN UP=IN\nA: JMP A"), IO(), halt="OUT")


def test_step_budget_boundary(monkeypatch) -> None:
    monkeypatch.setattr(tis, "STEP_BUDGET", 3)
    assert solo("MOV 1 ACC\nMOV ACC RIGHT\nMOV 5 DOWN")[0] == 5  # exactly three instructions
    with pytest.raises(RuntimeError, match="step budget"):
        solo("MOV 1 ACC\nMOV ACC RIGHT\nNOP\nMOV 5 DOWN")  # the fourth is one too many


def test_step_budget_counts_every_instruction_including_jumps_and_nops(monkeypatch) -> None:
    monkeypatch.setattr(tis, "STEP_BUDGET", 5)
    assert solo("NOP\nNOP\nNOP\nJMP E\nNOP\nE: MOV 1 DOWN")[0] == 1  # NOP NOP NOP JMP MOV: five, the skipped NOP is free
    with pytest.raises(RuntimeError, match="step budget"):
        solo("NOP\nNOP\nNOP\nNOP\nJMP E\nNOP\nE: MOV 1 DOWN")


def test_step_budget_is_per_node_and_cumulative_over_the_whole_run(monkeypatch) -> None:
    """Documents: a slow-but-legitimate node dies at the budget; blocked time is free, executed instructions are not."""
    monkeypatch.setattr(tis, "STEP_BUDGET", 50)
    with pytest.raises(RuntimeError, match="step budget"):
        solo("MOV UP ACC\nMOV ACC RIGHT", *range(100))


def test_a_node_that_never_blocks_is_stopped_while_the_grid_is_being_primed() -> None:
    with pytest.raises(RuntimeError, match="@SPIN: step budget"):
        tis.run(tis.parse("@SPIN UP=IN\nNOP\n@OK UP=IN DOWN=OUT\nMOV 1 DOWN"), IO(), halt="OUT")


def test_two_nodes_that_ping_pong_forever_hit_the_budget_instead_of_hanging() -> None:
    nodes = tis.parse("@A RIGHT=B\nMOV 1 RIGHT\n@B LEFT=A\nMOV LEFT NIL")
    with pytest.raises(RuntimeError, match="step budget"):
        tis.run(nodes, IO(), halt="OUT")


def test_a_world_that_never_lets_the_halt_port_fire_hits_the_budget() -> None:
    class Endless(IO):
        def read(self, port):
            return 1

    with pytest.raises(RuntimeError, match="step budget"):
        tis.run(tis.parse("@N UP=IN DOWN=X\nMOV UP DOWN"), Endless(), halt="OUT")


def test_halt_that_names_a_node_never_fires() -> None:
    """Documents: halt is matched against world ports only; naming a node leaves the run to the budget."""
    with pytest.raises(RuntimeError, match="step budget"):
        tis.run(tis.parse("@A DOWN=OUT\nMOV 1 DOWN"), IO(), halt="A")


def test_a_non_integer_world_token_is_refused() -> None:
    with pytest.raises((TypeError, ValueError)):
        solo("MOV UP ACC\nADD 1\nMOV ACC DOWN", 1.5)


def test_a_none_or_str_world_token_raises() -> None:
    with pytest.raises(TypeError):
        solo("MOV UP DOWN", None)
    with pytest.raises(TypeError):
        solo("MOV UP DOWN", "5")


def test_the_halt_value_is_returned_after_clamping_a_negative_token() -> None:
    assert solo("MOV -5 DOWN")[0] == -5
