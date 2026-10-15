"""Read LuckyPath source layers and prepare a portable, metre-based dataset.

OSM way topology is preserved: geometrically crossing lines are never joined
unless the source ways share a node. Source files are always opened read-only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape
from shapely.ops import polygonize, transform, unary_union

CRS = "EPSG:32618"
TO_METRES = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
TO_LONLAT = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
ALLOW_FOOT = {"yes", "designated", "permissive", "official"}
BLOCK_ACCESS = {"no", "private", "customers", "delivery"}
NO_WALK_HIGHWAYS = {"motorway", "motorway_link", "trunk", "trunk_link"}
NON_ROUTABLE = {"construction", "proposed", "raceway", "bus_guideway", "platform"}


@dataclass(frozen=True)
class Edge:
    id: str
    u: str
    v: str
    coords: list[tuple[float, float]]
    length: float
    osm_tags: dict[str, str]


@dataclass
class Dataset:
    nodes: dict[str, tuple[float, float]] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    places: list[dict[str, Any]] = field(default_factory=list)
    buildings: list[Polygon] = field(default_factory=list)
    major_roads: list[LineString] = field(default_factory=list)
    waterfront: list[Any] = field(default_factory=list)
    entrances: list[tuple[float, float]] = field(default_factory=list)
    trees: list[tuple[float, float]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _tags(element: ET.Element) -> dict[str, str]:
    return {str(t.get("k")): str(t.get("v")) for t in element.findall("tag")}


def _source_info(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _walkable_reason(tags: dict[str, str]) -> str | None:
    """Return a rejection reason; foot-specific permission overrides access."""
    highway = tags.get("highway", "")
    foot = tags.get("foot", "").lower()
    access = tags.get("access", "").lower()
    if not highway:
        return "not_highway"
    if highway in NON_ROUTABLE:
        return "non_routable_highway"
    if tags.get("area") == "yes":
        return "area_boundary"
    if foot in BLOCK_ACCESS:
        return "foot_restricted"
    if access in BLOCK_ACCESS and foot not in ALLOW_FOOT:
        return "access_restricted"
    if highway in NO_WALK_HIGHWAYS and foot not in ALLOW_FOOT:
        return "motor_road"
    if highway in {"cycleway", "bridleway"} and foot not in ALLOW_FOOT:
        return "foot_permission_unspecified"
    return None


def _polygon_parts(geometry: Any) -> list[Polygon]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    return [p for g in getattr(geometry, "geoms", []) for p in _polygon_parts(g)]


def _valid_polygon(geometry: Any) -> list[Polygon]:
    # buffer(0) is used only to repair polygon geometry, never road topology.
    return _polygon_parts(geometry if geometry.is_valid else geometry.buffer(0))


def _read_osm(path: Path, dataset: Dataset) -> None:
    root = ET.parse(path).getroot()
    raw_nodes = {
        str(n.get("id")): (float(n.get("lon")), float(n.get("lat")))
        for n in root.findall("node")
    }
    ways = {str(w.get("id")): w for w in root.findall("way")}
    bounds = root.find("bounds")
    if bounds is not None:
        bbox = [float(bounds.get(k)) for k in ("minlon", "minlat", "maxlon", "maxlat")]
    elif raw_nodes:
        xs, ys = zip(*raw_nodes.values())
        bbox = [min(xs), min(ys), max(xs), max(ys)]
    else:
        raise ValueError("The OSM input contains no coordinate nodes.")
    study_area = box(*bbox)
    projected = {n: TO_METRES.transform(*xy) for n, xy in raw_nodes.items()}
    road_stats: Counter[str] = Counter()

    def refs(way: ET.Element) -> list[str]:
        return [str(nd.get("ref")) for nd in way.findall("nd")]

    for way_id, way in ways.items():
        tags = _tags(way)
        if "highway" not in tags:
            continue
        road_stats["source_highway_ways"] += 1
        # The quietness proxy measures distance to all major roads, including
        # those deliberately excluded from the pedestrian routing graph.
        highway = tags["highway"]
        ids = refs(way)
        if (any(name in highway for name in ("motorway", "trunk", "primary", "secondary", "tertiary"))
                and len(ids) >= 2 and all(n in raw_nodes for n in ids)
                and LineString([raw_nodes[n] for n in ids]).intersects(study_area)):
            dataset.major_roads.append(LineString([projected[n] for n in ids]))
        reason = _walkable_reason(tags)
        if reason:
            road_stats[reason] += 1
            continue
        ids = refs(way)
        if len(ids) < 2:
            road_stats["too_few_nodes"] += 1
            continue
        if any(n not in raw_nodes for n in ids):
            # Never bridge across a missing source node.
            road_stats["missing_node_reference_ways"] += 1
            continue
        if not LineString([raw_nodes[n] for n in ids]).intersects(study_area):
            road_stats["outside_study_bounds"] += 1
            continue
        road_stats["included_highway_ways"] += 1
        if tags.get("oneway:foot") not in (None, "no", "0", "false"):
            road_stats["explicit_foot_direction_ways"] += 1
        for index, (u, v) in enumerate(zip(ids, ids[1:])):
            coords = [projected[u], projected[v]]
            length = math.dist(*coords)
            if u == v or length <= 1e-6:
                road_stats["zero_length_segments"] += 1
                continue
            dataset.nodes[u] = projected[u]
            dataset.nodes[v] = projected[v]
            dataset.edges.append(Edge(
                f"way:{way_id}:{index}", u, v, coords, length,
                {**tags, "osm_way_id": way_id},
            ))

    building_stats: Counter[str] = Counter()
    consumed_ways: set[str] = set()
    for relation in root.findall("relation"):
        tags = _tags(relation)
        if tags.get("building", "no") == "no":
            continue
        building_stats["source_building_relations"] += 1
        if tags.get("type") != "multipolygon":
            building_stats["unsupported_building_relations"] += 1
            continue
        groups: dict[str, list[Any]] = {"outer": [], "inner": []}
        member_ids: set[str] = set()
        incomplete = False
        for member in relation.findall("member"):
            role = member.get("role") or "outer"
            if role not in groups:
                continue
            way_id = str(member.get("ref"))
            way = ways.get(way_id)
            if member.get("type") != "way" or way is None:
                incomplete = True
                continue
            ids = refs(way)
            if len(ids) < 2 or any(n not in projected for n in ids):
                incomplete = True
                continue
            groups[role].append(LineString([projected[n] for n in ids]))
            member_ids.add(way_id)
        if incomplete or not groups["outer"]:
            building_stats["incomplete_building_relations"] += 1
            continue
        outer_polygons = list(polygonize(groups["outer"]))
        if not outer_polygons:
            building_stats["unclosed_building_relations"] += 1
            continue
        geometry = unary_union(outer_polygons)
        if groups["inner"]:
            holes = list(polygonize(groups["inner"]))
            if not holes:
                building_stats["unclosed_building_relations"] += 1
                continue
            geometry = geometry.difference(unary_union(holes))
        if not transform(TO_LONLAT.transform, geometry).intersects(study_area):
            building_stats["outside_building_relations"] += 1
            continue
        parts = _valid_polygon(geometry)
        if not parts:
            building_stats["invalid_building_relations"] += 1
            continue
        dataset.buildings.extend(parts)
        consumed_ways.update(member_ids)
        building_stats["included_building_relations"] += 1

    for way_id, way in ways.items():
        tags = _tags(way)
        if tags.get("building", "no") == "no":
            continue
        building_stats["source_building_ways"] += 1
        if way_id in consumed_ways:
            building_stats["relation_member_ways_deduplicated"] += 1
            continue
        ids = refs(way)
        if len(ids) < 4 or ids[0] != ids[-1] or any(n not in projected for n in ids):
            building_stats["incomplete_building_ways"] += 1
            continue
        if not Polygon([raw_nodes[n] for n in ids]).intersects(study_area):
            building_stats["outside_building_ways"] += 1
            continue
        parts = _valid_polygon(Polygon([projected[n] for n in ids]))
        if not parts:
            building_stats["invalid_building_ways"] += 1
            continue
        dataset.buildings.extend(parts)
        building_stats["included_building_ways"] += 1

    dataset.metadata.update({
        "crs": CRS,
        "coordinate_units": "metres",
        "study_bbox_wgs84": bbox,
        "osm": {
            **_source_info(path),
            "source_nodes": len(raw_nodes), "source_ways": len(ways),
            "source_relations": len(root.findall("relation")),
            "roads": dict(road_stats), "buildings": dict(building_stats),
        },
        "routing_policy": {
            "direction": "undirected walking; motor-vehicle oneway is not applied",
            "topology": "adjacent OSM node references; no geometric intersection joins",
            "foot_permissions": "explicit foot permission overrides generic access",
            "cycleways": "require affirmative foot permission",
            "bounds": "keep complete ways intersecting the OSM study bounds",
        },
        "limitations": [
            "OSM pedestrian access and connectivity have not been verified on site.",
            "Buildings describe mapped footprints, not a complete visibility or enclosure model.",
            "Unresolved/incomplete building features are counted in metadata; absence is not proof of open space.",
            "Relation restrictions and conditional access rules are not evaluated.",
        ],
    })
    if road_stats["explicit_foot_direction_ways"]:
        dataset.metadata["limitations"].append(
            "Explicit foot one-way tags exist; the current graph is undirected."
        )
    if dataset.nodes:
        xs, ys = zip(*dataset.nodes.values())
        dataset.metadata["network_bbox_projected"] = [min(xs), min(ys), max(xs), max(ys)]


def _read_places(path: Path, dataset: Dataset) -> None:
    stats: Counter[str] = Counter()
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            stats["source_rows"] += 1
            try:
                lon, lat = float(row["longitude"]), float(row["latitude"])
                if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                    raise ValueError
                record: dict[str, Any] = dict(row)
                record.update(longitude=lon, latitude=lat)
                record["x"], record["y"] = TO_METRES.transform(lon, lat)
                for key in ("social_component", "access_component"):
                    value = float(row[key])
                    if not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError
                    record[key] = value
            except (KeyError, TypeError, ValueError):
                stats["invalid_rows"] += 1
                continue
            place_id = str(row.get("place_id", ""))
            if place_id and place_id in seen:
                stats["duplicate_ids"] += 1
                continue
            seen.add(place_id)
            dataset.places.append(record)
            stats["included_rows"] += 1
    dataset.metadata["places"] = {**_source_info(path), **stats}


def _read_waterfront(folder: Path, dataset: Dataset) -> None:
    sources = []
    for filename, kind in [
        ("04_All_Publicly_Accessible_Waterfront_WGS84.geojson", "polygons"),
        ("02_WPAA_Official_Access_Points_WGS84.geojson", "entrances"),
    ]:
        path = folder / filename
        if not path.exists():
            sources.append({"file": filename, "missing": True})
            dataset.metadata["limitations"].append(f"Waterfront source layer is missing: {filename}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        stats: Counter[str] = Counter()
        for feature in data.get("features", []):
            stats["source_features"] += 1
            if not feature.get("geometry"):
                stats["missing_geometry"] += 1
                continue
            geometry = transform(TO_METRES.transform, shape(feature["geometry"]))
            if kind == "polygons":
                parts = _valid_polygon(geometry)
                dataset.waterfront.extend(parts)
                stats["included_features"] += bool(parts)
            elif isinstance(geometry, Point) and not geometry.is_empty:
                dataset.entrances.append((geometry.x, geometry.y))
                stats["included_features"] += 1
            else:
                stats["unsupported_geometry"] += 1
        sources.append({**_source_info(path), **stats})
    manifest = folder / "99_manifest.json"
    if manifest.exists():
        value = json.loads(manifest.read_text(encoding="utf-8"))
        dataset.metadata["gh_coordinate_reference"] = {
            key: value[key] for key in ("study_bbox_wgs84", "origin_utm_epsg32618", "translation_vec") if key in value
        }
        sources.append(_source_info(manifest))
    dataset.metadata["waterfront_sources"] = sources


def _read_trees(path: Path | None, dataset: Dataset) -> None:
    if path is None:
        dataset.metadata["trees"] = {"provided": False, "included_rows": 0}
        dataset.metadata["limitations"].append("No tree census was supplied; tree coverage is unavailable.")
        return
    bbox = dataset.metadata["study_bbox_wgs84"]
    minlon, minlat, maxlon, maxlat = bbox
    stats: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            stats["source_rows"] += 1
            try:
                lon, lat = float(row["longitude"]), float(row["latitude"])
                if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                stats["invalid_coordinates"] += 1
                continue
            if not (minlon <= lon <= maxlon and minlat <= lat <= maxlat):
                stats["included_outside_study_bounds"] += 1
            status = str(row.get("status", "")).strip().lower()
            statuses[status or "unspecified"] += 1
            dataset.trees.append(TO_METRES.transform(lon, lat))
            stats["included_rows"] += 1
    dataset.metadata["trees"] = {
        **_source_info(path), "provided": True, **stats,
        "filter": "all source rows with valid coordinates, matching the GH CSV reader; no status or bounds filtering",
        "included_status_counts": dict(statuses),
    }
    dataset.metadata["limitations"].append(
        "The 2015 tree census is historical; GH-compatible counts include non-Alive records and are not current canopy coverage."
    )


def load_dataset(data_dir: Path, tree_csv: Path | None = None, *, osm_path: Path | None = None) -> Dataset:
    """Load the active GH road extract and deduplicated building context.

    The original GH project connects its routes to sibling ``will.xml``. The
    larger west/east extracts supply building context around those routes.
    A stand-alone source folder without will.xml falls back to west/map.osm.
    """
    data_dir = Path(data_dir)
    active_source = data_dir.parent / "will.xml"
    osm_path = Path(osm_path) if osm_path is not None else (
        active_source if active_source.is_file() else data_dir / "west" / "map.osm"
    )
    places_path = data_dir / "places_output_v1_4" / "williamsburg_places_scored_v1_4_full.csv"
    for label, path in (("OSM road data", osm_path), ("places table", places_path)):
        if not path.is_file():
            raise ValueError(f"Required {label} is missing from the supplied data folder.")
    if tree_csv is not None and not Path(tree_csv).is_file():
        raise ValueError("The supplied tree census file does not exist.")
    dataset = Dataset()
    _read_osm(osm_path, dataset)
    context_sources = []
    building_geometries = []
    geometry_keys: set[bytes] = set()
    duplicates = 0
    for label in ("west", "east"):
        context_path = data_dir / label / "map.osm"
        if not context_path.is_file():
            continue
        if context_path.resolve() == osm_path.resolve():
            context = dataset
        else:
            context = Dataset()
            _read_osm(context_path, context)
        context_sources.append({
            **context.metadata["osm"], "role": f"{label} building context",
        })
        for geometry in context.buildings:
            key = geometry.normalize().wkb
            if key in geometry_keys:
                duplicates += 1
                continue
            geometry_keys.add(key)
            building_geometries.append(geometry)
    if context_sources:
        dataset.buildings = building_geometries
        dataset.metadata["building_context_sources"] = context_sources
        dataset.metadata["building_context_duplicates_removed"] = duplicates
    dataset.metadata["routing_policy"]["source_selection"] = (
        "active GH will.xml when present; explicit override or west extract fallback"
    )
    dataset.metadata["limitations"].append(
        "Python keeps individual OSM node-pair segments; its pedestrian filter and segmentation are not an exact GH topology reproduction."
    )
    _read_places(places_path, dataset)
    _read_waterfront(data_dir / "Williamsburg_Waterfront_GH_Ready", dataset)
    _read_trees(Path(tree_csv) if tree_csv is not None else None, dataset)
    dataset.metadata["counts"] = {
        "nodes": len(dataset.nodes), "edges": len(dataset.edges),
        "places": len(dataset.places), "buildings": len(dataset.buildings),
        "major_roads": len(dataset.major_roads),
        "waterfront_polygons": len(dataset.waterfront),
        "waterfront_entrances": len(dataset.entrances), "trees": len(dataset.trees),
    }
    return dataset


def save_dataset(dataset: Dataset, path: Path) -> None:
    """Write a portable JSON snapshot; no source paths or executable objects."""
    value = {
        "format": "luckypath-dataset", "version": 1, "crs": CRS,
        "nodes": dataset.nodes,
        "edges": [
            {"id": e.id, "u": e.u, "v": e.v, "coords": e.coords,
             "length": e.length, "osm_tags": e.osm_tags} for e in dataset.edges
        ],
        "places": dataset.places,
        "buildings": [mapping(g) for g in dataset.buildings],
        "major_roads": [mapping(g) for g in dataset.major_roads],
        "waterfront": [mapping(g) for g in dataset.waterfront],
        "entrances": dataset.entrances, "trees": dataset.trees,
        "metadata": dataset.metadata,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")


def load_prepared_dataset(path: Path) -> Dataset:
    """Load only the versioned JSON format, never pickle or executable input."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "luckypath-dataset" or data.get("version") != 1 or data.get("crs") != CRS:
        raise ValueError("Unsupported prepared dataset format, version, or coordinate system.")
    return Dataset(
        nodes={str(n): tuple(xy) for n, xy in data["nodes"].items()},
        edges=[Edge(str(e["id"]), str(e["u"]), str(e["v"]),
                    [tuple(xy) for xy in e["coords"]], float(e["length"]), dict(e["osm_tags"]))
               for e in data["edges"]],
        places=data["places"],
        buildings=[shape(g) for g in data["buildings"]],
        major_roads=[shape(g) for g in data.get("major_roads", [])],
        waterfront=[shape(g) for g in data["waterfront"]],
        entrances=[tuple(xy) for xy in data["entrances"]],
        trees=[tuple(xy) for xy in data["trees"]],
        metadata=data["metadata"],
    )
