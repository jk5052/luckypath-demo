// External information is independent of the recommendation and birth analysis.
// API keys only enter the settings request; no storage or route payload uses them.
const $=id=>document.getElementById(id);
const finite=value=>typeof value==='number'&&Number.isFinite(value);
const coordinate=value=>Array.isArray(value)&&value.length===2&&value.every(finite)&&Math.abs(value[0])<=180&&Math.abs(value[1])<=90;
const label=(value,fallback='')=>typeof value==='string'?value.slice(0,800):fallback;
const SERVICE_MESSAGES={
  missing_key:'Connect an openrouteservice key to search addresses and compare walking routes.',
  auth_failed:'The API key was not accepted. Check the key and your account permissions.',
  quota:'The service request limit has been reached. Try again later.',
  timeout:'The service took too long to respond. Your local route is still available.',
  network:'Could not connect to the service. Your local route is still available.',
  provider_error:'The service could not process this request. Your local route is still available.',
  bad_response:'The service returned an invalid response, so this result was not displayed.',
  forecast_unavailable:'No forecast is available for your travel window.',
  redirect_blocked:'The service returned an unexpected redirect. Please try again later.',
  busy:'Another request is in progress. Try again shortly.',
};
const LOCAL_SEARCH='Saved neighborhood places · connect an API key for address search';
const ADDRESS_SEARCH='Addresses and places in Williamsburg · openrouteservice';
function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function message(id,text,error=false){$(id).textContent=text;$(id).classList.toggle('error',error);}
function attribution(value){return Array.isArray(value)?value.map(item=>typeof item==='string'?item:label(item?.name)).filter(Boolean).join(' · '):label(value);}
function time(value){if(typeof value!=='string'||!value.trim())return null;const date=new Date(value);return Number.isFinite(date.getTime())?new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(date):null;}
function minutes(value){return `${Math.ceil(value-1e-9)} min`;}
function metrics(id,items){const list=$(id);list.replaceChildren();for(const[name,value]of items)list.append(el('dt','',name),el('dd','',value));}
function serviceConfig(value){
  if(!value||typeof value.ors?.configured!=='boolean'||!['environment','session','none'].includes(value.ors.key_source))throw Error('Invalid API settings response.');
  return {ors:{configured:value.ors.configured,key_source:value.ors.key_source,can_manage_key:value.ors.can_manage_key!==false},weather:{enabled:value.weather?.enabled===true}};
}
function searchResults(body){
  if(body?.status!=='ok'||!['local','openrouteservice'].includes(body.provider)||!Array.isArray(body.results)||body.results.length>30)throw Error('Invalid place search response.');
  if(body.results.some(row=>!coordinate(row?.lonlat)||typeof row.name!=='string'||!row.name.trim()))throw Error('Invalid search result coordinates.');
  return body.results;
}
function weatherData(body){
  if(body?.status!=='ok'||body.provider!=='open-meteo'||!time(body.departure_at)||!time(body.window_end)||!time(body.fetched_at)||!body.summary)throw Error('Invalid weather response.');
  if(new Date(body.window_end)<new Date(body.departure_at))throw Error('Invalid forecast window.');
  const s=body.summary;
  for(const key of ['temperature_c','apparent_temperature_c','precipitation_probability_max','precipitation_mm_total','wind_speed_kmh_max'])if(s[key]!==null&&!finite(s[key]))throw Error('Invalid weather measurements.');
  if((s.precipitation_probability_max!==null&&(s.precipitation_probability_max<0||s.precipitation_probability_max>100))||(s.precipitation_mm_total!==null&&s.precipitation_mm_total<0)||(s.wind_speed_kmh_max!==null&&s.wind_speed_kmh_max<0))throw Error('Invalid weather measurements.');
  return s;
}
function referenceData(body,budget){
  if(body?.status!=='ok'||body.provider!=='openrouteservice'||body.same_route_as_recommendation!==false||body.user_walking_speed_applied!==false||typeof body.within_time_budget!=='boolean')throw Error('Invalid reference route response.');
  for(const key of ['distance_m','walking_minutes','total_minutes','buffer_minutes','stop_minutes'])if(!finite(body[key])||body[key]<0)throw Error('Invalid reference route timing.');
  if(Math.abs(body.total_minutes-body.walking_minutes-body.buffer_minutes-body.stop_minutes)>1e-6||Math.abs(body.buffer_minutes-budget.buffer_minutes)>1e-6||Math.abs(body.stop_minutes-budget.stop_minutes)>1e-6||body.within_time_budget!==(body.total_minutes<=budget.max_minutes+1e-9))throw Error('Reference route timing does not match the request.');
  return body;
}
function snapshot(value){
  if(!coordinate(value?.origin)||!coordinate(value?.destination)||!value.time_budget)return null;
  const budget={};for(const key of ['max_minutes','walking_speed_kmh','buffer_minutes','stop_minutes']){if(!finite(value.time_budget[key]))return null;budget[key]=value.time_budget[key];}
  if(budget.max_minutes<1||budget.max_minutes>240||budget.walking_speed_kmh<2||budget.walking_speed_kmh>6||budget.buffer_minutes<0||budget.buffer_minutes>30||budget.stop_minutes<0||budget.stop_minutes>120)return null;
  const copy={origin:[...value.origin],destination:[...value.destination],time_budget:budget};
  if(value.via){const point=value.via.lonlat||value.via;if(!coordinate(point))return null;copy.via=[...point];}else if(budget.stop_minutes!==0)return null;
  return copy;
}

