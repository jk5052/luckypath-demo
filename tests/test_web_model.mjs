import test from 'node:test';
import assert from 'node:assert/strict';
import {
  insideBounds, parseLatLon, formatDistance, distanceText,
  createRevisionGate, routeExplanation, friendlyError,
  TIME_BUDGET_DEFAULTS, parseTimeBudget, formatMinutes, routeBudgetView,
  routeRequestSnapshot, routeDownload,
  isKeyboardEditable, viewportLayout,
} from '../luckypath/web/model.js';

const bbox = [-73.970, 40.709, -73.951, 40.724];
const profile = {open: 5, green: 10, still: 5, social: 4, access: 4};
const geometry = coordinates => ({type: 'LineString', coordinates});
const line = geometry([[-73.96, 40.715], [-73.961, 40.716]]);
const otherLine = geometry([[-73.96, 40.715], [-73.962, 40.716], [-73.961, 40.716]]);
const result = (same = false, via = null, length = 100) => ({
  shortest: {length_m: 100, route: {geometry: line}},
  personalized: {length_m: length, route: {geometry: same ? structuredClone(line) : otherLine}},
  explicit_via: via,
});

test('coordinate entry accepts latitude, longitude and sends longitude, latitude', () => {
  assert.deepEqual(parseLatLon(' 40.7178 , -73.9577 ', bbox), [-73.9577, 40.7178]);
  assert.equal(insideBounds([-73.970, 40.709], bbox), true);
  assert.equal(insideBounds([-73.951, 40.724], bbox), true);
});

test('coordinate entry rejects wrong order, missing values, nonfinite and out-of-area values', () => {
  for (const text of ['-73.9577, 40.7178', '', '40.7178', '40.7178,', ', -73.9577',
    '40.7178, -73.9577, 0', 'NaN, -73.9577', 'Infinity, -73.9577',
    '40.7178, -Infinity', '40.8, -73.96', '40.7178, -73.98']) {
    assert.throws(() => parseLatLon(text, bbox), undefined, text);
  }
  for (const point of [null, [], [NaN, 40.715], [-73.96, Infinity], ['-73.96', 40.715],
    [-73.96, 40.715, 0], [true, 40.715]]) {
    assert.equal(insideBounds(point, bbox), false);
  }
});

test('display distances preserve units and never show invalid or negative distance', () => {
  assert.deepEqual(formatDistance(0), {value: '0', unit: 'm'});
  assert.equal(distanceText(813.94), '814 m');
  assert.equal(distanceText(1084.30), '1.08 km');
  assert.equal(distanceText(1000), '1.00 km');
  for (const value of [null, undefined, NaN, Infinity, -1, '100']) {
    assert.deepEqual(formatDistance(value), {value: '—', unit: 'm'});
  }
});

test('keyboard focus excludes buttons, selectors and readonly controls', () => {
  for(const element of [null,{tagName:'BUTTON'},{tagName:'SELECT'},
    {tagName:'INPUT',type:'range'},{tagName:'INPUT',type:'checkbox'},
    {tagName:'INPUT',type:'text',readOnly:true},{tagName:'TEXTAREA',disabled:true}]){
    assert.equal(isKeyboardEditable(element),false);
  }
  for(const element of [{tagName:'INPUT',type:'text'},{tagName:'INPUT',type:'number'},
    {tagName:'INPUT',type:'date'},{tagName:'TEXTAREA'},
    {tagName:'DIV',isContentEditable:true}]){
    assert.equal(isKeyboardEditable(element),true);
  }
});

test('pinch zoom keeps the layout height and navigation, with or without input focus', () => {
  for(const editableFocused of [false,true]){
    for(const scale of [1.25,2,3]){
      assert.deepEqual(viewportLayout({layoutHeight:844,visualHeight:844/scale,scale,editableFocused}),
        {height:844,keyboardOpen:false});
    }
  }
  // A reduced visual viewport alone is not proof that a keyboard is open.
  assert.deepEqual(viewportLayout({layoutHeight:844,visualHeight:460,scale:1,editableFocused:false}),
    {height:844,keyboardOpen:false});
});

test('keyboard opening, field switching, blur and closing restore the correct app height', () => {
  const base={layoutHeight:844,scale:1,editableFocused:true};
  assert.deepEqual(viewportLayout({...base,visualHeight:844}),{height:844,keyboardOpen:false});
  assert.deepEqual(viewportLayout({...base,visualHeight:460}),{height:460,keyboardOpen:true});
  // Focus moves to another input while the same keyboard remains open.
  assert.deepEqual(viewportLayout({...base,visualHeight:460}),{height:460,keyboardOpen:true});
  // Blur restores the layout even before Safari's closing animation updates the visual viewport.
  assert.deepEqual(viewportLayout({...base,visualHeight:460,editableFocused:false}),{height:844,keyboardOpen:false});
  assert.deepEqual(viewportLayout({...base,visualHeight:844}),{height:844,keyboardOpen:false});
});

