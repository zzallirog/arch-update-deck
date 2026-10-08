"""A register machine small enough to read: nodes, one ACC each, blocking ports.

A port wired to another node is a rendezvous: the writer waits for the reader. A port wired to any
other name belongs to the world: reading it asks the world for a token,
writing hands it one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

NODE_LINES = 15
LIMIT = 999
STEP_BUDGET = 10_000
DIRECTIONS = ("UP", "DOWN", "LEFT", "RIGHT")
FACING = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT", "RIGHT": "LEFT"}
JUMPS: dict[str, Callable[[int], bool]] = {
    "JMP": lambda acc: True,
    "JEZ": lambda acc: acc == 0,
    "JNZ": lambda acc: acc != 0,
    "JGZ": lambda acc: acc > 0,
    "JLZ": lambda acc: acc < 0,
}
ARITY = {"NOP": 0, "SWP": 0, "SAV": 0, "NEG": 0, "ADD": 1, "SUB": 1, "JRO": 1, "MOV": 2, **dict.fromkeys(JUMPS, 1)}
_LABEL = re.compile(r"^(\w+):\s*(.*)$")
_LITERAL = re.compile(r"^-?\d+$")


class World(Protocol):
    def read(self, port: str) -> int: ...

    def write(self, port: str, value: int) -> None: ...


@dataclass
class Node:
    name: str
    wires: dict[str, str]
    code: list[tuple[str, ...]] = field(default_factory=list)
    labels: dict[str, int] = field(default_factory=dict)


def _clamp(value: int) -> int:
    return max(-LIMIT, min(LIMIT, value))


def _check(node: Node) -> None:
    if not node.code:
        raise ValueError(f"@{node.name}: empty node")
    for op, *args in node.code:
        if op not in ARITY or len(args) != ARITY[op]:
            raise ValueError(f"@{node.name}: bad instruction {' '.join((op, *args))}")
        if op in JUMPS and args[0] not in node.labels:
            raise ValueError(f"@{node.name}: unknown label {args[0]}")
        operands = args if op in {"MOV", "ADD", "SUB", "JRO"} else []
        for operand in operands:
            if operand in DIRECTIONS and operand not in node.wires:
                raise ValueError(f"@{node.name}: port {operand} is not wired")
            if operand not in DIRECTIONS and operand not in {"ACC", "NIL"} and not _LITERAL.match(operand):
                raise ValueError(f"@{node.name}: bad operand {operand}")
            if _LITERAL.match(operand) and (len(operand) > 4 or abs(int(operand)) > LIMIT):
                raise ValueError(f"@{node.name}: literal {operand[:8]} is outside -{LIMIT}..{LIMIT}")
        if op == "MOV" and _LITERAL.match(args[1]):
            raise ValueError(f"@{node.name}: MOV into a literal")


def parse(text: str) -> dict[str, Node]:
    nodes: dict[str, Node] = {}
    node: Node | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("@"):
            name, *wiring = line[1:].split()
            if name in nodes:
                raise ValueError(f"@{name}: defined twice")
            node = nodes[name] = Node(name, {})
            for direction, _, peer in (item.partition("=") for item in wiring):
                if direction not in DIRECTIONS or not peer or direction in node.wires:
                    raise ValueError(f"@{name}: bad wire {direction}={peer}")
                node.wires[direction] = peer
            continue
        if node is None:
            raise ValueError(f"instruction outside a node: {line}")
        label = _LABEL.match(line)
        if label:
            if label.group(1) in node.labels:
                raise ValueError(f"@{node.name}: label {label.group(1)} defined twice")
            node.labels[label.group(1)] = len(node.code)
            line = label.group(2)
        if not line:
            continue
        node.code.append(tuple(re.split(r"[\s,]+", line)))
        if len(node.code) > NODE_LINES:
            raise ValueError(f"@{node.name}: more than {NODE_LINES} instructions; split the state into another node")
    for item in nodes.values():
        _check(item)
        # A wire between two nodes must be one wire: the far end returns it from the facing side.
        for direction, peer in item.wires.items():
            if peer in nodes and (peer == item.name or nodes[peer].wires.get(FACING[direction]) != item.name):
                raise ValueError(f"@{item.name}: {direction}={peer} is not returned as {FACING[direction]}={item.name}")
    return nodes


Step = Callable[[str, int, int], None]  # node name, instruction index, ACC before it runs


def _execute(node: Node, step: Step | None = None) -> Iterator[tuple[Any, ...]]:
    """Run one node forever, yielding each port request it blocks on."""
    acc = bak = pc = steps = 0
    size = len(node.code)
    while True:
        steps += 1
        if steps > STEP_BUDGET:
            raise RuntimeError(f"@{node.name}: step budget spent")
        op, *args = node.code[pc]
        here, pc = pc, (pc + 1) % size
        if step:
            step(node.name, here, acc)
        if op in JUMPS:
            if JUMPS[op](acc):
                pc = node.labels[args[0]] % size
            continue
        if op == "SWP":
            acc, bak = bak, acc
            continue
        if op == "SAV":
            bak = acc
            continue
        if op == "NEG":
            acc = -acc
            continue
        if op == "NOP":
            continue
        source = args[0]
        if source in DIRECTIONS:
            value = yield ("read", node.wires[source])
        else:
            value = acc if source == "ACC" else 0 if source == "NIL" else int(source)
        value = _clamp(value)
        if op == "ADD":
            acc = _clamp(acc + value)
        elif op == "SUB":
            acc = _clamp(acc - value)
        elif op == "JRO":
            pc = max(0, min(size - 1, here + value))
        elif args[1] == "ACC":
            acc = value
        elif args[1] in DIRECTIONS:
            yield ("write", node.wires[args[1]], value)


def run(
    nodes: dict[str, Node],
    world: World,
    halt: str,
    trace: Callable[[str, str, int], None] | None = None,
    step: Step | None = None,
) -> int:
    """Step the grid until a node writes to the world port `halt`; return that token."""
    note = trace or (lambda source, target, value: None)
    procs = {name: _execute(node, step) for name, node in nodes.items()}
    wants = {name: next(proc) for name, proc in procs.items()}
    while True:
        moved = False
        for name in nodes:
            kind, peer, *payload = wants[name]
            if peer not in nodes:
                if kind == "read":
                    value = world.read(peer)
                    if type(value) is not int:
                        raise TypeError(f"world port {peer} answered {value!r}; a token is one integer")
                    value = _clamp(value)
                    note(peer, name, value)
                    wants[name] = procs[name].send(value)
                else:
                    note(name, peer, payload[0])
                    if peer == halt:
                        return payload[0]
                    world.write(peer, payload[0])
                    wants[name] = next(procs[name])
                moved = True
            elif kind == "write" and wants[peer][:2] == ("read", name):
                note(name, peer, payload[0])
                wants[peer] = procs[peer].send(payload[0])
                wants[name] = next(procs[name])
                moved = True
        if not moved:
            raise RuntimeError(f"deadlock: {wants}")
