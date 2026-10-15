// Controller tests use synthetic data and a fake transport; no provider key or browser storage.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createExternalController} from '../luckypath/web/external.js';

class Element {
  constructor(){this.children=[];this.listeners={};this.attributes={};this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.classList={toggle(){}};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(name,value){this.attributes[name]=value;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  async emit(name){for(const fn of this.listeners[name]??[])await fn({preventDefault(){}});}
}
const config=(configured=false,source=configured?'session':'none')=>({external_services:{ors:{configured,key_source:source},weather:{enabled:true,provider:'open-meteo'}}});
const trip=(minutes=30)=>({origin:[-73.9577,40.7178],destination:[-73.967,40.7147],time_budget:{max_minutes:minutes,walking_speed_kmh:4.5,buffer_minutes:3,stop_minutes:0}});
const weather=()=>({status:'ok',provider:'open-meteo',departure_at:'2026-10-13T14:00:00-04:00',window_end:'2026-10-13T14:30:00-04:00',fetched_at:'2026-10-13T14:00:00-04:00',summary:{temperature_c:20.4,apparent_temperature_c:19,precipitation_probability_max:15,precipitation_mm_total:0.2,wind_speed_kmh_max:12.5},messages:['시간별 예보이며 실제 이동 시간 동안의 정확한 강수 합계는 아니에요.'],attribution:[{name:'Open-Meteo',url:'https://open-meteo.com/'}]});
const reference=()=>({status:'ok',provider:'openrouteservice',distance_m:1100,walking_minutes:16,total_minutes:19,buffer_minutes:3,stop_minutes:0,within_time_budget:true,same_route_as_recommendation:false,user_walking_speed_applied:false});
const found=(name='Domino Park',provider='local')=>({status:'ok',provider,results:[{id:'demo',name,lonlat:[-73.965,40.714]}],attribution:[{name:'OpenStreetMap'}]});
function fixture(t,connected=false){
  t.mock.timers.enable({apis:['setTimeout']});
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  globalThis.document={getElementById:$,createElement:()=>new Element()};
  // Any accidental browser persistence is a test failure.
  Object.defineProperty(globalThis,'localStorage',{configurable:true,get(){throw Error('Browser persistence forbidden');}});
  Object.defineProperty(globalThis,'sessionStorage',{configurable:true,get(){throw Error('Browser persistence forbidden');}});
  const requests=[],chosen=[];
  const controller=createExternalController({api(path,options){return new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));},onChoose(...args){chosen.push(args);}});
  $('search-target').value='destination';$('reference-result').hidden=true;$('weather-result').hidden=true;
  controller.configure(config(connected));controller.invalidateJourney(trip());
  return {$,controller,requests,chosen};
}
const requestBody=request=>JSON.parse(request.options.body);

test('public demo hides key controls, never submits keys, and keeps local search usable',async t=>{
  const f=fixture(t),publicConfig=config(false,'none');
  publicConfig.external_services.ors.can_manage_key=false;
  publicConfig.external_services.weather.enabled=false;
  f.controller.configure(publicConfig);
  assert.equal(f.$('ors-settings-panel').hidden,true);
  assert.equal(f.$('deployment-info').hidden,false);
  assert.equal(f.$('ors-connect').disabled,true);
  f.$('ors-api-key').value='synthetic-not-to-send';
  await f.$('ors-settings-form').emit('submit');
  await f.$('ors-disconnect').emit('click');
  assert.equal(f.requests.length,0);
  assert.doesNotMatch(f.$('reference-status').textContent,/Connect an API key/);
  f.$('search-query').value='Domino';
  const pending=f.$('place-search-form').emit('submit');
  assert.equal(f.requests[0].path,'/explore/search');
  f.requests[0].resolve({...found(),fallback:true,reason:'missing_key'});await pending;
  assert.doesNotMatch(f.$('search-mode').textContent,/connect an API key/i);
  assert.doesNotMatch(f.$('search-status').textContent,/Connect an openrouteservice key/);
});

test('no key uses explicit local search and a selected result changes only one endpoint',async t=>{
  const f=fixture(t);assert.match(f.$('search-mode').textContent,/Saved neighborhood/);
  assert.equal(f.requests.length,0);
  f.$('search-query').value='Domino';const pending=f.$('place-search-form').emit('submit');
  assert.deepEqual(requestBody(f.requests[0]),{query:'Domino'});
  f.requests[0].resolve(found());await pending;
  await f.$('search-results').children[0].emit('click');
  assert.deepEqual(f.chosen,[['destination',{id:'demo',name:'Domino Park',lonlat:[-73.965,40.714]}]]);
  assert.equal(f.$('reference-refresh').disabled,true);
});

