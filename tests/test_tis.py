"""control.tis is the engine: these run the shipped program against a scripted world."""

import pytest

from arch_updater import tis
from arch_updater.auto import PROGRAM


class Script:
    """A world whose ports answer from a list; it records every read in order."""

    def __init__(self, census=(), apply=(), repair=()):
        self.answers = {"WATCH": list(census), "APPLY": list(apply), "CENSUS": list(repair), "IN": [5]}
        self.reads: list[str] = []
        self.failures: list[int] = []

    def read(self, port):
        self.reads.append(port)
        return self.answers[port].pop(0)

    def write(self, port, value):
        assert port == "CENSUS"
        self.failures.append(value)


def play(world):
    return tis.run(tis.parse(PROGRAM.read_text(encoding="utf-8")), world, halt="OUT")


def test_every_node_fits_a_tis_node() -> None:
    nodes = tis.parse(PROGRAM.read_text(encoding="utf-8"))
    assert set(nodes) == {"CTL", "FIX"}
    assert all(len(node.code) <= tis.NODE_LINES for node in nodes.values())


def test_sixteenth_instruction_is_refused() -> None:
    with pytest.raises(ValueError, match="split the state"):
        tis.parse("@N UP=IN\n" + "NOP\n" * 16)


def test_green_and_empty_queue_is_idle_without_touching_repair() -> None:
    world = Script(census=[1], apply=[0])
    assert play(world) == 1
    assert world.reads == ["WATCH", "APPLY"]


def test_unknown_census_halts_before_any_update() -> None:
    world = Script(census=[0])
    assert play(world) == 0
    assert world.reads == ["WATCH"]


def test_a_repair_must_be_proven_by_a_census_before_any_update() -> None:
    world = Script(census=[-3, 1, 1], apply=[1, 0], repair=[1])
    assert play(world) == 1
    assert world.failures == [-3]
    assert world.reads == ["WATCH", "CENSUS", "WATCH", "APPLY", "WATCH", "APPLY"]


def test_a_failed_update_goes_through_repair_and_back_to_census() -> None:
    world = Script(census=[1, 1, 1], apply=[-4, 1, 0], repair=[1])
    assert play(world) == 1
    assert world.failures == [-4]
    assert world.reads == ["WATCH", "APPLY", "CENSUS", "WATCH", "APPLY", "WATCH", "APPLY"]


def test_repair_budget_is_three_then_the_lane_answers_no_without_repairing() -> None:
    world = Script(census=[-1, -1, -1, -1], repair=[1, 1, 1])
    assert play(world) == 0
    assert world.reads.count("CENSUS") == 3
    assert "APPLY" not in world.reads


def test_no_known_repair_halts_blocked() -> None:
    world = Script(census=[-2], repair=[0])
    assert play(world) == 0
    assert world.reads == ["WATCH", "CENSUS"]


def test_jro_counts_from_its_own_line() -> None:
    nodes = tis.parse("@N UP=IN DOWN=OUT\nMOV UP ACC\nJRO 2\nMOV 7 DOWN\nMOV ACC DOWN\nMOV 9 DOWN\n")
    assert tis.run(nodes, Script(), halt="OUT") == 5


def test_two_nodes_that_only_read_each_other_deadlock_loudly() -> None:
    nodes = tis.parse("@A RIGHT=B\nMOV RIGHT ACC\n@B LEFT=A\nMOV LEFT ACC\n")
    with pytest.raises(RuntimeError, match="deadlock"):
        tis.run(nodes, Script(), halt="OUT")


def test_a_program_that_names_something_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        tis.parse("@N UP=IN\nA: NOP\nA: NOP\n")
    with pytest.raises(ValueError, match="twice"):
        tis.parse("@N UP=IN\nNOP\n@N UP=IN\nNOP\n")
    with pytest.raises(ValueError, match="not wired"):
        tis.parse("@N UP=IN\nJRO LEFT\n")
