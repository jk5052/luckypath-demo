"""Opt-in local field-pilot collection. No identity, GPS or birth-data collection."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

DIMENSIONS = ("open", "green", "still", "social", "access")
RATINGS = ("quiet", "green", "walkability", "goal_fit", "detour_worth")
WEATHER = ("clear", "cloudy", "rain", "snow", "mixed", "unknown")
TIME_OF_DAY = ("morning", "afternoon", "evening", "night")
ISSUES = ("none", "closed", "inaccessible", "unsafe_crossing", "noise", "missing_place", "map_error", "other")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _text(value, label, maximum, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f"{label} must be text of {minimum}..{maximum} characters")
    return value.strip()


def _uuid(value, label):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError(f"{label} must be a canonical UUID") from None
    return value


def _scores(value):
    if not isinstance(value, dict) or set(value) != set(DIMENSIONS):
        raise ValueError("profile_scores must contain exactly the five dimensions")
    for key, number in value.items():
        if type(number) not in (int, float) or not math.isfinite(number) or not 1 <= number <= 10:
            raise ValueError(f"profile_scores.{key} must be a finite number in 1..10")
    return value


def validate_manifest(value):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Pilot manifest schema_version must be 1")
    _text(value.get("pilot_id"), "pilot_id", 100)
    pairs = value.get("route_pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("Pilot manifest requires route_pairs")
    seen = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("Each route pair must be an object")
        pair_id = _text(pair.get("pair_id"), "pair_id", 100)
        if pair_id in seen:
            raise ValueError("Duplicate pair_id")
        seen.add(pair_id)
        for key in ("neighborhood", "label", "model_version", "calibration_id"):
            _text(pair.get(key), key, 200)
        for key in ("dataset_sha256", "features_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", pair.get(key, "")):
                raise ValueError(f"{key} must be a SHA-256 digest")
        _scores(pair.get("profile_scores"))
        routes = pair.get("routes")
        if not isinstance(routes, dict) or set(routes) != {"shortest", "personalized"}:
            raise ValueError("Each pair needs shortest and personalized routes")
        endpoints = []
        for route in routes.values():
            if not isinstance(route, dict):
                raise ValueError("Route must be an object")
            length = route.get("length_m")
            if type(length) not in (int, float) or not math.isfinite(length) or length <= 0:
                raise ValueError("Route length_m must be positive and finite")
            geom = route.get("geometry", {})
            coords = geom.get("coordinates")
            if geom.get("type") != "LineString" or not isinstance(coords, list) or len(coords) < 2:
                raise ValueError("Route geometry must be a nonempty LineString")
            for point in coords:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("Route coordinates must be [longitude, latitude]")
                if any(type(n) not in (int, float) or not math.isfinite(n) for n in point):
                    raise ValueError("Route coordinates must be finite")
                if not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
                    raise ValueError("Route coordinates must be WGS84")
            endpoints.append((coords[0], coords[-1]))
        if endpoints[0] != endpoints[1]:
            raise ValueError("Compared routes must share endpoints")
        if routes["shortest"]["length_m"] > routes["personalized"]["length_m"] + 1e-6:
            raise ValueError("Shortest route cannot exceed personalized length")
    canonical(value)  # Reject non-finite values, including optional metadata.
    return value


class PilotStore:
    def __init__(self, manifest_path, db_path):
        self.manifest = validate_manifest(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
        self.manifest_sha256 = digest(self.manifest)
        self.pairs = {p["pair_id"]: p for p in self.manifest["route_pairs"]}
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS manifests (
                    sha256 TEXT PRIMARY KEY, manifest_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assignments (
                    assignment_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback (
                    submission_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL,
                    variant TEXT NOT NULL, request_json TEXT NOT NULL,
                    record_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(assignment_id, variant));
            """)
            db.execute("INSERT OR IGNORE INTO manifests VALUES (?,?)", (self.manifest_sha256, canonical(self.manifest)))

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=15)

    def public_manifest(self):
        return {
            "schema_version": 1, "pilot_id": self.manifest["pilot_id"],
            "manifest_sha256": self.manifest_sha256,
            "route_pairs": [{**{k: p[k] for k in ("pair_id", "neighborhood", "label", "profile_scores")},
                             "route_version": digest(p),
                             "identical_geometry": p["routes"]["shortest"]["geometry"] == p["routes"]["personalized"]["geometry"]}
                            for p in self.pairs.values()],
            "collection_status": "Local voluntary pilot; no automatic location or identity collection",
        }

    def assign(self, request):
        required = {"assignment_id", "participant_code", "pair_id", "manifest_sha256", "consent", "initial_intention", "profile_scores"}
        if not isinstance(request, dict) or set(request) != required:
            raise ValueError("Assignment fields must be: " + ", ".join(sorted(required)))
        _uuid(request["assignment_id"], "assignment_id")
        if request["consent"] is not True:
            raise ValueError("Voluntary consent is required before local storage")
        if not isinstance(request["participant_code"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", request["participant_code"]):
            raise ValueError("participant_code must be 3..32 letters, digits, underscores or hyphens; use a pseudonym")
        if request["manifest_sha256"] != self.manifest_sha256:
            raise ValueError("Manifest version changed; reload the pilot")
        pair = self.pairs.get(request["pair_id"])
        if pair is None:
            raise ValueError("Unknown pair_id")
        _text(request["initial_intention"], "initial_intention", 200)
        if _scores(request["profile_scores"]) != pair["profile_scores"]:
            raise ValueError("Confirmed profile differs from these routes; generate a new route pair first")
        encoded = canonical(request)
        # Independent hash bits assign neutral A/B labels and walking order.
        bits = bytes.fromhex(digest([self.manifest["pilot_id"], request["participant_code"], request["pair_id"]]))
        kinds = ["shortest", "personalized"] if bits[0] % 2 == 0 else ["personalized", "shortest"]
        order = ["A", "B"] if bits[1] % 2 == 0 else ["B", "A"]
        result = {"assignment_id": request["assignment_id"], "pair_id": pair["pair_id"],
                  "manifest_sha256": self.manifest_sha256, "route_version": digest(pair),
                  "presented_order": order, "profile_scores": pair["profile_scores"],
                  "routes": {variant: pair["routes"][kind] for variant, kind in zip(("A", "B"), kinds)}}
        stored = {**result, "condition_by_variant": dict(zip(("A", "B"), kinds)),
                  "participant_code": request["participant_code"], "initial_intention": request["initial_intention"],
                  "model_version": pair["model_version"], "calibration_id": pair["calibration_id"],
                  "dataset_sha256": pair["dataset_sha256"], "features_sha256": pair["features_sha256"]}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request_json,response_json FROM assignments WHERE assignment_id=?", (request["assignment_id"],)).fetchone()
            if old:
                if old[0] != encoded:
                    raise ValueError("assignment_id already used for a different request")
                stored = json.loads(old[1])
                return {key: stored[key] for key in result}
            db.execute("INSERT INTO assignments VALUES (?,?,?,?)", (request["assignment_id"], encoded, canonical(stored), datetime.now(timezone.utc).isoformat()))
        return result

    def submit(self, request):
        required = {"submission_id", "assignment_id", "pair_id", "route_variant", "manifest_sha256", "route_version",
                    "consent", "profile_scores", "walk_completed", "is_test", "walk_date", "time_of_day", "weather", "ratings", "issue", "issue_note"}
        if not isinstance(request, dict) or set(request) != required:
            raise ValueError("Feedback fields must be: " + ", ".join(sorted(required)))
        for key in ("submission_id", "assignment_id"):
            _uuid(request[key], key)
        if request["consent"] is not True:
            raise ValueError("Voluntary consent is required")
        for key in ("walk_completed", "is_test"):
            if type(request[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        if request["route_variant"] not in ("A", "B"):
            raise ValueError("route_variant must be A or B")
        for key, choices in (("weather", WEATHER), ("time_of_day", TIME_OF_DAY), ("issue", ISSUES)):
            if request[key] not in choices:
                raise ValueError(f"Invalid {key}")
        try:
            day = date.fromisoformat(request["walk_date"])
            if day.isoformat() != request["walk_date"] or day > date.today():
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("walk_date must be a YYYY-MM-DD date, not in the future") from None
        _text(request["issue_note"], "issue_note", 500, 0)
        ratings = request["ratings"]
        if not isinstance(ratings, dict):
            raise ValueError("ratings must be an object")
        if request["walk_completed"] or ratings:
            if set(ratings) != set(RATINGS):
                raise ValueError("All five ratings are required after a completed walk")
            for key, value in ratings.items():
                if type(value) is not int or not 1 <= value <= 5:
                    raise ValueError(f"ratings.{key} must be an integer in 1..5")
        _scores(request["profile_scores"])
        encoded = canonical(request)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request_json FROM feedback WHERE submission_id=?", (request["submission_id"],)).fetchone()
            if old:
                if old[0] != encoded:
                    raise ValueError("submission_id already used for different feedback")
                return {"ok": True, "submission_id": request["submission_id"], "duplicate": True}
            row = db.execute("SELECT response_json FROM assignments WHERE assignment_id=?", (request["assignment_id"],)).fetchone()
            if not row:
                raise ValueError("Unknown assignment_id; request an assignment before walking")
            assignment = json.loads(row[0])
            for key in ("pair_id", "manifest_sha256", "route_version", "profile_scores"):
                if request[key] != assignment[key]:
                    raise ValueError(f"Feedback {key} does not match the assigned route")
            if request["manifest_sha256"] != self.manifest_sha256:
                raise ValueError("Manifest version changed; do not submit against a different pilot")
            now = datetime.now(timezone.utc).isoformat()
            record = {**request, **{k: assignment[k] for k in ("participant_code", "initial_intention", "presented_order", "model_version", "calibration_id", "dataset_sha256", "features_sha256")},
                      "condition": assignment["condition_by_variant"][request["route_variant"]],
                      "neighborhood": self.pairs[request["pair_id"]]["neighborhood"],
                      "analysis_eligible": request["walk_completed"] and not request["is_test"],
                      "submitted_at": now}
            try:
                db.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?)", (request["submission_id"], request["assignment_id"], request["route_variant"], encoded, canonical(record), now))
            except sqlite3.IntegrityError:
                raise ValueError("This route variant already has feedback for this assignment") from None
        return {"ok": True, "submission_id": request["submission_id"], "duplicate": False,
                "analysis_eligible": record["analysis_eligible"]}


def read_feedback(db_path):
    path = Path(db_path)
    if not path.is_file():
        raise ValueError("Feedback database does not exist; no field feedback collected")
    # Read-only URI prevents accidentally creating or mutating an export source.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT record_json FROM feedback ORDER BY created_at,submission_id")]


def export_feedback(db_path, output, format="jsonl", eligible_only=False):
    records = read_feedback(db_path)
    if eligible_only:
        records = [r for r in records if r["analysis_eligible"]]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        if format == "jsonl":
            for row in records:
                stream.write(canonical(row) + "\n")
        elif format == "csv":
            columns = ["submission_id", "assignment_id", "participant_code", "pair_id", "neighborhood", "route_variant", "condition", "analysis_eligible", "is_test", "walk_completed", "walk_date", "time_of_day", "weather", "initial_intention", "issue", "issue_note", "manifest_sha256", "route_version", "model_version", "calibration_id", "dataset_sha256", "features_sha256", "presented_order", "submitted_at", *[f"profile_{k}" for k in DIMENSIONS], *[f"rating_{k}" for k in RATINGS]]
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for record in records:
                flat = {k: record.get(k, "") for k in columns}
                flat["presented_order"] = ",".join(record["presented_order"])
                flat.update({f"profile_{k}": v for k, v in record["profile_scores"].items()})
                flat.update({f"rating_{k}": v for k, v in record["ratings"].items()})
                # Keep self-entered text inert when CSV is opened in spreadsheets.
                flat = {k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for k, v in flat.items()}
                writer.writerow(flat)
        else:
            raise ValueError("Export format must be jsonl or csv")
    return {"rows": len(records), "eligible_only": eligible_only, "format": format}


def summarize_feedback(db_path):
    records = read_feedback(db_path)
    eligible = [r for r in records if r["analysis_eligible"]]
    groups = {}
    for row in eligible:
        key = (row["manifest_sha256"], row["pair_id"], row["condition"])
        group = groups.setdefault(key, {"manifest_sha256": key[0], "pair_id": key[1], "condition": key[2], "neighborhood": row["neighborhood"], "rows": []})
        group["rows"].append(row)
    comparisons = []
    by_assignment = {}
    for row in eligible:
        by_assignment.setdefault(row["assignment_id"], {})[row["condition"]] = row
    for assignment_id, rows in by_assignment.items():
        if set(rows) == {"shortest", "personalized"}:
            first = rows["shortest"]
            comparisons.append({"assignment_id": assignment_id, "pair_id": first["pair_id"],
                                "manifest_sha256": first["manifest_sha256"],
                                "personalized_minus_shortest": {k: rows["personalized"]["ratings"][k] - first["ratings"][k] for k in RATINGS}})
    summary_groups = []
    for group in groups.values():
        rows = group.pop("rows")
        summary_groups.append({**group, "completed_ratings": len(rows), "participants": len({r["participant_code"] for r in rows}),
                               "rating_means": {key: sum(r["ratings"][key] for r in rows) / len(rows) for key in RATINGS}})
    return {"status": "field_feedback_available" if eligible else "no field feedback collected", "total_submissions": len(records),
            "eligible_submissions": len(eligible), "excluded_test_or_not_walked": len(records) - len(eligible),
            "groups": summary_groups, "complete_pair_comparisons": comparisons,
            "interpretation": "Descriptive voluntary feedback only; no validated experience score or automatic weight updates. Compare within participant and route pair; repeated participants are not independent samples."}


def pilot_html():
    return HTML.encode("utf-8")


HTML = r'''<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>LuckyPath · 산책 파일럿</title>
<style>
:root{font-family:system-ui,sans-serif;color:#183f36;background:#f2f4ec}*{box-sizing:border-box}body{max-width:900px;margin:auto;padding:28px 20px 60px}h1{font-size:32px;margin-bottom:8px}h2{font-size:21px}p{line-height:1.65}.muted{color:#54675e;font-size:14px}.card{background:#fff;border:1px solid #dbe3d6;border-radius:16px;padding:24px;margin:20px 0}label{display:block;margin:15px 0 6px}input:not([type=checkbox]),select,textarea,button{font:inherit;padding:10px;border-radius:7px;border:1px solid #bac9be;max-width:100%}input[type=range]{width:100%;padding:0}button{background:#225c48;color:white;cursor:pointer;margin-top:20px}button:disabled{opacity:.5}fieldset{border:0;padding:0}svg{width:100%;height:300px;background:#f1f5ee;border-radius:10px}#status{white-space:pre-wrap}#status.error{color:#9a3527}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.note{border-left:3px solid #b88034;padding-left:14px}output{float:right}small{display:block}.hidden{display:none}@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style>
<h1>오늘의 산책을 확인해요</h1><p>같은 출발지와 도착지 사이의 두 경로를 걸어 보고, 직접 느낀 경험을 기록하는 소규모 파일럿이에요.</p>
<p class="muted">이 화면은 이 컴퓨터에만 기록해요. 이름·연락처·생년월일·자동 GPS는 받지 않아요. 별명에는 실제 이름을 쓰지 마세요. 자유 입력에도 개인정보를 넣지 마세요.</p>
<div class="card" id="setup"><h2>1. 걷기 전</h2>
<label for="code">참여 코드</label><input id="code" placeholder="예: walk_07" pattern="[A-Za-z0-9_-]{3,32}" maxlength="32"><small class="muted">직접 정한 영문·숫자·_·- 3~32자. 두 경로에 같은 코드를 써요.</small>
<label for="pair">산책 구역</label><select id="pair"></select><p id="same" class="note hidden">이 두 경로는 선형이 같아요. 경로 차이의 비교 효과를 판단할 수 없는 대조 사례예요.</p>
<label for="intention">오늘 원하는 경험</label><input id="intention" maxlength="200" placeholder="예: 조용하고 녹음이 느껴지는 산책" style="width:100%">
<p>이 경로를 계산한 선호 값이에요. 오늘의 내 선호와 맞는지 확인하세요. 1은 낮은 중요도, 10은 높은 중요도예요.</p><div id="sliders"></div>
<p class="muted">사교·접근성 값은 정차 후보 평가에만 쓰여요. 이 파일럿의 두 경로 선형에는 개방감·녹음·고요함만 반영돼요.</p>
<p id="profileWarning" class="note hidden">선호가 달라졌어요. 현재 저장된 경로와 맞지 않으므로 진행자에게 이 값으로 새 경로를 요청하세요.</p>
<label><input type="checkbox" id="consent"> 자발적으로 참여하며 이 컴퓨터에 응답을 저장하는 데 동의해요. 언제든 중단할 수 있어요.</label>
<button id="start" type="button">경로 배정 받기</button></div>
<div id="walk" class="hidden"><div class="card"><h2>2. 배정된 경로</h2><p id="order"></p><p class="note">실제 길을 확인하는 사전 답사가 필요한 경로예요. 아래 그림은 경로 모양을 비교하는 도식이며 길 안내 지도는 아니에요. 통행이 어렵다면 중단하고 기록해 주세요.</p><div class="grid" id="maps"></div><p class="muted">첫 경로를 걷고 바로 평가한 다음, 가능한 한 비슷한 조건에서 다음 경로를 걸어 주세요. A/B는 중립적인 표시예요.</p></div>
<form id="feedback" class="card"><h2>3. 방금 걸은 경험</h2><label for="variant">평가할 경로</label><select id="variant"><option>A</option><option>B</option></select>
<label><input id="completed" type="checkbox"> 이 경로를 실제로 끝까지 걸었어요</label><label><input id="isTest" type="checkbox"> 화면·저장 기능을 확인하는 테스트 응답이에요</label>
<div class="grid"><div><label for="walkDate">산책 날짜</label><input id="walkDate" type="date" required></div><div><label for="time">시간대</label><select id="time"><option value="morning">아침</option><option value="afternoon">낮</option><option value="evening">저녁</option><option value="night">밤</option></select></div></div>
<label for="weather">날씨</label><select id="weather"><option value="unknown">모름</option><option value="clear">맑음</option><option value="cloudy">흐림</option><option value="rain">비</option><option value="snow">눈</option><option value="mixed">변화함</option></select>
<fieldset id="ratings"><p>1 = 전혀 그렇지 않다 · 5 = 매우 그렇다</p></fieldset>
<label for="issue">어려웠던 점</label><select id="issue"><option value="none">없음</option><option value="closed">폐쇄됨</option><option value="inaccessible">통행이 어려움</option><option value="unsafe_crossing">건너기 불편하거나 위험함</option><option value="noise">소음</option><option value="missing_place">예상한 장소가 없음</option><option value="map_error">경로 자료 오류</option><option value="other">기타</option></select>
<label for="note">추가 메모 (선택)</label><textarea id="note" maxlength="500" rows="3" style="width:100%"></textarea><p class="muted">걷지 않았거나 테스트로 표시한 응답은 경험 평가 분석에서 제외해요.</p><button id="save">이 경로의 응답 저장</button></form></div>
<p id="status" role="status" aria-live="polite"></p>
<script>
const $=id=>document.getElementById(id),dims={open:'개방감',green:'녹음',still:'고요함',social:'사교 활동',access:'접근성'},ratingLabels={quiet:'조용하게 느껴졌다',green:'녹음이 느껴졌다',walkability:'걷기 편했다',goal_fit:'오늘 원한 경험과 맞았다',detour_worth:'걸은 거리가 만족할 만했다'};
let manifest,assignment,pair,submitted=new Set(),pending={},attemptId=crypto.randomUUID();
function status(text,error=false){$('status').textContent=text;$('status').className=error?'error':''}
async function api(path,data){const r=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const v=await r.json();if(!r.ok)throw Error(v.error||'요청을 처리하지 못했어요');return v}
function scores(){return Object.fromEntries(Object.keys(dims).map(k=>[k,Number($('score_'+k).value)]))}
function profileMatch(){return Object.keys(dims).every(k=>scores()[k]===pair.profile_scores[k])}
function checkProfile(){$('profileWarning').classList.toggle('hidden',profileMatch());$('start').disabled=!profileMatch()}
function selectPair(){pair=manifest.route_pairs.find(p=>p.pair_id===$('pair').value);$('sliders').replaceChildren();for(const [key,label]of Object.entries(dims)){const l=document.createElement('label');l.htmlFor='score_'+key;l.textContent=label;const out=document.createElement('output');out.textContent=pair.profile_scores[key];l.append(out);const i=document.createElement('input');i.type='range';i.id='score_'+key;i.min=1;i.max=10;i.step=1;i.value=pair.profile_scores[key];i.addEventListener('input',()=>{out.textContent=i.value;checkProfile()});$('sliders').append(l,i)}$('same').classList.toggle('hidden',!pair.identical_geometry);checkProfile()}
function renderMaps(){const all=Object.values(assignment.routes).flatMap(r=>r.geometry.coordinates),cos=Math.cos(all[0][1]*Math.PI/180),xs=all.map(p=>p[0]*cos),ys=all.map(p=>p[1]);const minx=Math.min(...xs),maxx=Math.max(...xs),miny=Math.min(...ys),maxy=Math.max(...ys),span=Math.max(maxx-minx,maxy-miny)||.001,xy=p=>[25+(p[0]*cos-minx)*250/span,275-(p[1]-miny)*250/span];$('maps').replaceChildren();for(const variant of assignment.presented_order){const r=assignment.routes[variant],div=document.createElement('div'),title=document.createElement('h3');title.textContent='경로 '+variant+' · '+Math.round(r.length_m)+' m';div.append(title);const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 300 300');svg.setAttribute('role','img');svg.setAttribute('aria-label','경로 '+variant+' 선형 도식');const line=document.createElementNS(svg.namespaceURI,'polyline');line.setAttribute('points',r.geometry.coordinates.map(p=>xy(p).join(',')).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','#28644e');line.setAttribute('stroke-width','4');svg.append(line);for(const [p,txt]of [[r.geometry.coordinates[0],'출발'],[r.geometry.coordinates.at(-1),'도착']]){const [x,y]=xy(p),circle=document.createElementNS(svg.namespaceURI,'circle'),label=document.createElementNS(svg.namespaceURI,'text');circle.setAttribute('cx',x);circle.setAttribute('cy',y);circle.setAttribute('r',5);circle.setAttribute('fill','#bd7b39');label.setAttribute('x',Math.max(4,Math.min(255,x+7)));label.setAttribute('y',Math.max(15,y-7));label.textContent=txt;svg.append(circle,label)}div.append(svg);const a=document.createElement('a');a.textContent='경로 GeoJSON 저장';a.download=assignment.pair_id+'-'+variant+'.geojson';a.href=URL.createObjectURL(new Blob([JSON.stringify({type:'Feature',properties:{label:variant,length_m:r.length_m},geometry:r.geometry})],{type:'application/geo+json'}));div.append(a);$('maps').append(div)}}
for(const [key,label]of Object.entries(ratingLabels)){const l=document.createElement('label');l.htmlFor='rating_'+key;l.textContent=label;const s=document.createElement('select');s.id='rating_'+key;const blank=document.createElement('option');blank.value='';blank.textContent='선택';s.append(blank);for(let n=1;n<=5;n++){const o=document.createElement('option');o.value=n;o.textContent=n;s.append(o)}$('ratings').append(l,s)}
$('completed').addEventListener('change',()=>{$('ratings').disabled=!$('completed').checked});$('ratings').disabled=true;
const today=new Date();$('walkDate').value=[today.getFullYear(),String(today.getMonth()+1).padStart(2,'0'),String(today.getDate()).padStart(2,'0')].join('-');$('walkDate').max=$('walkDate').value;
$('pair').addEventListener('change',selectPair);
$('start').addEventListener('click',async()=>{try{if(!$('consent').checked)throw Error('저장 동의가 필요해요');if(!profileMatch())throw Error('선호 값에 맞는 경로를 새로 준비해야 해요');assignment=await api('/pilot/assignment',{assignment_id:attemptId,participant_code:$('code').value,pair_id:pair.pair_id,manifest_sha256:manifest.manifest_sha256,consent:true,initial_intention:$('intention').value,profile_scores:scores()});$('setup').classList.add('hidden');$('walk').classList.remove('hidden');$('order').textContent='걸을 순서: '+assignment.presented_order.join(' → ');$('variant').value=assignment.presented_order[0];renderMaps();status('배정과 걷기 전 선호를 이 컴퓨터에 저장했어요.')}catch(e){status(e.message,true)}});
$('feedback').addEventListener('submit',async e=>{e.preventDefault();try{const v=$('variant').value;if(submitted.has(v))throw Error('이 경로 응답은 이미 저장했어요');const ratings=$('completed').checked?Object.fromEntries(Object.keys(ratingLabels).map(k=>[k,Number($('rating_'+k).value)])):{};const body={submission_id:pending[v]?.submission_id||crypto.randomUUID(),assignment_id:assignment.assignment_id,pair_id:assignment.pair_id,route_variant:v,manifest_sha256:assignment.manifest_sha256,route_version:assignment.route_version,consent:true,profile_scores:assignment.profile_scores,walk_completed:$('completed').checked,is_test:$('isTest').checked,walk_date:$('walkDate').value,time_of_day:$('time').value,weather:$('weather').value,ratings,issue:$('issue').value,issue_note:$('note').value};pending[v]=body;await api('/feedback',body);submitted.add(v);status('경로 '+v+' 응답을 저장했어요.'+(submitted.size===2?' 두 경로 기록이 끝났어요.':' 다음 경로를 걸은 뒤 평가해 주세요.'));if(submitted.size===2){$('save').disabled=true}else{$('variant').value=assignment.presented_order.find(k=>!submitted.has(k));$('completed').checked=false;$('ratings').disabled=true;Object.keys(ratingLabels).forEach(k=>$('rating_'+k).value='');$('note').value='';$('issue').value='none'}}catch(e){status(e.message,true)}});
api('/pilot/manifest').then(v=>{manifest=v;for(const p of v.route_pairs){const o=document.createElement('option');o.value=p.pair_id;o.textContent=p.neighborhood+' · '+p.label;$('pair').append(o)}selectPair()}).catch(e=>status(e.message,true));
</script></html>'''