test('editing a query cancels old results, including a transport resolving after abort',async t=>{
  const f=fixture(t);f.$('search-query').value='old';const pending=f.$('place-search-form').emit('submit');
  f.$('search-query').value='new';await f.$('search-query').emit('input');
  assert.equal(f.requests[0].options.signal.aborted,true);
  f.requests[0].resolve(found('old'));await pending;
  assert.equal(f.$('search-results').children.length,0);assert.deepEqual(f.chosen,[]);
});

test('switching target invalidates prior results and provider labels are plain text',async t=>{
  const f=fixture(t,true);f.$('search-query').value='park';const pending=f.$('place-search-form').emit('submit');
  const name='<img src=x onerror=alert(1)>';f.requests[0].resolve(found(name,'openrouteservice'));await pending;
  const oldButton=f.$('search-results').children[0];assert.equal(oldButton.textContent,name);assert.equal(oldButton.children.length,0);
  f.$('search-target').value='origin';await f.$('search-target').emit('change');await oldButton.emit('click');
  assert.deepEqual(f.chosen,[]);
});

test('invalid search coordinates are rejected without an endpoint callback',async t=>{
  const f=fixture(t);f.$('search-query').value='park';const pending=f.$('place-search-form').emit('submit');
  const bad=found();bad.results[0].lonlat=[-73.9,'40.7'];f.requests[0].resolve(bad);await pending;
  assert.equal(f.$('search-results').children.length,0);assert.deepEqual(f.chosen,[]);assert.match(f.$('search-status').textContent,/Could not finish/);
});

test('weather auto loads after debounce and sends only duration',async t=>{
  const f=fixture(t);t.mock.timers.tick(400);assert.equal(f.requests.length,1);
  assert.equal(f.requests[0].path,'/explore/weather');assert.deepEqual(requestBody(f.requests[0]),{duration_minutes:30});
  f.requests[0].resolve(weather());await Promise.resolve();await Promise.resolve();
  assert.equal(f.$('weather-result').hidden,false);assert.equal(f.$('weather-metrics').children[6].textContent,'Rainfall in overlapping hours');
  assert.deepEqual(f.chosen,[]);assert.match(f.$('weather-window').textContent,/New York time/);
  assert.match(f.$('weather-messages').children[1].textContent,/not your exact travel period/);
  assert.doesNotMatch(f.$('weather-messages').children.map(node=>node.textContent).join(' '),/[\uac00-\ud7a3]/);
});

test('new duration hides stale weather immediately and late response cannot replace it',async t=>{
  const f=fixture(t);const older=f.$('weather-refresh').emit('click');
  f.controller.invalidateJourney(trip(45));assert.equal(f.requests[0].options.signal.aborted,true);
  const newer=f.$('weather-refresh').emit('click');f.requests[1].resolve(weather());await newer;
  const stale=weather();stale.summary.temperature_c=99;f.requests[0].resolve(stale);await older;
  assert.equal(f.$('weather-metrics').children[1].textContent,'20.4°C');assert.deepEqual(requestBody(f.requests[1]),{duration_minutes:45});
});

test('weather failure and incomplete forecast leave local recommendation hooks untouched',async t=>{
  const f=fixture(t);const failed=f.$('weather-refresh').emit('click');f.requests[0].reject(Error('offline'));await failed;
  assert.match(f.$('weather-status').textContent,/Route recommendations are still/);assert.equal(f.$('weather-result').hidden,true);
  const retry=f.$('weather-refresh').emit('click');const data=weather();data.summary.precipitation_probability_max=null;f.requests[1].resolve(data);await retry;
  assert.equal(f.$('weather-metrics').children[5].textContent,'Unavailable');assert.deepEqual(f.chosen,[]);
  assert.equal(f.$('weather-messages').children.at(-1).textContent,'Some forecast measurements are unavailable.');
});

test('malformed weather never displays NaN or a false success',async t=>{
  const f=fixture(t);const pending=f.$('weather-refresh').emit('click');const data=weather();data.summary.precipitation_probability_max=101;f.requests[0].resolve(data);await pending;
  assert.equal(f.$('weather-result').hidden,true);assert.match(f.$('weather-status').textContent,/Could not load/);
});

test('reference is explicit, allows only route inputs, and labels independent provider pace',async t=>{
  const f=fixture(t,true);const input={...trip(),profile_scores:{green:9},birth_date:'synthetic-do-not-send',ors_api_key:'fake-do-not-send'};f.controller.invalidateJourney(input);
  assert.equal(f.requests.length,0);const pending=f.$('reference-refresh').emit('click');assert.deepEqual(requestBody(f.requests[0]),trip());
  f.requests[0].resolve(reference());await pending;
  assert.equal(f.$('reference-result').hidden,false);assert.match(f.$('reference-status').textContent,/Separate.*provider walking pace/);assert.match(f.$('reference-fit').textContent,/separate/);
});