export function createExternalController({api,onChoose}){
  let configured=false,services=null,journey=null,journeyKey='',duration=null,weatherTimer;
  const tasks={search:{rev:0},weather:{rev:0},reference:{rev:0},settings:{rev:0}};
  function cancel(name){const task=tasks[name];task.rev++;task.controller?.abort();task.controller=null;return task.rev;}
  function start(name){const rev=cancel(name),controller=new AbortController();tasks[name].controller=controller;return {rev,signal:controller.signal};}
  function current(name,rev){return rev===tasks[name].rev;}
  const post=(path,body,signal)=>api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal});
  // Render known reason codes, never a provider's raw or differently localized message.
  function unavailable(body,fallback){if(body?.reason==='missing_key'&&services?.ors.can_manage_key===false)return 'Address search and external route comparison are not enabled in this demo. Saved neighborhood places are available.';return Object.hasOwn(SERVICE_MESSAGES,body?.reason)?SERVICE_MESSAGES[body.reason]:fallback;}
  function clearSearch(){cancel('search');$('search-results').replaceChildren();$('search-attribution').textContent='';$('search-submit').disabled=!configured;message('search-status','');}
  function clearReference(){cancel('reference');$('reference-result').hidden=true;$('reference-metrics').replaceChildren();$('reference-fit').textContent='';$('reference-refresh').disabled=!journey||!services?.ors.configured;message('reference-status',!services?.ors.configured?(services?.ors.can_manage_key===false?'External route comparison is not enabled in this demo.':'Connect an API key to compare an external walking route.'):journey?'Compare an external route using your current trip settings.':'Enter a valid start, destination, and available time.');}
  function renderServices(){
    const source=services.ors.key_source;
    const managed=services.ors.can_manage_key===false;
    $('ors-settings-panel').hidden=managed;$('deployment-info').hidden=!managed;
    $('search-mode').textContent=services.ors.configured?ADDRESS_SEARCH:managed?'Saved neighborhood places':LOCAL_SEARCH;
    $('ors-state').textContent=services.ors.configured?`API key configured · ${source==='environment'?'server environment':'current server session'} · Search or compare a route to check availability.`:'No API key connected. Saved places and local recommendations are available.';
    $('ors-connect').disabled=managed;$('ors-disconnect').disabled=managed||source!=='session';$('search-submit').disabled=false;
    $('weather-refresh').disabled=!journey||!services.weather.enabled;
    clearSearch();clearReference();
  }
  async function search(event){
    event?.preventDefault();if(!configured)return;
    const query=$('search-query').value.trim(),target=$('search-target').value;
    clearSearch();if(query.length<2||query.length>160){message('search-status','Enter 2–160 characters to search.',true);return;}
    const {rev,signal}=start('search');$('search-submit').disabled=true;message('search-status','Finding places…');
    try{
      const body=await post('/explore/search',{query},signal);if(!current('search',rev))return;
      if(body?.status!=='ok'){message('search-status',unavailable(body,'Could not search for places. Please try again.'),true);return;}
      const rows=searchResults(body);$('search-mode').textContent=body.provider==='local'?(services.ors.can_manage_key===false?'Saved neighborhood places':LOCAL_SEARCH):ADDRESS_SEARCH;
      for(const row of rows){const button=el('button','search-result-button',row.name);button.type='button';button.setAttribute('aria-label',`Use ${row.name} as your ${target==='origin'?'start':'destination'}`);button.addEventListener('click',()=>{if(!current('search',rev))return;const applied=onChoose(target,{id:row.id,name:row.name,lonlat:[...row.lonlat]});if(applied===false){message('search-status','This place is outside the current routing area.',true);return;}clearSearch();message('search-status',`${row.name} is now your ${target==='origin'?'start':'destination'}.`);});$('search-results').append(button);}
      const resultMessage=rows.length?`${rows.length} ${rows.length===1?'result':'results'} · Choose your ${target==='origin'?'start A':'destination B'}.`:'No matching places in this area. You can also choose a point on the map.';
      const fallbackMessage=body.provider==='local'&&body.fallback?`${unavailable(body,'Address search is unavailable.')} Showing saved neighborhood places.`:'';
      message('search-status',[fallbackMessage,resultMessage].filter(Boolean).join(' '));
      $('search-attribution').textContent=body.provider==='local'?'OpenStreetMap contributors / NYC Open Data · saved places':attribution(body.attribution);
    }catch(error){if(current('search',rev)&&error.name!=='AbortError')message('search-status','Could not finish the search. You can still choose a point on the map.',true);}
    finally{if(current('search',rev))$('search-submit').disabled=false;}
  }
  async function weather(){
    clearTimeout(weatherTimer);if(!configured||!journey||!services.weather.enabled)return;
    const requestedDuration=journey.time_budget.max_minutes,{rev,signal}=start('weather');$('weather-result').hidden=true;$('weather-refresh').disabled=true;message('weather-status','Checking the forecast for your trip…');
    try{
      const body=await post('/explore/weather',{duration_minutes:requestedDuration},signal);if(!current('weather',rev))return;
      if(body?.status!=='ok'){message('weather-status',unavailable(body,'Could not load the forecast. Try again shortly.'),true);return;}
      const s=weatherData(body),display=(value,suffix)=>value===null?'Unavailable':`${Number(value.toFixed(1))}${suffix}`;
      metrics('weather-metrics',[['Temperature at departure',display(s.temperature_c,'°C')],['Feels like',display(s.apparent_temperature_c,'°C')],['Peak rain chance',display(s.precipitation_probability_max,'%')],['Rainfall in overlapping hours',display(s.precipitation_mm_total,' mm')],['Peak wind speed',display(s.wind_speed_kmh_max,' km/h')]]);
      $('weather-window').textContent=`${time(body.departure_at)} → ${time(body.window_end)} · New York time`;
      const notes=['Hourly forecast for a trip starting now; conditions on your street may differ.','Rainfall and probability cover overlapping forecast hours, not your exact travel period.'];
      if(Object.values(s).some(value=>value===null))notes.push('Some forecast measurements are unavailable.');
      if(s.precipitation_probability_max!==null&&s.precipitation_probability_max>=50)notes.push('Rain probability reaches at least 50% during your travel window.');
      $('weather-messages').replaceChildren();for(const note of notes)$('weather-messages').append(el('p','field-help',note));
      $('weather-fetched').textContent=`Updated ${time(body.fetched_at)} · ${attribution(body.attribution)||'Open-Meteo'}`;
      $('weather-result').hidden=false;message('weather-status','Forecast for departure now · actual conditions may vary.');
    }catch(error){if(current('weather',rev)&&error.name!=='AbortError')message('weather-status','Could not load the weather. Route recommendations are still available.',true);}
    finally{if(current('weather',rev))$('weather-refresh').disabled=false;}
  }
  async function reference(){
    if(!journey||!services?.ors.configured)return;
    const request=snapshot(journey),{rev,signal}=start('reference');$('reference-result').hidden=true;$('reference-refresh').disabled=true;message('reference-status','Checking an external route…');
    try{
      const body=await post('/explore/reference',request,signal);if(!current('reference',rev))return;
      if(body?.status!=='ok'){message('reference-status',unavailable(body,'Could not load an external route.'),true);return;}
      const r=referenceData(body,request.time_budget),distance=r.distance_m<1000?`${Math.round(r.distance_m)} m`:`${(r.distance_m/1000).toFixed(2)} km`;
      metrics('reference-metrics',[['External route distance',distance],['Walking time at provider pace',`About ${minutes(r.walking_minutes)}`],['Buffer · time at stop',`${r.buffer_minutes} min · ${r.stop_minutes} min`],['Estimated total time',`About ${minutes(r.total_minutes)}`]]);
      $('reference-fit').textContent=r.within_time_budget?`This external route is estimated to fit within ${request.time_budget.max_minutes} min. It is separate from your recommended route.`:`This external route is estimated to exceed ${request.time_budget.max_minutes} min. It is separate from your recommended route.`;
      $('reference-result').hidden=false;message('reference-status','Separate openrouteservice route · provider walking pace');
    }catch(error){if(current('reference',rev)&&error.name!=='AbortError')message('reference-status','Could not compare the external route. Your recommendation is still available.',true);}
    finally{if(current('reference',rev))$('reference-refresh').disabled=false;}
  }
  async function settings(event,disconnect=false){
    event?.preventDefault();if(!configured||services?.ors.can_manage_key===false)return;
    const key=disconnect?'':$('ors-api-key').value.trim();$('ors-api-key').value='';
    if(!disconnect&&!key){message('ors-settings-status','Enter your API key.',true);return;}
    const {rev,signal}=start('settings');$('ors-connect').disabled=true;$('ors-disconnect').disabled=true;clearSearch();clearReference();$('search-submit').disabled=true;$('reference-refresh').disabled=true;message('ors-settings-status','Updating this server session…');
    try{
      const body=await post('/explore/external/settings',{ors_api_key:key},signal);if(!current('settings',rev))return;
      services=serviceConfig(body.external_services||body);renderServices();message('ors-settings-status',disconnect?'The session key has been removed.':'Key applied. Search or compare a route to check availability.');
    }catch(error){if(current('settings',rev)&&error.name!=='AbortError')message('ors-settings-status','Could not update the API settings. Re-enter the key and try again.',true);}
    finally{if(current('settings',rev)){$('ors-connect').disabled=false;$('ors-disconnect').disabled=services.ors.key_source!=='session';$('search-submit').disabled=false;$('reference-refresh').disabled=!journey||!services.ors.configured;}}
  }
  function invalidateJourney(value){
    const next=snapshot(value),key=JSON.stringify(next);if(key===journeyKey)return;
    journey=next;journeyKey=key;clearReference();
    const nextDuration=journey?.time_budget.max_minutes??null;
    if(nextDuration!==duration){duration=nextDuration;cancel('weather');clearTimeout(weatherTimer);$('weather-result').hidden=true;$('weather-refresh').disabled=!journey||!services?.weather.enabled;$('weather-window').textContent=journey?`Next ${duration} min · Williamsburg`:'Departing now · Williamsburg';message('weather-status',journey?'Preparing the forecast for your trip…':'Enter your available time to see the forecast.');if(configured&&journey&&services.weather.enabled)weatherTimer=setTimeout(weather,400);}
  }
  $('place-search-form').addEventListener('submit',search);$('search-query').addEventListener('input',clearSearch);$('search-target').addEventListener('change',clearSearch);
  $('weather-refresh').addEventListener('click',weather);$('reference-refresh').addEventListener('click',reference);
  $('ors-settings-form').addEventListener('submit',settings);$('ors-disconnect').addEventListener('click',event=>settings(event,true));
  return {
    configure(config){try{services=serviceConfig(config.external_services||config);configured=true;renderServices();if(journey&&services.weather.enabled)void weather();else if(!services.weather.enabled)message('weather-status','Weather is turned off in settings.');}catch{configured=false;services=null;for(const id of ['search-submit','weather-refresh','reference-refresh','ors-connect','ors-disconnect'])$(id).disabled=true;message('weather-status','Could not read service settings. Route recommendations are still available.',true);}},
    invalidateJourney,
    clearSearch,
  };
}