test('normal viewport resizing and transient invalid measurements do not trigger keyboard mode', () => {
  assert.deepEqual(viewportLayout({layoutHeight:390,visualHeight:390,scale:1,editableFocused:false}),
    {height:390,keyboardOpen:false});
  for(const visualHeight of [0,NaN,undefined,Infinity]){
    assert.deepEqual(viewportLayout({layoutHeight:844,visualHeight,scale:1,editableFocused:true}),
      {height:844,keyboardOpen:false});
  }
  assert.deepEqual(viewportLayout({layoutHeight:844,visualHeight:460,scale:0,editableFocused:true}),
    {height:844,keyboardOpen:false});
});

test('a late response cannot replace a newer selection, even before its request starts', () => {
  const gate = createRevisionGate();
  const firstRequest = gate.invalidate();
  assert.equal(gate.isCurrent(firstRequest), true);
  const selectedNewInputs = gate.invalidate();
  assert.equal(gate.isCurrent(firstRequest), false);
  assert.equal(gate.isCurrent(selectedNewInputs), true);
  const secondRequest = gate.invalidate();
  let displayed = null;
  for (const [token, payload] of [[secondRequest, 'newer'], [firstRequest, 'older']]) {
    if (gate.isCurrent(token)) displayed = payload;
  }
  assert.equal(displayed, 'newer');
  assert.equal(gate.current(), secondRequest);
});

test('all route preferences set to one explain the distance-only decision', () => {
  const copy = routeExplanation(result(true), {...profile, open: 1, green: 1, still: 1});
  assert.match(copy, /set to 1.*prioritizes distance/);
});

test('same route with active preferences is described honestly without a quality claim', () => {
  const copy = routeExplanation(result(true), profile);
  assert.match(copy, /are the same/);
  assert.doesNotMatch(copy, /verified|guaranteed|quieter|greener/);
});

test('explicit via is mentioned for both alternatives including coincident routes', () => {
  for (const same of [false, true]) {
    const copy = routeExplanation(result(same, [-73.96, 40.715]), profile);
    assert.match(copy, /Both routes/);
  }
  const zeroCopy = routeExplanation(result(true, [-73.96, 40.715]),
    {...profile, open: 1, green: 1, still: 1});
  assert.match(zeroCopy, /Both routes/);
});

test('only a truly zero-length route is called the same walking point', () => {
  assert.match(routeExplanation(result(true, null, 0), profile), /same walking point/);
  assert.doesNotMatch(routeExplanation(result(false, null, 0.9), profile), /same walking point/);
});

test('actual engine routing errors receive actionable English wording', () => {
  assert.match(friendlyError(new Error('No connected walking route exists for origin to destination.')), /No connected walking route/);
  assert.match(friendlyError(new Error('Origin is 210.0 m from the walking network; maximum snap distance is 150 m.')), /too far from a walking path/);
  assert.match(friendlyError(new Error('origin must be inside the study area shown on the map')), /inside the dashed Williamsburg/);
  assert.match(friendlyError(new TypeError('Failed to fetch')), /reach the server/);
  assert.equal(friendlyError(new Error('요청 항목을 확인해 주세요.')), 'Could not complete the request. Check your inputs and try again.');
});

test('time entry accepts form numbers and only counts a dwell when a via exists', () => {
  const fields={max_minutes:'30',walking_speed_kmh:'4.5',buffer_minutes:'3',stop_minutes:'12'};
  assert.deepEqual(parseTimeBudget(fields,true),{max_minutes:30,walking_speed_kmh:4.5,buffer_minutes:3,stop_minutes:12});
  assert.deepEqual(parseTimeBudget(fields,false),TIME_BUDGET_DEFAULTS);
  assert.equal(parseTimeBudget({...fields,stop_minutes:''},false).stop_minutes,0);
  assert.equal(parseTimeBudget({...fields,max_minutes:'240',buffer_minutes:'30',stop_minutes:'120'},true).max_minutes,240);
});

test('time entry rejects missing, nonfinite, boolean and out-of-range values without clamping', () => {
  for(const [key,values] of Object.entries({max_minutes:['',null,undefined,true,0,241,Infinity,NaN],walking_speed_kmh:['',1.9,6.1,false],buffer_minutes:['',-1,31],stop_minutes:['',-1,121]})){
    for(const value of values)assert.throws(()=>parseTimeBudget({...TIME_BUDGET_DEFAULTS,[key]:value},true),undefined,`${key}=${value}`);
  }
});

test('displayed estimated minutes round upwards and unknown values stay unknown', () => {
  assert.equal(formatMinutes(0),'0 min');
  assert.equal(formatMinutes(30),'30 min');
  assert.equal(formatMinutes(30.01),'31 min');
  for(const value of [null,undefined,NaN,Infinity,-1,'30'])assert.equal(formatMinutes(value),'—');
});

function timedResult(feasible=true,minimum=22.2){
  const route=result(false);
  return {...route,personalized:feasible?route.personalized:null,
    budget:{status:feasible?'feasible':'infeasible',max_minutes:feasible?30:15,
      minimum_required_minutes:minimum,estimated_total_minutes:feasible?25.2:null,remaining_minutes:feasible?4.8:null}};
}