test('endpoint or pace changes clear and cancel reference but unchanged journey retains it',async t=>{
  const f=fixture(t,true);const first=f.$('reference-refresh').emit('click');f.requests[0].resolve(reference());await first;
  f.controller.invalidateJourney({...trip(),profile_scores:{green:10}});assert.equal(f.$('reference-result').hidden,false);
  const older=f.$('reference-refresh').emit('click');const next=trip();next.origin=[-73.96,40.719];f.controller.invalidateJourney(next);
  assert.equal(f.$('reference-result').hidden,true);assert.equal(f.requests[1].options.signal.aborted,true);
  f.requests[1].resolve(reference());await older;assert.equal(f.$('reference-result').hidden,true);
  const pending=f.$('reference-refresh').emit('click');next.time_budget.walking_speed_kmh=3.5;f.controller.invalidateJourney(next);f.requests[2].resolve(reference());await pending;
  assert.equal(f.$('reference-result').hidden,true);
});

test('contradictory reference sums or same-route claims cannot render',async t=>{
  const f=fixture(t,true);
  for(const data of [{...reference(),total_minutes:18},{...reference(),same_route_as_recommendation:true},{...reference(),within_time_budget:false}]){
    const pending=f.$('reference-refresh').emit('click');f.requests.at(-1).resolve(data);await pending;assert.equal(f.$('reference-result').hidden,true);
  }
  assert.deepEqual(f.chosen,[]);
});

test('settings clears the password before response and excludes it from subsequent calls',async t=>{
  const f=fixture(t);f.$('ors-api-key').value='synthetic-api-key';const pending=f.$('ors-settings-form').emit('submit');
  assert.equal(f.$('ors-api-key').value,'');assert.equal(f.requests[0].path,'/explore/external/settings');assert.deepEqual(requestBody(f.requests[0]),{ors_api_key:'synthetic-api-key'});
  f.requests[0].resolve(config(true).external_services);await pending;assert.match(f.$('ors-state').textContent,/key configured/);assert.doesNotMatch(f.$('ors-state').textContent,/connection verified|synthetic-api-key/);
  const comparison=f.$('reference-refresh').emit('click');assert.equal(f.requests[1].options.body.includes('synthetic-api-key'),false);f.requests[1].resolve(reference());await comparison;
  const disconnect=f.$('ors-disconnect').emit('click');assert.deepEqual(requestBody(f.requests[2]),{ors_api_key:''});f.requests[2].resolve(config(false).external_services);await disconnect;
  assert.equal(f.$('reference-result').hidden,true);assert.equal(f.$('reference-refresh').disabled,true);
});

test('rejected settings never echo key or raw provider error and environment key is identified',async t=>{
  const f=fixture(t);f.$('ors-api-key').value='synthetic-secret';const pending=f.$('ors-settings-form').emit('submit');f.requests[0].reject(Error('synthetic-secret'));await pending;
  assert.equal(f.$('ors-api-key').value,'');assert.doesNotMatch(f.$('ors-settings-status').textContent,/synthetic-secret/);
  f.controller.configure(config(true,'environment'));assert.match(f.$('ors-state').textContent,/server environment/);assert.equal(f.$('ors-disconnect').disabled,true);
});

test('invalid external configuration degrades without throwing or mutating route callbacks',t=>{
  const f=fixture(t);assert.doesNotThrow(()=>f.controller.configure({external_services:{ors:{configured:'yes'}}}));
  assert.equal(f.$('search-submit').disabled,true);assert.equal(f.$('reference-refresh').disabled,true);assert.deepEqual(f.chosen,[]);
});


test('a local fallback translates its structured reason and attribution into English',async t=>{
  const f=fixture(t,true);f.$('search-query').value='park';const pending=f.$('place-search-form').emit('submit');const body=found();body.reason='auth_failed';body.fallback=true;body.message='API 키를 확인해 주세요. 저장된 동네 장소에서 찾았어요.';body.attribution='OpenStreetMap contributors / NYC Open Data · 저장된 장소 자료';f.requests[0].resolve(body);await pending;
  assert.match(f.$('search-status').textContent,/API key was not accepted/);assert.match(f.$('search-status').textContent,/1 result/);assert.match(f.$('search-mode').textContent,/Saved neighborhood/);
  assert.doesNotMatch(f.$('search-status').textContent+f.$('search-attribution').textContent,/[\uac00-\ud7a3]/);
});
