"""Prepare once, request routes repeatedly, or run a local web API."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .data import load_dataset, save_dataset
from .engine import Engine
from .features import prepare_features
from .calibration import recalibrate_features, validate_calibration, validate_calibration_sources

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="luckypath", description="Williamsburg spatial scoring and route API")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Prepare portable data from the original project; read-only source")
    prepare.add_argument("--source-data", type=Path, required=True)
    prepare.add_argument("--trees", type=Path)
    prepare.add_argument("--osm", type=Path)
    prepare.add_argument("--output", type=Path, default=DEFAULT_DATA)
    prepare.add_argument("--calibration", type=Path, help="Fixed reference JSON; default retains legacy normalization")
    recalibrate = sub.add_parser("recalibrate", help="Apply a fixed reference to cached raw measurements")
    recalibrate.add_argument("--data", type=Path, default=DEFAULT_DATA)
    recalibrate.add_argument("--calibration", type=Path, required=True)
    route = sub.add_parser("route", help="Calculate a route request JSON")
    route.add_argument("--data", type=Path, default=DEFAULT_DATA)
    route.add_argument("--request", type=Path, required=True)
    route.add_argument("--output", type=Path)
    route.add_argument("--geojson", type=Path)
    server = sub.add_parser("serve", help="Serve the local JSON API")
    server.add_argument("--data", type=Path, default=DEFAULT_DATA)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--allowed-origin", help="Optional exact frontend origin, e.g. http://localhost:3000")
    server.add_argument("--pilot-manifest", type=Path, help="Opt-in local A/B pilot route manifest")
    server.add_argument("--feedback-db", type=Path, help="Opt-in local SQLite response storage; requires --pilot-manifest")
    server.add_argument("--explore", action="store_true", help="Enable the local interactive GIS map; requires prepared map context")
    export = sub.add_parser("feedback-export", help="Export locally collected pilot responses")
    export.add_argument("--feedback-db", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    export.add_argument("--eligible-only", action="store_true", help="Exclude test and incomplete-walk responses")
    summary = sub.add_parser("feedback-summary", help="Describe real completed-walk responses; never invent a quality score")
    summary.add_argument("--feedback-db", type=Path, required=True)
    summary.add_argument("--output", type=Path)
    layer = sub.add_parser("export-layer", help="Export a WGS84 map layer")
    layer.add_argument("name", choices=["roads", "places", "waterfront", "buildings", "trees"])
    layer.add_argument("--data", type=Path, default=DEFAULT_DATA)
    layer.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "feedback-export":
            from .pilot import export_feedback
            print(json.dumps(export_feedback(args.feedback_db, args.output, args.format, args.eligible_only)))
            return
        if args.command == "feedback-summary":
            from .pilot import summarize_feedback
            result = summarize_feedback(args.feedback_db)
            if args.output:
                write_json(args.output, result)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "serve" and bool(args.pilot_manifest) != bool(args.feedback_db):
            raise ValueError("Use --pilot-manifest and --feedback-db together to opt in to collection")
        if args.command == "prepare":
            print("Reading source layers…", flush=True)
            dataset = load_dataset(args.source_data, args.trees, osm_path=args.osm)
            print(f"Measuring {len(dataset.edges)} road segments…", flush=True)
            calibration = json.loads(args.calibration.read_text(encoding="utf-8")) if args.calibration else None
            features = prepare_features(dataset, calibration=calibration)
            args.output.mkdir(parents=True, exist_ok=True)
            save_dataset(dataset, args.output / "dataset.json")
            features["dataset_sha256"] = hashlib.sha256((args.output / "dataset.json").read_bytes()).hexdigest()
            write_json(args.output / "features.json", features)
            print(f"Prepared {len(dataset.edges)} edges and {len(dataset.places)} places in {args.output}")
            return
        engine = Engine(args.data)
        if args.command == "recalibrate":
            calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
            validate_calibration(calibration, engine.features.get("parameters", {}))
            validate_calibration_sources(engine.dataset.metadata, calibration)
            features = recalibrate_features(engine.features, calibration)
            write_json(args.data / "features.json", features)
            print(f"Applied calibration {features['normalization']['id']} to {len(features['edges'])} cached edges")
        elif args.command == "route":
            result = engine.recommend(json.loads(args.request.read_text(encoding="utf-8")))
            if args.output:
                write_json(args.output, result)
            else:
                print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
            if args.geojson:
                write_json(args.geojson, result["geojson"])
            if args.output:
                print(f"Route {result['personalized']['length_m']:.1f} m; shortest {result['shortest']['length_m']:.1f} m. Saved {args.output}")
        elif args.command == "export-layer":
            write_json(args.output, engine.layer(args.name))
            print(f"Saved {args.output}")
        elif args.command == "serve":
            from .server import serve
            pilot = None
            explore = None
            if args.pilot_manifest:
                from .pilot import PilotStore
                pilot = PilotStore(args.pilot_manifest, args.feedback_db)
            if args.explore:
                from .explore import ExploreService
                explore = ExploreService(engine, args.data)
                explore.context()
            serve(engine, args.host, args.port, args.allowed_origin, pilot=pilot, explore=explore)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
