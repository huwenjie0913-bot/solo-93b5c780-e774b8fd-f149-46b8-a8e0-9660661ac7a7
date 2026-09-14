from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Optional

from .models import (
    InitialLiquidInput,
    PlateTypeInput,
    ProgramInput,
    StepId,
    StepInput,
    TargetThreshold,
    TipId,
    plate_wells,
)

EPS = 1e-9
GRAPH_INF = 10**9


@dataclass
class Packet:
    lot: str
    component: str
    origin_well: str
    volume: float
    node: int
    history: tuple[dict[str, Any], ...]


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, int] = {}
        self.reverse: dict[int, str] = {}
        self.edges: list[dict[str, Any]] = []
        self.source = self.node("source")

    def node(self, name: str) -> int:
        if name not in self.nodes:
            idx = len(self.nodes)
            self.nodes[name] = idx
            self.reverse[idx] = name
        return self.nodes[name]

    def add_edge(self, u: int, v: int, capacity: float = GRAPH_INF, metadata: Optional[dict[str, Any]] = None) -> None:
        edge = {"u": u, "v": v, "capacity": float(capacity), "metadata": metadata or {}}
        self.edges.append(edge)

    def build_flow_network(self, sink_edges: list[tuple[int, float]], candidate_nodes: set[int]):
        next_id = len(self.nodes) + 1
        mapping: dict[int, int] = {}
        split_out: dict[int, int] = {}

        def mapped(old: int) -> int:
            nonlocal next_id
            if old not in mapping:
                mapping[old] = next_id
                next_id += 1
            return mapping[old]

        # Original nodes plus one output vertex for each unit-capacity candidate,
        # plus the super sink.
        capacity = 2 * len(self.nodes) + len(candidate_nodes) + 1
        adjacency: list[list[dict[str, Any]]] = [[] for _ in range(capacity)]

        source = mapped(self.source)
        sink = 2 * len(self.nodes) + len(candidate_nodes)

        def add_flow_edge(u: int, v: int, cap: float, original: Optional[dict[str, Any]] = None) -> None:
            forward = {"to": v, "rev": len(adjacency[v]), "cap": float(cap), "original": original}
            backward = {"to": u, "rev": len(adjacency[u]), "cap": 0.0, "original": None}
            adjacency[u].append(forward)
            adjacency[v].append(backward)

        for old_node in self.nodes.values():
            vin = mapped(old_node)
            if old_node in candidate_nodes:
                vout = next_id
                next_id += 1
                split_out[old_node] = vout
                adjacency.extend([] for _ in range(vout + 1 - len(adjacency)))
                add_flow_edge(vin, vout, 1.0, {"kind": "candidate_split", "node": old_node})
            else:
                add_flow_edge(vin, vin, GRAPH_INF) if False else None

        # Every non-candidate node has unlimited capacity. A self-edge is not useful,
        # so original edges are remapped directly through candidate vout where needed.
        def out_of(old: int) -> int:
            vin = mapped(old)
            return split_out.get(old, vin)

        def into(old: int) -> int:
            return mapped(old)

        for edge in self.edges:
            add_flow_edge(out_of(edge["u"]), into(edge["v"]), edge["capacity"], edge)
        for arrival_node, weight in sink_edges:
            add_flow_edge(out_of(arrival_node), sink, GRAPH_INF, {"kind": "target_sink"})

        return source, sink, adjacency, mapping


class Dinic:
    def __init__(self, graph: list[list[dict[str, Any]]], source: int, sink: int) -> None:
        self.graph = graph
        self.source = source
        self.sink = sink
        self.level: list[int] = []
        self.ptr: list[int] = []

    def bfs(self) -> bool:
        self.level = [-1] * len(self.graph)
        self.level[self.source] = 0
        queue = deque([self.source])
        while queue:
            u = queue.popleft()
            for edge in self.graph[u]:
                if edge["cap"] > EPS and self.level[edge["to"]] < 0:
                    self.level[edge["to"]] = self.level[u] + 1
                    queue.append(edge["to"])
        return self.level[self.sink] >= 0

    def dfs(self, u: int, pushed: float) -> float:
        if u == self.sink:
            return pushed
        while self.ptr[u] < len(self.graph[u]):
            edge = self.graph[u][self.ptr[u]]
            if edge["cap"] > EPS and self.level[edge["to"]] == self.level[u] + 1:
                flow = self.dfs(edge["to"], min(pushed, edge["cap"]))
                if flow > EPS:
                    edge["cap"] -= flow
                    self.graph[edge["to"]][edge["rev"]]["cap"] += flow
                    return flow
            self.ptr[u] += 1
        return 0.0

    def max_flow(self) -> float:
        total = 0.0
        while self.bfs():
            self.ptr = [0] * len(self.graph)
            while True:
                pushed = self.dfs(self.source, GRAPH_INF)
                if pushed <= EPS:
                    break
                total += pushed
        return total

    def reachable(self) -> set[int]:
        seen = {self.source}
        queue = deque([self.source])
        while queue:
            u = queue.popleft()
            for edge in self.graph[u]:
                if edge["cap"] > EPS and edge["to"] not in seen:
                    seen.add(edge["to"])
                    queue.append(edge["to"])
        return seen