test('a feasible budget uses the chosen-route estimate and does not offer to increase time', () => {
  const view=routeBudgetView(timedResult());
  assert.equal(view.feasible,true);assert.equal(view.display_minutes,'26 min');
  assert.equal(view.increase_to_minutes,null);
  assert.match(view.message,/Actual travel time may vary/);
});

test('a feasible label cannot override elapsed or minimum time exceeding the maximum', () => {
  for(const budget of [
    {max_minutes:15,minimum_required_minutes:16,estimated_total_minutes:20},
    {max_minutes:15,minimum_required_minutes:12,estimated_total_minutes:20},
    {max_minutes:15,minimum_required_minutes:16,estimated_total_minutes:14},
  ]){
    const response=timedResult();Object.assign(response.budget,budget);
    assert.throws(()=>routeBudgetView(response),/verify the estimated time/);
  }
  const boundary=timedResult();
  Object.assign(boundary.budget,{max_minutes:30,minimum_required_minutes:30+1e-10,estimated_total_minutes:30+1e-10});
  assert.equal(routeBudgetView(boundary).feasible,true);
});

test('provided route estimates must agree with the budget and their component sum', () => {
  const response=timedResult();
  response.shortest.time_estimate={total_minutes:22.2};
  response.personalized.time_estimate={walking_minutes:20,connector_minutes:2.2,stop_minutes:0,buffer_minutes:3,total_minutes:25.2};
  assert.equal(routeBudgetView(response).feasible,true);
  response.personalized.time_estimate.total_minutes=26;
  assert.throws(()=>routeBudgetView(response),/verify the estimated time/);
  response.personalized.time_estimate.total_minutes=25.2;
  response.personalized.time_estimate.connector_minutes=8;
  assert.throws(()=>routeBudgetView(response),/verify the estimated time/);
  response.personalized.time_estimate.connector_minutes=2.2;
  response.shortest.time_estimate.total_minutes=23;
  assert.throws(()=>routeBudgetView(response),/verify the estimated time/);
});

test('an infeasible result shows only a minimum and treats shortest as a reference', () => {
  const response=timedResult(false),view=routeBudgetView(response);
  assert.equal(view.feasible,false);assert.equal(view.display_minutes,'23 min');
  assert.equal(view.increase_to_minutes,23);
  assert.match(view.message,/Add time or change/);
  assert.match(routeExplanation(response,profile),/for reference/);
  assert.equal(routeBudgetView(timedResult(false,240.1)).increase_to_minutes,null);
  assert.throws(()=>routeBudgetView({...response,personalized:result().personalized}));
  assert.throws(()=>routeBudgetView({...timedResult(),personalized:null}));
  assert.throws(()=>routeBudgetView(result()));
});

test('successful request snapshots preserve time and route inputs across later edits', () => {
  const state={origin:[-73.96,40.715],destination:[-73.961,40.716],scores:{...profile},
    via:{lonlat:[-73.962,40.717],name:'A place'},time_budget:{...TIME_BUDGET_DEFAULTS,stop_minutes:8},birth_date:'private'};
  const request=routeRequestSnapshot(state);
  state.origin[0]=-73.965;state.scores.green=2;state.via.lonlat[1]=40.719;state.time_budget.max_minutes=10;
  assert.deepEqual(request.origin,[-73.96,40.715]);assert.equal(request.profile_scores.green,10);
  assert.deepEqual(request.via,[-73.962,40.717]);assert.equal(request.time_budget.max_minutes,30);
  assert.equal(request.time_budget.stop_minutes,8);assert.equal('birth_date' in request,false);
});

test('downloads include time policy and estimates but allowlist profile provenance', () => {
  const request=routeRequestSnapshot({origin:[-73.96,40.715],destination:[-73.961,40.716],scores:profile,via:null,time_budget:TIME_BUDGET_DEFAULTS});
  const response=timedResult();
  response.geojson={type:'FeatureCollection',features:[]};response.model_version='model';
  response.budget.policy_id='walking-time-budget-v1';
  response.personalized.time_estimate={total_minutes:25.2,estimated:true};
  response.shortest.time_estimate={total_minutes:22.2,estimated:true};
  const source={kind:'independent-analyses',analysis_version:'v2',saju_policy:'s',astrology_policy:'a',fusion_policy:'f',birth_date:'PRIVATE_BIRTH',chart:'PRIVATE_CHART'};
  const output=routeDownload(response,request,source);
  assert.deepEqual(output.properties.time_budget,TIME_BUDGET_DEFAULTS);
  assert.equal(output.properties.budget.policy_id,'walking-time-budget-v1');
  assert.equal(output.properties.time_estimates.personalized.total_minutes,25.2);
  assert.equal(output.properties.time_estimates.shortest.total_minutes,22.2);
  assert.doesNotMatch(JSON.stringify(output),/PRIVATE|birth_date|chart/);
  response.personalized=null;response.budget.status='infeasible';
  assert.equal(routeDownload(response,request,source).properties.time_estimates.personalized,null);
});
