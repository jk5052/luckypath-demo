"""Spatial measurements migrated from the supplied Grasshopper Python components.

All geometry calculations use EPSG:32618 (metres), never longitude/latitude.
Measurements describe the source dataset; they are not live noise, safety, or
accessibility measurements.
"""
from __future__ import annotations

import bisect
import heapq
import math
from collections import defaultdict

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from .calibration import recalibrate_features, validate_calibration, validate_calibration_sources


def minmax(values: list[float], constant: float = 0.5) -> list[float]:
    """Explicit neutral policy for a constant field (GH degenerate domains differ)."""
    if not values:
        return []
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Cannot normalize a non-finite spatial measurement")
    lo, hi = min(values), max(values)
    if hi == lo:
        return [constant] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def percentile(values: list[float]) -> list[float]:
    """Average tie ranks, as in source script_18.py."""
    ordered = sorted(values)
    if len(ordered) <= 1:
        return [0.5] * len(ordered)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Non-finite values cannot be ranked")
    return [
        (bisect.bisect_left(ordered, v) + bisect.bisect_right(ordered, v) - 1)
        / 2 / (len(ordered) - 1)
        for v in values
    ]


def linearity(edges) -> list[float]:
    """Best continuation alignment at each endpoint; source script_10.py."""
    endpoints = defaultdict(list)
    for i, e in enumerate(edges):
        endpoints[e.u].append((i, True))
        endpoints[e.v].append((i, False))

    def tangent(edge, start):
        a, b = (edge.coords[0], edge.coords[1]) if start else (edge.coords[-2], edge.coords[-1])
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(dx, dy)
        return (dx / norm, dy / norm) if norm else (0.0, 0.0)

    def continuation(index, start):
        e = edges[index]
        direction = tangent(e, start)
        if start:
            direction = (-direction[0], -direction[1])
        best = 0.0
        for other, other_start in endpoints[e.u if start else e.v]:
            if other == index:
                continue
            nd = tangent(edges[other], other_start)
            if not other_start:
                nd = (-nd[0], -nd[1])
            best = max(best, min(1.0, direction[0] * nd[0] + direction[1] * nd[1]))
        return best

    return [(continuation(i, True) + continuation(i, False)) / 2 for i in range(len(edges))]


def nearby_aggregates(midpoints, points, values=None, radius=100.0):
    """Circular neighbourhood counts/sums; source scripts 11 and 14."""
    if radius <= 0 or not math.isfinite(radius):
        raise ValueError("Neighbourhood radius must be positive and finite")
    if not points:
        return [0.0] * len(midpoints)
    if values is not None and len(values) != len(points):
        raise ValueError("Point and score lengths differ")
    found = cKDTree(np.asarray(points)).query_ball_point(np.asarray(midpoints), radius)
    return [float(len(ids)) if values is None else sum(values[i] for i in ids) for ids in found]


