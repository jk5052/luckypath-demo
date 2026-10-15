"""Route along the pedestrian network, with proportional costs at snapped points.

Coordinates in the dataset are EPSG:32618 metres. Public inputs and GeoJSON
outputs use WGS84 longitude, latitude. OSM node identifiers define connectivity;
crossing lines and coincident coordinates alone never create a junction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import heapq
import itertools
import math
from typing import Any

from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import substring


_TO_METRES = Transformer.from_crs(4326, 32618, always_xy=True)
_TO_LONLAT = Transformer.from_crs(32618, 4326, always_xy=True)
_SNAP_EPSILON_M = 1e-6
_ENDPOINT_TOLERANCE_M = 0.05
_BUDGET_TOLERANCE_M = 1e-8
_BUDGET_BRACKET_STEPS = 24
_BUDGET_BISECTION_STEPS = 24


class RoutingError(ValueError):
    """The supplied inputs cannot produce a route on this network."""


class BudgetRouteError(RoutingError):
    """A connected route exists, but its minimum network length exceeds the cap."""

    def __init__(self, min_network_length_m: float, max_network_length_m: float):
        self.min_network_length_m = min_network_length_m
        self.max_network_length_m = max_network_length_m
        super().__init__(
            "The shortest connected walking route exceeds max_network_length_m "
            f"({min_network_length_m:.3f} m > {max_network_length_m:.3f} m)."
        )


@dataclass(frozen=True)
class _Road:
    edge_id: str
    u: tuple
    v: tuple
    line: LineString
    cost: float


@dataclass(frozen=True)
class _Arc:
    target: tuple
    cost: float
    edge_id: str
    coords: tuple
    length: float


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RoutingError(f"{label} must be a finite number.") from exc
    if not math.isfinite(number):
        raise RoutingError(f"{label} must be a finite number.")
    if number < 0 or (positive and number == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise RoutingError(f"{label} must be {qualifier}.")
    return number


def _pair(value: Any, label: str) -> tuple[float, float]:
    try:
        if isinstance(value, (str, bytes, Mapping)) or len(value) != 2:
            raise ValueError
        if any(isinstance(item, bool) for item in value):
            raise ValueError
        x, y = float(value[0]), float(value[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError
        return x, y
    except (TypeError, ValueError, IndexError, OverflowError) as exc:
        raise RoutingError(f"{label} must contain two finite coordinates.") from exc


def _input_point(value: Any, label: str) -> tuple[tuple[float, float], Point]:
    lon, lat = _pair(value, label)
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise RoutingError(f"{label} must be longitude, latitude in WGS84.")
    try:
        x, y = _TO_METRES.transform(lon, lat, errcheck=True)
    except Exception as exc:
        raise RoutingError(f"{label} cannot be projected onto the local map.") from exc
    if not math.isfinite(x) or not math.isfinite(y):
        raise RoutingError(f"{label} cannot be projected onto the local map.")
    return (lon, lat), Point(x, y)


def _roads(dataset: Any, edge_costs: Mapping) -> list[_Road]:
    if not isinstance(edge_costs, Mapping):
        raise RoutingError("edge_costs must map every road ID to its total cost.")
    if not hasattr(dataset, "nodes") or not hasattr(dataset, "edges"):
        raise RoutingError("Dataset must provide nodes and edges.")
    if not isinstance(dataset.nodes, Mapping):
        raise RoutingError("Dataset nodes must be a coordinate mapping.")
    try:
        edges = list(dataset.edges)
    except TypeError as exc:
        raise RoutingError("Dataset edges must be a sequence.") from exc
    if not edges:
        raise RoutingError("The walking network contains no roads.")

    roads = []
    seen_ids = set()
    for edge in edges:
        try:
            edge_id, u, v = edge.id, edge.u, edge.v
            if not all(isinstance(item, str) for item in (edge_id, u, v)):
                raise RoutingError("Road and node IDs must be strings.")
            if edge_id in seen_ids:
                raise RoutingError(f"Duplicate road ID: {edge_id}.")
            seen_ids.add(edge_id)
            if u not in dataset.nodes or v not in dataset.nodes:
                raise RoutingError(f"Road {edge_id} references a missing node.")
            start = _pair(dataset.nodes[u], f"Node {u}")
            end = _pair(dataset.nodes[v], f"Node {v}")
            coords = [_pair(coord, f"Road {edge_id}") for coord in edge.coords]
            if len(coords) < 2:
                raise RoutingError(f"Road {edge_id} needs at least two coordinates.")

            # Preserve node identities, and orient geometry from u to v.
            forward = math.dist(coords[0], start) + math.dist(coords[-1], end)
            backward = math.dist(coords[-1], start) + math.dist(coords[0], end)
            if backward < forward:
                coords.reverse()
            if (math.dist(coords[0], start) > _ENDPOINT_TOLERANCE_M
                    or math.dist(coords[-1], end) > _ENDPOINT_TOLERANCE_M):
                raise RoutingError(f"Road {edge_id} geometry does not meet its nodes.")
            coords[0], coords[-1] = start, end
            line = LineString(coords)
            if not line.is_valid or not math.isfinite(line.length) or line.length <= 0:
                raise RoutingError(f"Road {edge_id} has invalid or zero-length geometry.")
            _number(edge.length, f"Road {edge_id} length", positive=True)
            if edge_id not in edge_costs:
                raise RoutingError(f"Missing cost for road {edge_id}.")
            cost = _number(edge_costs[edge_id], f"Cost for road {edge_id}")
            roads.append(_Road(edge_id, ("node", u), ("node", v), line, cost))
        except (AttributeError, TypeError) as exc:
            raise RoutingError("Each road must provide id, u, v, coords and length.") from exc
    return roads


def _snap(point: Point, roads: list[_Road], label: str, max_snap: float) -> dict:
    # Stable road order resolves equal-distance candidates deterministically.
    road_index = min(range(len(roads)), key=lambda index: roads[index].line.distance(point))
    road = roads[road_index]
    distance_along = float(road.line.project(point))
    if distance_along <= _SNAP_EPSILON_M:
        distance_along = 0.0
    elif road.line.length - distance_along <= _SNAP_EPSILON_M:
        distance_along = float(road.line.length)
    snapped = road.line.interpolate(distance_along)
    distance = float(point.distance(snapped))
    if distance > max_snap + _SNAP_EPSILON_M:
        raise RoutingError(
            f"{label} is {distance:.1f} m from the walking network; "
            f"maximum snap distance is {max_snap:g} m."
        )
    return {"road_index": road_index, "along": distance_along,
            "point": snapped, "distance_m": distance, "edge_id": road.edge_id}


def _graph(roads: list[_Road], snaps: dict) -> dict[tuple, list[_Arc]]:
    graph: dict[tuple, list[_Arc]] = {}
    by_road: dict[int, list[dict]] = {}
    for snap in snaps.values():
        by_road.setdefault(snap["road_index"], []).append(snap)

    for index, road in enumerate(roads):
        full_length = float(road.line.length)
        cuts = [(0.0, road.u), (full_length, road.v)]
        for snap in sorted(by_road.get(index, []), key=lambda item: item["along"]):
            existing = next((cut for cut in cuts
                             if abs(cut[0] - snap["along"]) <= _SNAP_EPSILON_M), None)
            if existing is None:
                existing = (snap["along"], ("snap", road.edge_id, len(cuts)))
                cuts.append(existing)
            snap["along"], snap["node"] = existing
            snap["point"] = road.line.interpolate(existing[0])

        cuts.sort(key=lambda item: item[0])
        for (start, u), (end, v) in zip(cuts, cuts[1:]):
            segment = substring(road.line, start, end)
            coords = tuple(tuple(coord) for coord in segment.coords)
            length = float(segment.length)
            # An input cost describes the entire original road, not a metre.
            cost = road.cost * (length / full_length)
            graph.setdefault(u, []).append(_Arc(v, cost, road.edge_id, coords, length))
            graph.setdefault(v, []).append(
                _Arc(u, cost, road.edge_id, tuple(reversed(coords)), length)
            )
    return graph


def _shortest_path(
    graph: dict, start: tuple, end: tuple, label: str, *, arc_weight=None,
) -> list[_Arc]:
    if start == end:
        return []
    distances = {start: 0.0}
    previous: dict[tuple, tuple[tuple, _Arc]] = {}
    counter = itertools.count()
    queue = [(0.0, next(counter), start)]
    while queue:
        cost, _, node = heapq.heappop(queue)
        if cost > distances[node]:
            continue
        if node == end:
            break
        for arc in graph.get(node, []):
            candidate = cost + (arc.cost if arc_weight is None else arc_weight(arc))
            if not math.isfinite(candidate):
                raise RoutingError("Combined route costs exceed the supported numeric range.")
            if candidate < distances.get(arc.target, math.inf):
                distances[arc.target] = candidate
                previous[arc.target] = (node, arc)
                heapq.heappush(queue, (candidate, next(counter), arc.target))
    if end not in distances:
        raise RoutingError(f"No connected walking route exists for {label}.")
    route = []
    current = end
    while current != start:
        current, arc = previous[current]
        route.append(arc)
    route.reverse()
    return route


def _prepare_graph(dataset, origin_lonlat, destination_lonlat, edge_costs,
                   via_lonlat, max_snap_m):
    max_snap = _number(max_snap_m, "max_snap_m")
    inputs = {"origin": _input_point(origin_lonlat, "Origin")}
    if via_lonlat is not None:
        inputs["via"] = _input_point(via_lonlat, "Via point")
    inputs["destination"] = _input_point(destination_lonlat, "Destination")
    roads = _roads(dataset, edge_costs)
    snaps = {label: _snap(point, roads, label.capitalize(), max_snap)
             for label, (_, point) in inputs.items()}
    graph = _graph(roads, snaps)
    return inputs, snaps, graph


def _path_through(graph, snaps, *, arc_weight=None) -> list[_Arc]:
    path: list[_Arc] = []
    labels = list(snaps)
    for before, after in zip(labels, labels[1:]):
        path.extend(_shortest_path(
            graph, snaps[before]["node"], snaps[after]["node"], f"{before} to {after}",
            arc_weight=arc_weight,
        ))
    return path


def _path_totals(path: list[_Arc]) -> tuple[float, float]:
    try:
        total_cost = float(math.fsum(arc.cost for arc in path))
        length_m = float(math.fsum(arc.length for arc in path))
    except OverflowError as exc:
        raise RoutingError("Combined route totals exceed the supported numeric range.") from exc
    return total_cost, length_m


def _serialize_route(path: list[_Arc], inputs: dict, snaps: dict) -> dict:
    coords = []
    edge_ids = []
    for arc in path:
        # Preserve intermediate vertices and backtracking through a via point.
        if coords and coords[-1] != arc.coords[0]:
            raise RoutingError("The route contains a gap in its road geometry.")
        coords.extend(arc.coords[1:] if coords else arc.coords)
        if not edge_ids or edge_ids[-1] != arc.edge_id:
            edge_ids.append(arc.edge_id)
    if not coords:
        point = snaps["origin"]["point"]
        coords = [(point.x, point.y), (point.x, point.y)]
    route_coords = [[float(lon), float(lat)]
                    for lon, lat in (_TO_LONLAT.transform(x, y) for x, y in coords)]
    total_cost, length_m = _path_totals(path)
    public_snaps = {}
    for label, snap in snaps.items():
        point = snap["point"]
        lon, lat = _TO_LONLAT.transform(point.x, point.y)
        public_snaps[label] = {
            "input_lonlat": list(inputs[label][0]),
            "lonlat": [float(lon), float(lat)],
            "distance_m": float(inputs[label][1].distance(point)),
            "edge_id": snap["edge_id"],
        }
    properties = {"total_cost": total_cost, "length_m": length_m, "edge_ids": edge_ids}
    result = {
        "route": {"type": "Feature", "geometry": {
            "type": "LineString", "coordinates": route_coords,
        }, "properties": properties.copy()},
        **properties,
        "snaps": public_snaps,
        "snapped_origin": public_snaps["origin"]["lonlat"],
        "snapped_destination": public_snaps["destination"]["lonlat"],
    }
    if "via" in public_snaps:
        result["snapped_via"] = public_snaps["via"]["lonlat"]
    return result


def plan_route(
    dataset: Any,
    origin_lonlat: Any,
    destination_lonlat: Any,
    edge_costs: Mapping[str, float],
    via_lonlat: Any = None,
    max_snap_m: float = 150,
) -> dict:
    """Return a minimum-cost, two-way walking route through an optional stop.

    All requested points are snapped and split into one shared graph before
    searching. Costs are apportioned by travelled geometry length. ``length_m``
    measures the network route only, excluding the off-network snap distances.
    Neither the dataset nor the supplied costs are modified.

    Raises RoutingError for invalid inputs, distant points, or disconnected legs.
    No straight-line substitute is returned when a network route is unavailable.
    """
    inputs, snaps, graph = _prepare_graph(
        dataset, origin_lonlat, destination_lonlat, edge_costs, via_lonlat, max_snap_m,
    )
    return _serialize_route(_path_through(graph, snaps), inputs, snaps)


def plan_budget_route(
    dataset: Any,
    origin_lonlat: Any,
    destination_lonlat: Any,
    edge_costs: Mapping[str, float],
    max_network_length_m: float,
    via_lonlat: Any = None,
    max_snap_m: float = 150,
) -> dict:
    """Select a low-cost route whose actual network length fits a single cap.

    The original minimum-cost route, exact minimum-distance route, and bounded
    length-penalty searches form a candidate set. The feasible candidate with
    lowest ORIGINAL cost wins; equal costs prefer the shorter route. This is a
    candidate heuristic, not a globally optimal constrained-path algorithm.
    At most 50 searches through the requested points use one shared snapped
    graph. An optional via consumes the same total budget across both legs.

    The result preserves all ``plan_route`` fields and adds ``budget_selection``
    explaining selection. Costs always use the caller's original edge costs.
    The cap excludes snap connectors, stop time, or any other off-network travel;
    callers must reserve their allowances before converting time to this cap.
    Every result satisfies ``length_m <= max_network_length_m + 1e-8``.

    Raises BudgetRouteError with ``min_network_length_m`` and
    ``max_network_length_m`` if even the exact shortest connected route cannot
    fit. Other invalid inputs, distant snaps, and disconnected legs raise
    RoutingError. Identical endpoints do not generate a walking loop.
    """
    cap = _number(max_network_length_m, "max_network_length_m")
    inputs, snaps, graph = _prepare_graph(
        dataset, origin_lonlat, destination_lonlat, edge_costs, via_lonlat, max_snap_m,
    )

    # Keep original arcs in every path so a search penalty can never leak into
    # serialized costs. Both via legs use the same metric before totals are tested.
    shortest = _path_through(graph, snaps, arc_weight=lambda arc: arc.length)
    _, minimum_length = _path_totals(shortest)
    if minimum_length > cap + _BUDGET_TOLERANCE_M:
        raise BudgetRouteError(minimum_length, cap)

    original = _path_through(graph, snaps)
    _, original_length = _path_totals(original)
    candidates = []
    seen = set()
    searches = 2
    bracket_steps = bisection_steps = 0

    def remember(path, source, penalty=None):
        key = tuple((arc.edge_id, arc.coords) for arc in path)
        totals = _path_totals(path)
        if key not in seen:
            seen.add(key)
            candidates.append({"path": path, "cost": totals[0], "length": totals[1],
                               "source": source, "penalty": penalty})
        return totals[1] <= cap + _BUDGET_TOLERANCE_M

    remember(original, "original_personalized", 0.0)
    remember(shortest, "shortest_distance")

    if original_length > cap + _BUDGET_TOLERANCE_M:
        # Normalize the two metrics separately so bounded searches work across
        # cost units, including tiny costs and all-zero costs. The positive
        # factor 1/(1+penalty) keeps search weights finite without changing their
        # relative order. A recorded penalty is dimensionless, not cost/metre.
        cost_scale = max((arc.cost for arcs in graph.values() for arc in arcs), default=0) or 1.0
        length_scale = max(arc.length for arcs in graph.values() for arc in arcs)

        def search(penalty):
            nonlocal searches
            cost_share = 1.0 / (1.0 + penalty)
            length_share = penalty / (1.0 + penalty)
            path = _path_through(
                graph, snaps,
                arc_weight=lambda arc: (
                    cost_share * (arc.cost / cost_scale)
                    + length_share * (arc.length / length_scale)
                ),
            )
            searches += 1
            return remember(path, "length_penalty", penalty)

        lower, upper = 0.0, 1.0
        bracketed = False
        for _ in range(_BUDGET_BRACKET_STEPS):
            bracket_steps += 1
            if search(upper):
                bracketed = True
                break
            lower, upper = upper, upper * 2.0
        if bracketed:
            for _ in range(_BUDGET_BISECTION_STEPS):
                bisection_steps += 1
                midpoint = (lower + upper) / 2.0
                if search(midpoint):
                    upper = midpoint
                else:
                    lower = midpoint

    feasible = [candidate for candidate in candidates
                if candidate["length"] <= cap + _BUDGET_TOLERANCE_M]
    # The exact shortest route guarantees a feasible fallback. Stable min keeps
    # the original route when both cost and length tie exactly.
    selected = min(feasible, key=lambda candidate: (candidate["cost"], candidate["length"]))
    result = _serialize_route(selected["path"], inputs, snaps)
    if result["length_m"] > cap + _BUDGET_TOLERANCE_M:
        raise RoutingError("The selected walking route exceeds its network length budget.")
    result["budget_selection"] = {
        "method": "bounded_length_penalty_candidates",
        "global_constrained_optimum_guaranteed": False,
        "original_cost_preserved": True,
        "max_network_length_m": cap,
        "min_network_length_m": minimum_length,
        "unconstrained_network_length_m": original_length,
        "selected_candidate": selected["source"],
        "selected_normalized_length_penalty": selected["penalty"],
        "candidate_count": len(candidates),
        "feasible_candidate_count": len(feasible),
        "search_count": searches,
        "search_limit": 2 + _BUDGET_BRACKET_STEPS + _BUDGET_BISECTION_STEPS,
        "bracket_iterations": bracket_steps,
        "bisection_iterations": bisection_steps,
        "length_tolerance_m": _BUDGET_TOLERANCE_M,
        "includes_snap_connectors": False,
        "via_uses_shared_budget": True,
    }
    return result