class Simulator:
    def __init__(
        self,
        program: ProgramInput,
        targets: Optional[list[TargetThreshold]] = None,
        interventions: Optional[dict[StepId, str]] = None,
        report_trace: bool = True,
        max_blocker_candidates: int = 40,
    ) -> None:
        self.program = program
        self.targets = list(targets or [])
        self.interventions = interventions or {}
        self.report_trace = report_trace
        self.max_blocker_candidates = max_blocker_candidates

        self.graph = Graph()
        self.wells = plate_wells(program.plate)
        self.well_packets: dict[str, dict[tuple[str, int], Packet]] = {
            well: {} for well in self.wells
        }
        self.tip_packets: dict[str, dict[tuple[str, int], Packet]] = {}
        self.native_components: dict[str, set[str]] = {well: set() for well in self.wells}
        self.findings: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []
        self.cross_events: list[dict[str, Any]] = []
        self.first_cross: dict[tuple[str, str], dict[str, Any]] = {}
        self.candidate_nodes: dict[int, dict[str, Any]] = {}
        self.finding_seq = 0
        self.lot_seq = 0
        self.change_seq = 0

        strategy = program.tip_strategy
        self.active_tip = str(strategy.active_tip_id)
        self.tip_packets.setdefault(self.active_tip, {})
        self.tip_capacity = float(strategy.default_capacity)

    def residual_rate(self, step: Optional[StepInput] = None) -> float:
        if step is not None and step.residual_rate is not None:
            return float(step.residual_rate)
        if self.program.residual_rate is not None:
            return float(self.program.residual_rate)
        return float(self.program.tip_strategy.default_residual_rate)

    def wash_effectiveness(self, step: StepInput) -> float:
        if step.wash_effectiveness is not None:
            return float(step.wash_effectiveness)
        return float(self.program.tip_strategy.default_wash_effectiveness)

    def add_finding(self, **kwargs: Any) -> dict[str, Any]:
        self.finding_seq += 1
        finding = {"finding_id": f"F{self.finding_seq:04d}", **kwargs}
        self.findings.append(finding)
        return finding

    def step_meta(self, step: StepInput, index: int) -> dict[str, Any]:
        return {"step_id": step.id, "step_index": index, "step_type": step.type}

    def put_well_packet(self, well: str, packet: Packet) -> None:
        if packet.volume <= 1e-11:
            return
        key = (packet.lot, packet.node)
        store = self.well_packets[well]
        if key in store:
            store[key].volume += packet.volume
        else:
            store[key] = packet

    def put_tip_packet(self, tip: str, packet: Packet) -> None:
        if packet.volume <= 1e-11:
            return
        key = (packet.lot, packet.node)
        store = self.tip_packets.setdefault(tip, {})
        if key in store:
            store[key].volume += packet.volume
        else:
            store[key] = packet

    def clone_packet(self, packet: Packet, volume: float, node: int, event: dict[str, Any]) -> Packet:
        clean_volume = 0.0 if abs(volume) < 1e-11 else volume
        return Packet(
            lot=packet.lot,
            component=packet.component,
            origin_well=packet.origin_well,
            volume=float(clean_volume),
            node=node,
            history=packet.history + (event,),
        )

    def well_total(self, well: str) -> float:
        return sum(p.volume for p in self.well_packets[well].values())

    def tip_total(self, tip: Optional[str] = None) -> float:
        tip = tip or self.active_tip
        return sum(p.volume for p in self.tip_packets.setdefault(tip, {}).values())

    def component_totals(self, packets: dict[Any, Packet]) -> dict[str, float]:
        totals: dict[str, float] = {}
        for packet in packets.values():
            totals[packet.component] = totals.get(packet.component, 0.0) + packet.volume
        return {name: volume for name, volume in totals.items() if volume > 1e-11}

    def well_snapshot(self, well: str) -> dict[str, Any]:
        return {
            "volume": self.well_total(well),
            "capacity": self.wells[well],
            "components": self.component_totals(self.well_packets[well]),
        }

    def tip_snapshot(self, tip: Optional[str] = None) -> dict[str, Any]:
        tip = tip or self.active_tip
        return {
            "tip_id": tip,
            "volume": self.tip_total(tip),
            "capacity": self.tip_capacity,
            "components": self.component_totals(self.tip_packets.setdefault(tip, {})),
        }

    def initialize(self) -> None:
        for initial in self.program.initial_liquids:
            well = initial.well
            capacity = float(self.wells[well])
            requested = float(initial.volume)
            actual_request = requested
            if requested > capacity + EPS:
                self.add_finding(
                    code="WELL_OVERFLOW",
                    severity="error",
                    message=f"Initial liquid in {well} exceeds well capacity.",
                    phase="initialization",
                    well=well,
                    requested_volume=requested,
                    capacity=capacity,
                    overflow_volume=requested - capacity,
                    basis="initial volume is clamped to well capacity for simulation",
                )
                actual_request = capacity

            if initial.components:
                component_inputs = [(c.name, float(c.volume)) for c in initial.components]
            else:
                component_inputs = [("*", requested)] if requested > EPS else []

            scale = actual_request / requested if requested > EPS else 0.0
            for name, component_volume in component_inputs:
                volume = max(0.0, component_volume * scale)
                if volume <= EPS:
                    continue
                self.lot_seq += 1
                lot = f"L{self.lot_seq:04d}"
                node = self.graph.node(f"well:{well}:lot:{lot}:after:init")
                event = {
                    "kind": "initial",
                    "node": self.graph.reverse[node],
                    "step_id": None,
                    "well": well,
                    "component": name,
                    "volume": volume,
                }
                packet = Packet(lot, name, well, volume, node, (event,))
                self.graph.add_edge(self.graph.source, node, GRAPH_INF, {"kind": "initial_lot"})
                self.put_well_packet(well, packet)
                self.native_components[well].add(name)
                if name not in self.native_components.get("__initial_targets__", set()):
                    pass

        # The simulation graph is built from initial packets and every packet
        # transfer; target threshold evaluation determines the sink edges.

    def all_effective_targets(self) -> list[TargetThreshold]:
        merged = list(self.program.targets) + list(self.targets)
        unique: dict[tuple[str, Optional[str]], TargetThreshold] = {}
        for target in merged:
            unique[(target.well, target.component)] = target
        return list(unique.values())

    def record_cross_contamination(
        self,
        step: StepInput,
        index: int,
        well: str,
        packet: Packet,
        volume: float,
        well_before: float,
        well_after: float,
        mechanism: str,
        cycle: Optional[int] = None,
    ) -> None:
        if volume <= EPS or packet.component in self.native_components[well]:
            return
        fraction = volume / well_after if well_after > EPS else 0.0
        key = (well, packet.component)
        first = key not in self.first_cross
        event = {
            "code": "CROSS_CONTAMINATION",
            "severity": "warning",
            "message": f"{packet.component} from {packet.origin_well} reached {well}.",
            **self.step_meta(step, index),
            "mechanism": mechanism,
            "cycle": cycle,
            "target_well": well,
            "component": packet.component,
            "lot_id": packet.lot,
            "origin_well": packet.origin_well,
            "volume": volume,
            "well_volume_before": well_before,
            "well_volume_after": well_after,
            "fraction_after": fraction,
            "first_occurrence": first,
            "arrival_node": self.graph.reverse[packet.node],
            "propagation_chain": list(packet.history),
        }
        self.cross_events.append(event)
        if first:
            self.first_cross[key] = event

    def remove_proportional(self, store: dict[Any, Packet], amount: float) -> tuple[list[Packet], float]:
        total = sum(p.volume for p in store.values())
        if amount <= EPS or total <= EPS:
            return [], 0.0
        amount = min(amount, total)
        ratio = amount / total
        removed: list[Packet] = []
        empty_keys = []
        for key, packet in list(store.items()):
            part = packet.volume * ratio
            if part <= 1e-11:
                continue
            packet.volume -= part
            if packet.volume <= 1e-10:
                empty_keys.append(key)
            removed.append(
                Packet(
                    packet.lot,
                    packet.component,
                    packet.origin_well,
                    part,
                    packet.node,
                    packet.history,
                )
            )
        for key in empty_keys:
            del store[key]
        return removed, amount

    def clear_tip(self, tip: str) -> None:
        self.tip_packets[tip] = {}

    def select_tip(self, step: StepInput) -> str:
        if step.tip_id is not None:
            self.active_tip = str(step.tip_id)
        self.tip_packets.setdefault(self.active_tip, {})
        return self.active_tip

    def candidate_boundary(self, tip: str, step: StepInput, index: int) -> int:
        node_name = f"tip:{tip}:before-step:{step.id}"
        node = self.graph.node(node_name)
        self.candidate_nodes[node] = {
            "node": node_name,
            "action": "change_tip_or_full_wash",
            "step_id": step.id,
            "step_index": index,
            "before_step_type": step.type,
            "tip_id": tip,
        }
        return node

    def run_aspirate(self, step: StepInput, index: int) -> None:
        tip = self.select_tip(step)
        if step.id in self.interventions:
            self.clear_tip(tip)
        source = step.source or ""
        requested = float(step.volume or 0.0)
        rate = self.residual_rate(step)
        well_before = self.well_total(source)
        tip_before = self.tip_total(tip)
        available_capacity = self.tip_capacity - tip_before
        actual = max(0.0, min(requested, well_before, available_capacity))

        if requested > well_before + EPS:
            self.add_finding(
                code="SOURCE_LIQUID_INSUFFICIENT",
                severity="error",
                message=f"Source well {source} has insufficient liquid for aspirate.",
                **self.step_meta(step, index),
                well=source,
                requested_volume=requested,
                available_volume=well_before,
                actual_volume=actual,
                shortage_volume=requested - well_before,
                basis="actual = min(command volume, source volume, free tip capacity)",
            )
        if tip_before + requested > self.tip_capacity + EPS:
            self.add_finding(
                code="TIP_OVERFILL",
                severity="error",
                message="Aspiration would exceed occupied plus free tip capacity.",
                **self.step_meta(step, index),
                tip_id=tip,
                requested_volume=requested,
                tip_volume_before=tip_before,
                tip_capacity=self.tip_capacity,
                actual_volume=actual,
                basis="actual = min(command volume, source volume, tip capacity - current tip volume)",
            )

        boundary = self.candidate_boundary(tip, step, index)
        new_tip_store: dict[tuple[str, int], Packet] = {}
        old_store = self.tip_packets[tip]
        for packet in list(old_store.values()):
            self.graph.add_edge(packet.node, boundary)
            retain_node = self.graph.node(
                f"tip:{tip}:lot:{packet.lot}:aspirate-carryover:{step.id}"
            )
            event = {
                "kind": "aspirate_carryover",
                "node": self.graph.reverse[retain_node],
                "step_id": step.id,
                "source": source,
                "residual_rate": rate,
            }
            carried = self.clone_packet(packet, packet.volume, retain_node, event)
            self.graph.add_edge(boundary, retain_node)
            new_tip_store[(carried.lot, carried.node)] = carried
        old_store.clear()
        for packet in new_tip_store.values():
            self.put_tip_packet(tip, packet)

        drawn, _ = self.remove_proportional(self.well_packets[source], actual)
        for packet in drawn:
            drawn_node = self.graph.node(f"tip:{tip}:lot:{packet.lot}:aspirated:{step.id}")
            event = {
                "kind": "aspirate",
                "node": self.graph.reverse[drawn_node],
                "step_id": step.id,
                "source": source,
                "requested_volume": requested,
                "actual_volume": packet.volume,
            }
            moved = self.clone_packet(packet, packet.volume, drawn_node, event)
            self.graph.add_edge(packet.node, drawn_node)
            self.put_tip_packet(tip, moved)

        self.trace.append(
            {
                **self.step_meta(step, index),
                "active_tip": self.tip_snapshot(tip),
                "changed_wells": {source: self.well_snapshot(source)},
                "tip_before_volume": tip_before,
                "tip_after_volume": self.tip_total(tip),
                "requested_volume": requested,
                "actual_volume": actual,
                "residual_rate": rate,
            }
        )

    def _dispense_packets(
        self,
        step: StepInput,
        index: int,
        tip: str,
        target: str,
        requested: float,
        rate: float,
        mechanism: str,
        cycle: Optional[int] = None,
        start_node_name: Optional[str] = None,
    ) -> tuple[float, float, float]:
        tip_before = self.tip_total(tip)
        d = min(1.0, requested / tip_before) if tip_before > EPS else 0.0
        start: Optional[int] = None
        if d > EPS:
            start_name = start_node_name or f"tip:{tip}:dispense-start:{step.id}"
            start = self.graph.node(start_name)
            for packet in self.tip_packets[tip].values():
                self.graph.add_edge(packet.node, start)

        delivered_packets: list[Packet] = []
        residual_store: dict[tuple[str, int], Packet] = {}
        old_store = self.tip_packets[tip]
        for packet in list(old_store.values()):
            if d <= EPS:
                residual_store[(packet.lot, packet.node)] = packet
                continue
            delivered_volume = packet.volume * d * (1.0 - rate)
            residual_volume = packet.volume - delivered_volume
            out_node = self.graph.node(
                f"tip:{tip}:lot:{packet.lot}:{mechanism}-out:{step.id}:{cycle or 0}"
            )
            event = {
                "kind": mechanism,
                "node": self.graph.reverse[out_node],
                "step_id": step.id,
                "target_well": target,
                "cycle": cycle,
                "requested_volume": requested,
                "dispense_fraction": d,
                "residual_rate": rate,
            }
            delivered = self.clone_packet(packet, delivered_volume, out_node, event)
            self.graph.add_edge(start, out_node)
            delivered_packets.append(delivered)

            if residual_volume > 1e-11:
                residual_node = self.graph.node(
                    f"tip:{tip}:lot:{packet.lot}:{mechanism}-residual:{step.id}:{cycle or 0}"
                )
                residual_event = {
                    "kind": f"{mechanism}_retained",
                    "node": self.graph.reverse[residual_node],
                    "step_id": step.id,
                    "target_well": target,
                    "cycle": cycle,
                    "residual_rate": rate,
                }
                residual = self.clone_packet(packet, residual_volume, residual_node, residual_event)
                self.graph.add_edge(start, residual_node)
                residual_store[(residual.lot, residual.node)] = residual
        old_store.clear()
        for packet in residual_store.values():
            self.put_tip_packet(tip, packet)

        attempted_delivery = sum(p.volume for p in delivered_packets)
        well_before = self.well_total(target)
        available = self.wells[target] - well_before
        accepted_total = max(0.0, min(attempted_delivery, available))
        if requested > tip_before + EPS:
            self.add_finding(
                code="SOURCE_LIQUID_INSUFFICIENT",
                severity="error",
                message="Tip does not contain enough liquid for the dispense command.",
                **self.step_meta(step, index),
                tip_id=tip,
                requested_volume=requested,
                available_volume=tip_before,
                actual_volume=attempted_delivery,
                shortage_volume=requested - tip_before,
                basis="all available tip contents are dispensed; residual remains according to residual_rate",
            )
        if attempted_delivery > available + EPS:
            self.add_finding(
                code="WELL_OVERFLOW",
                severity="error",
                message=f"Dispense would overflow well {target}.",
                **self.step_meta(step, index),
                well=target,
                attempted_volume=attempted_delivery,
                available_volume=max(0.0, available),
                accepted_volume=accepted_total,
                overflow_volume=attempted_delivery - max(0.0, available),
                cycle=cycle,
                basis="accepted = min(delivered volume, well capacity - current well volume)",
            )

        accept_ratio = accepted_total / attempted_delivery if attempted_delivery > EPS else 0.0
        for packet in delivered_packets:
            accepted_volume = packet.volume * accept_ratio
            if accepted_volume <= EPS:
                continue
            accepted = Packet(
                packet.lot,
                packet.component,
                packet.origin_well,
                accepted_volume,
                packet.node,
                packet.history,
            )
            after_node = self.graph.node(
                f"well:{target}:lot:{packet.lot}:after:{step.id}:{cycle or 0}"
            )
            self.graph.add_edge(packet.node, after_node)
            self.put_well_packet(target, accepted)
            well_after = self.well_total(target)
            self.record_cross_contamination(
                step,
                index,
                target,
                accepted,
                accepted_volume,
                well_before,
                well_after,
                mechanism,
                cycle,
            )
        return attempted_delivery, accepted_total, well_before

    def run_dispense(self, step: StepInput, index: int) -> None:
        tip = self.select_tip(step)
        target = step.target or ""
        requested = float(step.volume or 0.0)
        rate = self.residual_rate(step)
        tip_before = self.tip_total(tip)
        attempted, accepted, _ = self._dispense_packets(
            step, index, tip, target, requested, rate, "dispense"
        )
        if self.program.tip_strategy.mode == "new_per_transfer":
            self.clear_tip(tip)
        self.trace.append(
            {
                **self.step_meta(step, index),
                "active_tip": self.tip_snapshot(tip),
                "changed_wells": {target: self.well_snapshot(target)},
                "tip_before_volume": tip_before,
                "tip_after_volume": self.tip_total(tip),
                "requested_volume": requested,
                "attempted_delivery_volume": attempted,
                "accepted_volume": accepted,
                "residual_rate": rate,
            }
        )

    def run_mix(self, step: StepInput, index: int) -> None:
        tip = self.select_tip(step)
        if step.id in self.interventions:
            self.clear_tip(tip)
        well = step.well or ""
        requested = float(step.volume or 0.0)
        cycles = step.cycles or 1
        rate = self.residual_rate(step)
        boundary = self.candidate_boundary(tip, step, index)
        tip_start_volume = self.tip_total(tip)

        for cycle in range(1, cycles + 1):
            well_before = self.well_total(well)
            tip_before = self.tip_total(tip)
            free = self.tip_capacity - tip_before
            actual = max(0.0, min(requested, well_before, free))
            if requested > well_before + EPS:
                self.add_finding(
                    code="SOURCE_LIQUID_INSUFFICIENT",
                    severity="error",
                    message=f"Mix well {well} has insufficient liquid for cycle {cycle}.",
                    **self.step_meta(step, index),
                    well=well,
                    cycle=cycle,
                    requested_volume=requested,
                    available_volume=well_before,
                    actual_volume=actual,
                    basis="actual = min(mix volume, well volume, free tip capacity)",
                )
            if tip_before + requested > self.tip_capacity + EPS:
                self.add_finding(
                    code="TIP_OVERFILL",
                    severity="error",
                    message="Mix aspiration would exceed tip capacity.",
                    **self.step_meta(step, index),
                    tip_id=tip,
                    cycle=cycle,
                    requested_volume=requested,
                    tip_volume_before=tip_before,
                    actual_volume=actual,
                )

            carried_store: dict[tuple[str, int], Packet] = {}
            old_tip = self.tip_packets[tip]
            for packet in list(old_tip.values()):
                if cycle == 1:
                    self.graph.add_edge(packet.node, boundary)
                    source_node: int = boundary
                else:
                    cycle_start = self.graph.node(
                        f"tip:{tip}:mix-cycle-start:{step.id}:{cycle}"
                    )
                    self.graph.add_edge(packet.node, cycle_start)
                    source_node = cycle_start
                retain_node = self.graph.node(
                    f"tip:{tip}:lot:{packet.lot}:mix-aspirate-carryover:{step.id}:{cycle}"
                )
                event = {
                    "kind": "mix_carryover",
                    "node": self.graph.reverse[retain_node],
                    "step_id": step.id,
                    "well": well,
                    "cycle": cycle,
                }
                carried = self.clone_packet(packet, packet.volume, retain_node, event)
                self.graph.add_edge(source_node, retain_node)
                carried_store[(carried.lot, carried.node)] = carried
            old_tip.clear()
            for packet in carried_store.values():
                self.put_tip_packet(tip, packet)

            drawn, _ = self.remove_proportional(self.well_packets[well], actual)
            for packet in drawn:
                drawn_node = self.graph.node(
                    f"tip:{tip}:lot:{packet.lot}:mix-aspirated:{step.id}:{cycle}"
                )
                event = {
                    "kind": "mix_aspirate",
                    "node": self.graph.reverse[drawn_node],
                    "step_id": step.id,
                    "well": well,
                    "cycle": cycle,
                    "volume": packet.volume,
                }
                moved = self.clone_packet(packet, packet.volume, drawn_node, event)
                self.graph.add_edge(packet.node, drawn_node)
                self.put_tip_packet(tip, moved)

            self._dispense_packets(
                step,
                index,
                tip,
                well,
                actual,
                rate,
                "mix_return",
                cycle,
                start_node_name=f"tip:{tip}:mix-return-start:{step.id}:{cycle}",
            )

        if self.program.tip_strategy.mode == "new_per_transfer":
            self.clear_tip(tip)
        self.trace.append(
            {
                **self.step_meta(step, index),
                "active_tip": self.tip_snapshot(tip),
                "changed_wells": {well: self.well_snapshot(well)},
                "tip_before_volume": tip_start_volume,
                "tip_after_volume": self.tip_total(tip),
                "requested_volume": requested,
                "cycles": cycles,
                "residual_rate": rate,
            }
        )

    def run_wash(self, step: StepInput, index: int) -> None:
        tip = self.select_tip(step)
        effectiveness = self.wash_effectiveness(step)
        before = self.tip_total(tip)
        start = self.graph.node(f"tip:{tip}:wash-start:{step.id}")
        survivors: dict[tuple[str, int], Packet] = {}
        for packet in self.tip_packets[tip].values():
            self.graph.add_edge(packet.node, start)
            remaining = packet.volume * (1.0 - effectiveness)
            if remaining <= 1e-11:
                continue
            survivor_node = self.graph.node(
                f"tip:{tip}:lot:{packet.lot}:wash-survivor:{step.id}"
            )
            event = {
                "kind": "wash",
                "node": self.graph.reverse[survivor_node],
                "step_id": step.id,
                "effectiveness": effectiveness,
                "remaining_fraction": 1.0 - effectiveness,
            }
            survivor = self.clone_packet(packet, remaining, survivor_node, event)
            self.graph.add_edge(start, survivor_node)
            survivors[(survivor.lot, survivor.node)] = survivor
        self.tip_packets[tip] = survivors
        self.trace.append(
            {
                **self.step_meta(step, index),
                "active_tip": self.tip_snapshot(tip),
                "tip_before_volume": before,
                "tip_after_volume": self.tip_total(tip),
                "wash_effectiveness": effectiveness,
                "removed_volume": before - self.tip_total(tip),
            }
        )

    def run_change_tip(self, step: StepInput, index: int) -> None:
        if step.tip_id is not None:
            new_tip = str(step.tip_id)
        else:
            base = str(self.program.tip_strategy.active_tip_id)
            self.change_seq += 1
            new_tip = f"{base}-changed-{self.change_seq}"
        self.active_tip = new_tip
        self.tip_packets[new_tip] = {}
        self.trace.append(
            {
                **self.step_meta(step, index),
                "active_tip": self.tip_snapshot(new_tip),
                "changed_tip": new_tip,
            }
        )

    def evaluate_targets(self, step_id: Optional[StepId] = None, step_index: Optional[int] = None) -> list[dict[str, Any]]:
        snapshots = []
        for target in self.all_effective_targets():
            well = target.well
            volume = self.well_total(well)
            components = self.component_totals(self.well_packets[well])
            native = self.native_components[well]
            foreign = {name: v for name, v in components.items() if name not in native}
            if target.component is None:
                numerator = sum(foreign.values())
                breakdown = foreign
                observed_component = max(foreign, key=foreign.get) if foreign else None
            else:
                numerator = components.get(target.component, 0.0) if target.component not in native else 0.0
                breakdown = {target.component: numerator} if numerator > EPS else {}
                observed_component = target.component if numerator > EPS else None
            fraction = numerator / volume if volume > EPS else 0.0
            snapshots.append(
                {
                    "well": well,
                    "component": target.component,
                    "threshold": target.threshold,
                    "step_id": step_id,
                    "step_index": step_index,
                    "well_volume": volume,
                    "contamination_volume": numerator,
                    "contamination_fraction": fraction,
                    "observed_component": observed_component,
                    "breakdown": breakdown,
                    "qualified": fraction <= target.threshold + EPS,
                    "basis": "contamination_fraction = foreign component volume / total well volume",
                }
            )
        return snapshots

    def run(self) -> dict[str, Any]:
        self.initialize()
        peak_snapshots: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
        threshold_events: list[dict[str, Any]] = []

        def observe(snapshots: list[dict[str, Any]]) -> None:
            for snap in snapshots:
                key = (snap["well"], snap["component"])
                old = peak_snapshots.get(key)
                if old is None or snap["contamination_fraction"] > old["contamination_fraction"]:
                    peak_snapshots[key] = dict(snap)
                if not snap["qualified"]:
                    threshold_events.append(snap)

        observe(self.evaluate_targets(None, None))
        for index, step in enumerate(self.program.steps):
            if step.type == "aspirate":
                self.run_aspirate(step, index)
            elif step.type == "dispense":
                self.run_dispense(step, index)
            elif step.type == "mix":
                self.run_mix(step, index)
            elif step.type == "wash":
                self.run_wash(step, index)
            elif step.type == "change_tip":
                self.run_change_tip(step, index)
            observe(self.evaluate_targets(step.id, index))

        target_results = []
        first_by_step = {}
        for event in threshold_events:
            key = (event["well"], event["component"])
            first_by_step.setdefault(key, event)

        for target in self.all_effective_targets():
            key = (target.well, target.component)
            peak = peak_snapshots.get(key)
            initial = self.evaluate_initial_snapshot(target)
            final_snapshots = [s for s in self.evaluate_targets() if (s["well"], s["component"]) == key]
            final = final_snapshots[0]
            first_threshold = first_by_step.get(key)
            chain = self.chain_for_target(target, peak)
            target_results.append(
                {
                    "well": target.well,
                    "component": target.component,
                    "threshold": target.threshold,
                    "qualified": final["qualified"] and (peak["qualified"] if peak else True),
                    "peak": peak,
                    "initial": initial,
                    "final": final,
                    "first_threshold_violation": first_threshold,
                    "first_contamination": chain["event"],
                    "propagation_chain": chain["chain"],
                    "blockable": chain["blockable"],
                    "calculation_basis": {
                        "threshold_rule": "maximum observed contamination fraction across initial state and every step",
                        "numerator": peak["contamination_volume"] if peak else 0.0,
                        "denominator": peak["well_volume"] if peak else 0.0,
                        "fraction": peak["contamination_fraction"] if peak else 0.0,
                    },
                }
            )

        return {
            "program": self.program.name,
            "status": "completed",
            "summary": self.summary(target_results),
            "findings": self.findings,
            "cross_contamination": {
                "events": self.cross_events,
                "first_by_well_component": list(self.first_cross.values()),
            },
            "target_results": target_results,
            "final_wells": {well: self.well_snapshot(well) for well in sorted(self.wells)},
            "final_tips": {tip: self.tip_snapshot(tip) for tip in sorted(self.tip_packets)},
            "trace": self.trace if self.report_trace else [],
            "metadata": {
                "effective_residual_rate": self.residual_rate(),
                "default_wash_effectiveness": self.program.tip_strategy.default_wash_effectiveness,
                "tip_strategy": self.program.tip_strategy.model_dump(),
                "interventions": self.interventions,
            },
        }

    def evaluate_initial_snapshot(self, target: TargetThreshold) -> dict[str, Any]:
        volume = self.well_total(target.well)
        components = self.component_totals(self.well_packets[target.well])
        native = self.native_components[target.well]
        if target.component is None:
            numerator = sum(v for name, v in components.items() if name not in native)
        else:
            numerator = components.get(target.component, 0.0) if target.component not in native else 0.0
        fraction = numerator / volume if volume > EPS else 0.0
        return {
            "well_volume": volume,
            "contamination_volume": numerator,
            "contamination_fraction": fraction,
            "qualified": fraction <= target.threshold + EPS,
        }

    def chain_for_target(self, target: TargetThreshold, peak: Optional[dict[str, Any]]):
        # Prefer the observed peak component when target component was not specified.
        component = target.component or (peak or {}).get("observed_component")
        if component is None:
            return {"event": None, "chain": [], "blockable": True}
        event = self.first_cross.get((target.well, component))
        if event is not None:
            return {"event": event, "chain": event["propagation_chain"], "blockable": True}
        # Initial foreign component is not removable by a mid-program tip action.
        initial = self.evaluate_initial_snapshot(target)
        if initial["contamination_volume"] > EPS:
            return {
                "event": {"step_id": None, "target_well": target.well, "component": component},
                "chain": [{"kind": "initial_foreign", "well": target.well, "component": component}],
                "blockable": False,
            }
        return {"event": None, "chain": [], "blockable": True}

    def summary(self, target_results: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "runtime_errors": sum(1 for f in self.findings if f["severity"] == "error"),
            "warnings": sum(1 for f in self.findings if f["severity"] == "warning"),
            "cross_contamination_events": len(self.cross_events),
            "targets_qualified": sum(1 for t in target_results if t["qualified"]),
            "targets_total": len(target_results),
            "all_targets_qualified": all(t["qualified"] for t in target_results) if target_results else None,
        }