def _intersection_points(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type in ("MultiPoint", "GeometryCollection"):
        return [p for g in geometry.geoms for p in _intersection_points(g)]
    # GH checked only point intersections, not overlapping curve portions.
    return []


def openness(midpoints, buildings, radius=200.0):
    """Eight horizontal rays to building boundaries; source script_19.py."""
    if not buildings:
        raise ValueError("Building data is required to calculate openness")
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("Openness radius must be positive and finite")
    boundaries = [p.boundary for p in buildings]
    tree = STRtree(boundaries)
    directions = [(math.cos(i * math.pi / 4), math.sin(i * math.pi / 4)) for i in range(8)]
    scores = []
    for xy in midpoints:
        pt = Point(xy)
        distances = []
        for dx, dy in directions:
            ray = LineString([xy, (xy[0] + dx * radius, xy[1] + dy * radius)])
            closest = radius
            for index in tree.query(ray):
                for hit in _intersection_points(ray.intersection(boundaries[int(index)])):
                    distance = pt.distance(hit)
                    if 0.01 < distance < closest:
                        closest = distance
            distances.append(closest)
        scores.append(max(0.0, min(1.0, sum(distances) / (8 * radius))))
    return scores


def major_road_distances(midpoints, edges, major_roads=None):
    """Distance from major road geometry, the source's proxy for stillness."""
    major = ("motorway", "trunk", "primary", "secondary", "tertiary")
    curves = major_roads if major_roads is not None else [LineString(e.coords) for e in edges if any(w in e.osm_tags.get("highway", "") for w in major)]
    if not curves:
        raise ValueError("Major-road geometry is required for the stillness proxy")
    tree = STRtree(curves)
    return [Point(xy).distance(curves[int(tree.nearest(Point(xy)))]) for xy in midpoints]


def waterfront_distances(edges, entrances, max_snap_m=150.0):
    """Multi-source network distance; includes a direct same-edge target path.

    The direct same-edge case repairs an overestimate in source script_08.py,
    which considered endpoint paths only. Unreachable edges are None, not -1.
    """
    if not entrances:
        raise ValueError("Waterfront entrance data is required")
    curves = [LineString(e.coords) for e in edges]
    tree = STRtree(curves)
    graph = defaultdict(list)
    targets = defaultdict(list)
    distance = {}
    queue = []
    for edge in edges:
        graph[edge.u].append((edge.v, edge.length))
        graph[edge.v].append((edge.u, edge.length))
    for xy in entrances:
        point = Point(xy)
        matches = sorted(int(i) for i in tree.query_nearest(point, all_matches=True))
        i = matches[0]
        curve, edge = curves[i], edges[i]
        snap_distance = curve.distance(point)
        if snap_distance > max_snap_m:
            continue
        along = curve.project(point)
        targets[i].append(along)
        for node, value in ((edge.u, along), (edge.v, edge.length - along)):
            if value < distance.get(node, math.inf):
                distance[node] = value
                heapq.heappush(queue, (value, node))
    while queue:
        value, node = heapq.heappop(queue)
        if value > distance[node]:
            continue
        for other, length in graph[node]:
            updated = value + length
            if updated < distance.get(other, math.inf):
                distance[other] = updated
                heapq.heappush(queue, (updated, other))
    result = []
    for i, e in enumerate(edges):
        value = min(distance.get(e.u, math.inf), distance.get(e.v, math.inf)) + e.length / 2
        if targets[i]:
            value = min(value, min(abs(e.length / 2 - t) for t in targets[i]))
        result.append(value if math.isfinite(value) else None)
    return result


def prepare_features(dataset, tree_radius_m=50.0, poi_radius_m=100.0, open_radius_m=200.0,
                     calibration=None):
    """Compute static per-edge attributes once; user weights are applied later."""
    if calibration is not None:
        validate_calibration(calibration, {"tree_radius_m": tree_radius_m,
                                         "poi_radius_m": poi_radius_m,
                                         "open_radius_m": open_radius_m})
        validate_calibration_sources(getattr(dataset, "metadata", {}), calibration)
    if not dataset.edges:
        raise ValueError("The dataset contains no routable edges")
    curves = [LineString(e.coords) for e in dataset.edges]
    midpoints = [tuple(line.interpolate(0.5, normalized=True).coords[0]) for line in curves]
    poi_xy = [(p["x"], p["y"]) for p in dataset.places]
    tree_counts = nearby_aggregates(midpoints, dataset.trees, radius=tree_radius_m)
    raw_social = nearby_aggregates(midpoints, poi_xy, [float(p["social_component"]) for p in dataset.places], poi_radius_m)
    raw_access = nearby_aggregates(midpoints, poi_xy, [float(p["access_component"]) for p in dataset.places], poi_radius_m)
    raw_still = major_road_distances(midpoints, dataset.edges, getattr(dataset, "major_roads", None))
    measurements = {
        "open": openness(midpoints, dataset.buildings, open_radius_m),
        "green": minmax(tree_counts),
        "still": percentile(raw_still),
        "social": minmax(raw_social),
        "access": minmax(raw_access),
        "linearity": linearity(dataset.edges),
        "waterfront_distance_m": waterfront_distances(dataset.edges, dataset.entrances),
        "trees_within_radius": tree_counts,
        "social_raw": raw_social,
        "access_raw": raw_access,
        "major_road_distance_m": raw_still,
    }
    result = {
        "schema_version": 1,
        "profile": "grasshopper-formulas-on-osm-walk-graph-v1",
        "parameters": {
            "tree_radius_m": tree_radius_m, "poi_radius_m": poi_radius_m,
            "open_radius_m": open_radius_m,
            "normalization": "dataset-wide minmax; constant field=0.5",
            "stillness": "average-tie percentile of distance to major roads; not measured noise",
        },
        "edges": {
            edge.id: {"midpoint_xy": list(midpoints[i]), **{key: vals[i] for key, vals in measurements.items()}}
            for i, edge in enumerate(dataset.edges)
        },
    }
    return recalibrate_features(result, calibration) if calibration is not None else result