def _filtered_exposure_edges(sim: Simulator, targets: list[TargetThreshold]) -> tuple[list[tuple[int, float]], bool]:
    edges: list[tuple[int, float]] = []
    unblockable = False
    target_wells = {t.well for t in targets}
    for target in targets:
        snapshot = None
        for candidate in sim.evaluate_targets():
            if candidate["well"] == target.well and candidate["component"] == target.component:
                snapshot = candidate
                break
        if snapshot and snapshot["contamination_volume"] > EPS:
            initial = sim.evaluate_initial_snapshot(target)
            if initial["contamination_volume"] > target.threshold + EPS:
                unblockable = True
        if target.component is None:
            events = [e for e in sim.first_cross.values() if e["target_well"] == target.well]
        else:
            events = [sim.first_cross.get((target.well, target.component))]
        for event in events:
            if event:
                node = sim.graph.nodes[event["arrival_node"]]
                edges.append((node, GRAPH_INF))
    return edges, (unblockable or False)


def find_minimum_blockers(
    program: ProgramInput,
    targets: list[TargetThreshold],
    max_candidates: int = 40,
) -> dict[str, Any]:
    baseline = Simulator(program, targets, report_trace=False).run()
    if all(t["qualified"] for t in baseline["target_results"]):
        return {
            "required_count": 0,
            "actions": [],
            "qualified": True,
            "optimality": "not_needed",
            "calculation_basis": "All targets already satisfy their thresholds.",
        }

    sim = Simulator(program, targets, report_trace=False)
    sim.initialize()
    for index, step in enumerate(program.steps):
        if step.type == "aspirate":
            sim.run_aspirate(step, index)
        elif step.type == "dispense":
            sim.run_dispense(step, index)
        elif step.type == "mix":
            sim.run_mix(step, index)
        elif step.type == "wash":
            sim.run_wash(step, index)
        elif step.type == "change_tip":
            sim.run_change_tip(step, index)

    candidate_items = list(sim.candidate_nodes.items())
    if len(candidate_items) > max_candidates:
        candidate_items = candidate_items[:max_candidates]
    candidate_node_set = {node for node, _ in candidate_items}
    sink_edges, initial_unblockable = _filtered_exposure_edges(sim, targets)
    if initial_unblockable:
        return {
            "required_count": None,
            "actions": [],
            "qualified": False,
            "optimality": "infeasible",
            "calculation_basis": "Contamination is present in the initial target liquid and cannot be removed by a later tip action.",
        }
    if not sink_edges:
        return {
            "required_count": None,
            "actions": [],
            "qualified": False,
            "optimality": "no_propagation_cut",
            "calculation_basis": "Threshold failure has no removable tip propagation path in the model.",
        }

    source, sink, flow_graph, mapping = sim.graph.build_flow_network(sink_edges, candidate_node_set)
    dinic = Dinic(flow_graph, source, sink)
    flow = dinic.max_flow()
    if flow >= GRAPH_INF - 1:
        return {
            "required_count": None,
            "actions": [],
            "qualified": False,
            "optimality": "infeasible",
            "calculation_basis": "The minimum-cut network contains an uncuttable infinite-capacity path.",
        }
    reachable = dinic.reachable()

    strict_cut: list[dict[str, Any]] = []
    for old_node, metadata in candidate_items:
        # The unit-capacity split edge crosses S -> complement exactly when cut.
        mapped_in = mapping[old_node]
        # Candidate vout is the forward neighbour on the stored split edge.
        split_edge = next(
            edge
            for edge in flow_graph[mapped_in]
            if edge.get("original", {}).get("kind") == "candidate_split"
        )
        mapped_out = split_edge["to"]
        if mapped_in in reachable and mapped_out not in reachable:
            strict_cut.append(metadata)

    strict_actions = [
        {
            "before_step_id": item["step_id"],
            "step_index": item["step_index"],
            "tip_id": item["tip_id"],
            "action": "change_tip_or_full_wash",
            "recommended_instruction": {"type": "wash", "tip_id": item["tip_id"], "wash_effectiveness": 1.0},
        }
        for item in strict_cut
    ]
    strict_interventions = {a["before_step_id"]: "clear_tip" for a in strict_actions}
    strict_verified = Simulator(program, targets, strict_interventions, report_trace=False).run()

    # The vertex cut is the minimum number of actions that completely sever every
    # contamination path. For a non-zero threshold, fewer partial interventions may
    # still be enough; enumerate below while the combinatorial cost is small.
    candidate_actions = [
        {
            "before_step_id": metadata["step_id"],
            "step_index": metadata["step_index"],
            "tip_id": metadata["tip_id"],
            "action": "change_tip_or_full_wash",
            "recommended_instruction": {
                "type": "wash",
                "tip_id": metadata["tip_id"],
                "wash_effectiveness": 1.0,
            },
        }
        for _, metadata in candidate_items
    ]
    strict_k = len(strict_actions)
    combo_space = sum(1 for k in range(0, strict_k) for _ in combinations(range(len(candidate_actions)), k))
    threshold_solution = None
    exact_threshold = False
    if strict_k > 1 and combo_space <= 5000:
        exact_threshold = True
        for k in range(0, strict_k):
            for combo in combinations(range(len(candidate_actions)), k):
                interventions = {candidate_actions[i]["before_step_id"]: "clear_tip" for i in combo}
                result = Simulator(program, targets, interventions, report_trace=False).run()
                if all(t["qualified"] for t in result["target_results"]):
                    threshold_solution = ([candidate_actions[i] for i in combo], result)
                    break
            if threshold_solution:
                break

    if threshold_solution is not None:
        actions, verified = threshold_solution
        optimality = "minimum_threshold_compliant"
    else:
        actions, verified = strict_actions, strict_verified
        optimality = "minimum_strict_block" if not exact_threshold else "minimum_strict_block_threshold_exact"

    return {
        "required_count": len(actions),
        "actions": actions,
        "qualified": all(t["qualified"] for t in verified["target_results"]),
        "verified_result_summary": verified["summary"],
        "introduced_runtime_findings": [
            finding
            for finding in verified["findings"]
            if finding not in baseline["findings"]
        ],
        "strict_full_block_count": strict_k,
        "minimum_cut_max_flow": flow,
        "optimality": optimality,
        "calculation_basis": {
            "vertex_cut_rule": "Each candidate boundary is a unit-capacity vertex; other transfers have infinite capacity.",
            "max_flow_equals_min_cut": flow,
            "candidate_count": len(candidate_actions),
            "threshold_combinations_tested": combo_space if strict_k > 1 else 0,
            "note": "A full wash and a fresh tip are equivalent in the model when wash effectiveness is 1.",
        },
    }


def run_review(
    program: ProgramInput,
    targets: Optional[list[TargetThreshold]] = None,
    report_trace: bool = True,
) -> dict[str, Any]:
    return Simulator(program, targets or [], report_trace=report_trace).run()
