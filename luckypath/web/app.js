import {PRESETS,TIME_BUDGET_DEFAULTS,insideBounds,parseLatLon,formatDistance,distanceText,formatMinutes,parseTimeBudget,routeBudgetView,routeRequestSnapshot,routeDownload,createRevisionGate,routeExplanation,friendlyError,isKeyboardEditable,viewportLayout} from './model.js';
import {createProfileController} from './profile.js';
import {createExternalController} from './external.js';

const $=id=>document.getElementById(id);
const state={config:null,origin:null,destination:null,originName:'Near Bedford Ave',destinationName:'East River waterfront',via:null,scores:{...PRESETS.green},result:null,resultRequest:null,resultSource:null,pick:null,ready:false,view:'profile',pickReturn:'journey'};
const gate=createRevisionGate();
let profileUI,externalUI;
let map,endpointLayer,venueLayer,personalizedLayer,shortestLayer,connectorLayer,controller,timer,roadLayer,labels=[];
const experienceLabels={open:'Open space',green:'Greenery',still:'Quiet',social:'Social energy',access:'Amenities'};
const categoryNames={cafe:'Café',restaurant:'Restaurant',bakery:'Bakery',park:'Park',plaza:'Plaza',ice_cream_shop:'Ice cream',book_store:'Bookshop',art_gallery:'Gallery',museum:'Museum',market:'Market',gift_shop:'Gift shop',florist:'Florist',community_center:'Community space',performing_arts_theater:'Theater',movie_theater:'Cinema'};
const iconPaths={open:'M3 9h14M3 5v8m14-8v8M7 2l-4 3 4 3m6 2 4 3-4 3',green:'M4 15C1 8 6 3 17 2c1 10-3 16-10 14M4 18 14 7',still:'M3 13c3 0 3-6 6-6s3 6 6 6h3M5 17h10',social:'M7 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6m6 1a2.5 2.5 0 1 0 0-5M2 17v-3a5 5 0 0 1 10 0v3m3-6a4 4 0 0 1 3 4v2',access:'M3 8h14l-2-5H5L3 8Zm1 0v10h12V8M8 18v-6h4v6'};

function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function status(message,error=false){$('status').textContent=message;$('status').classList.toggle('error',error);if(state.pick&&error)$('pick-hint-text').textContent=message;}
function setView(view,{focus=false,fit=true}={}){
  if(!['profile','journey','explore','places'].includes(view))return;
  if(state.pick)setPick(null,false);
  const changed=state.view!==view;state.view=view;document.body.dataset.view=view;document.title=`LuckyPath — ${{profile:'Your profile',journey:'Your trip',explore:'Your route',places:'Places nearby'}[view]}`;
  for(const panel of document.querySelectorAll('[data-panel]'))panel.hidden=panel.dataset.panel!==view;
  $('controls').hidden=view==='explore';
  for(const button of document.querySelectorAll('[data-view-target]'))button.setAttribute('aria-pressed',String(button.dataset.viewTarget===(view==='places'?'explore':view)));
  if(changed)$('controls').scrollTop=0;
  requestAnimationFrame(()=>{map?.invalidateSize({pan:false});if(fit)fitRoute();if(focus){const target=view==='explore'?$('fit-map'):$(`${view}-view`).querySelector('h1');if(view!=='explore')target?.setAttribute('tabindex','-1');target?.focus({preventScroll:true});}});
}
function showIdentity({name,saved}){
  $('profile-greeting').textContent='Your profile';
  $('identity-state').textContent=saved?'Saved in this browser':'Saju + Astrology';$('identity-state').classList.toggle('saved',saved);
  const avatar=$('profile-avatar');if(avatar)avatar.textContent=name?Array.from(name.trim())[0].toLocaleUpperCase('en'):'✦';
}
function latlng(point){return [point[1],point[0]];}
function coordText(point){return `${point[1].toFixed(5)}, ${point[0].toFixed(5)}`;}
function iconFor(key){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 20 20');svg.setAttribute('class','slider-icon');svg.setAttribute('aria-hidden','true');const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',iconPaths[key]);svg.append(path);return svg;}
function renderSliders(){for(const key of Object.keys(experienceLabels)){const row=el('div','slider-row'),heading=el('div','slider-heading'),label=el('label','',experienceLabels[key]),output=el('output','',state.scores[key]),input=el('input');label.htmlFor=`slider-${key}`;label.prepend(iconFor(key));input.type='range';input.id=`slider-${key}`;input.min='1';input.max='10';input.step='1';input.value=state.scores[key];input.setAttribute('aria-label',`${experienceLabels[key]} priority`);output.id=`value-${key}`;output.htmlFor=input.id;input.addEventListener('input',()=>{profileUI?.manual();state.scores[key]=Number(input.value);updateSliders();invalidate();});heading.append(label,output);row.append(heading,input);$(key==='social'||key==='access'?'place-sliders':'route-sliders').append(row);}updateSliders();}
function updateSliders(){for(const key of Object.keys(experienceLabels)){const input=$(`slider-${key}`);input.value=state.scores[key];input.style.setProperty('--fill',`${(state.scores[key]-1)/9*100}%`);$(`value-${key}`).textContent=state.scores[key];}for(const button of document.querySelectorAll('[data-preset]')){const active=Object.keys(state.scores).every(k=>state.scores[k]===PRESETS[button.dataset.preset][k]);button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}}
function fillLocationSelect(which){const select=$(`${which}-location`),current=el('option','',state[`${which}Name`]);current.value='current';select.replaceChildren(current);const group=el('optgroup');group.label='Neighborhood places';for(const place of state.config.locations){const option=el('option','',place.name);option.value=place.id;group.append(option);}select.append(group);select.value='current';$(`${which}-coordinates`).textContent=coordText(state[which]);$(`${which}-coordinate-input`).value=coordText(state[which]);}
function updateEndpoints(){fillLocationSelect('origin');fillLocationSelect('destination');$('via-row').hidden=!state.via;$('via-name').textContent=state.via?.name||'';$('stop-time-row').hidden=!state.via;$('stop-minutes').disabled=!state.via;if(!state.via)$('stop-minutes').value='0';renderEndpoints();}
function pin(letter,kind){return L.divIcon({className:`endpoint-pin ${kind}`,html:letter,iconSize:[29,29],iconAnchor:[14.5,14.5]});}
function renderEndpoints(){if(!map)return;endpointLayer.clearLayers();for(const [key,letter] of [['origin','A'],['destination','B']]){const marker=L.marker(latlng(state[key]),{icon:pin(letter,key),draggable:true,autoPan:true,title:`${key==='origin'?'From':'To'}: ${state[`${key}Name`]}`,keyboard:true}).addTo(endpointLayer);marker.bindTooltip(el('span','',`${letter} · ${state[`${key}Name`]}`));marker.on('dragend',()=>{const p=marker.getLatLng(),point=[p.lng,p.lat];if(!insideBounds(point,state.config.study_bbox_wgs84)){status('Choose your start and destination inside the dotted boundary.',true);renderEndpoints();return;}state[key]=point;state[`${key}Name`]='Selected on map';updateEndpoints();invalidate();});}if(state.via)L.marker(latlng(state.via.lonlat),{icon:pin('·','via'),title:`Stop: ${state.via.name}`}).bindTooltip(el('span','',state.via.name)).addTo(endpointLayer);}
function setPick(which,restore=true){
  const wasPicking=!!state.pick;
  if(which&&!wasPicking)state.pickReturn=state.view;
  state.pick=which;document.body.dataset.picking=String(!!which);
  $('pick-hint').hidden=!which;$('pick-hint-text').textContent=which==='origin'?'Tap the map to choose your start.':'Tap the map to choose your destination.';
  document.querySelector('.map-section').classList.toggle('picking',!!which);
  for(const key of ['origin','destination']){$(`pick-${key}`).classList.toggle('active',key===which);$(`pick-${key}`).setAttribute('aria-pressed',String(key===which));}
  $('controls').hidden=!!which||state.view==='explore';
  if(which){document.activeElement?.blur();requestAnimationFrame(()=>{fitRoute();$('cancel-pick').focus({preventScroll:true});});}
  else if(wasPicking&&restore)setView(state.pickReturn,{fit:false});
}
function syncExternalJourney(){let request=null;try{request={origin:state.origin,destination:state.destination,via:state.via?.lonlat,time_budget:readTimeBudget()};}catch{}externalUI?.invalidateJourney(request);}
function invalidate(message='Updating your route…',auto=true){
  syncExternalJourney();
  gate.invalidate();controller?.abort();clearTimeout(timer);state.result=null;state.resultRequest=null;state.resultSource=null;
  $('download').disabled=true;$('result-card').hidden=true;
  $('places-list').replaceChildren(el('p','empty-copy','Finding places near your route…'));$('places-count').textContent='—';
  for(const layer of [shortestLayer,personalizedLayer,connectorLayer,venueLayer])layer?.clearLayers();
  if(state.ready){status(message);$('calculate').disabled=false;$('calculate').firstElementChild.textContent='Find my route';if(auto)timer=setTimeout(()=>calculate(false),500);}
}
async function api(path,options){const response=await fetch(path,options);let body;try{body=await response.json();}catch{throw Error(`HTTP ${response.status}: Could not read the response.`);}if(!response.ok)throw Error(body.error||`HTTP ${response.status}`);return body;}
function readTimeBudget(){
  return parseTimeBudget({max_minutes:$('max-minutes').value,walking_speed_kmh:$('walking-speed').value,buffer_minutes:$('buffer-minutes').value,stop_minutes:$('stop-minutes').value},!!state.via);
}
function setTimeBudgetFields(values=TIME_BUDGET_DEFAULTS){
  const budget={...TIME_BUDGET_DEFAULTS,...values};
  for(const[id,key]of [['max-minutes','max_minutes'],['walking-speed','walking_speed_kmh'],['buffer-minutes','buffer_minutes'],['stop-minutes','stop_minutes']])$(id).value=String(budget[key]);
}
async function calculate(fit=false){
  if(!state.ready)return;
  clearTimeout(timer);const token=gate.invalidate();controller?.abort();controller=new AbortController();
  state.result=null;state.resultRequest=null;state.resultSource=null;
  $('download').disabled=true;$('calculate').disabled=true;$('calculate').firstElementChild.textContent='Finding your route…';$('result-card').hidden=true;
  for(const layer of [shortestLayer,personalizedLayer,connectorLayer,venueLayer])layer.clearLayers();
  status('Matching your time and preferences…');
  try{
    const request=routeRequestSnapshot({...state,time_budget:readTimeBudget()});
    const requestSource=profileUI.routeSource();
    const result=await api('/explore/route',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request),signal:controller.signal});
    if(!gate.isCurrent(token))return;
    const budget=routeBudgetView(result);
    state.result=result;state.resultRequest=request;state.resultSource=requestSource;
    renderResult(result,request);if(fit)fitRoute();
    status(budget.feasible?'Your route is ready. Open Map to see it.':'This trip needs more time. Check the minimum estimate.',!budget.feasible);
  }catch(error){
    if(error.name==='AbortError'||!gate.isCurrent(token))return;
    state.result=null;state.resultRequest=null;state.resultSource=null;$('result-card').hidden=true;
    for(const layer of [shortestLayer,personalizedLayer,connectorLayer,venueLayer])layer.clearLayers();
    $('places-list').replaceChildren(el('p','empty-copy','Find a route to see nearby places.'));$('places-count').textContent='—';status(friendlyError(error),true);
  }finally{if(gate.isCurrent(token)){$('calculate').disabled=false;$('calculate').firstElementChild.textContent='Find my route';}}
}
function renderResult(result,request){
  const view=routeBudgetView(result),route=view.feasible?result.personalized:result.shortest;
  for(const layer of [shortestLayer,personalizedLayer,connectorLayer])layer.clearLayers();
  L.geoJSON(result.shortest.route,{style:{color:'#2e4553',weight:7,opacity:.75},interactive:false}).addTo(shortestLayer);
  L.geoJSON(result.shortest.route,{style:{color:'#a8d0e1',weight:3,dashArray:'6 7',opacity:.9},interactive:false}).addTo(shortestLayer);
  if(view.feasible){
    L.geoJSON(result.personalized.route,{style:{color:'#203542',weight:9,opacity:.9},interactive:false}).addTo(personalizedLayer);
    L.geoJSON(result.personalized.route,{style:{color:'#e4ff5a',weight:4,opacity:1},interactive:false}).addTo(personalizedLayer);
  }
  updateLayerVisibility();let maxGap=0;
  for(const snap of Object.values(route.snaps||{})){const point=snap.input_lonlat,snapped=snap.lonlat,gap=snap.distance_m;maxGap=Math.max(gap,maxGap);if(gap>2)L.polyline([latlng(point),latlng(snapped)],{color:'#b39874',weight:2,dashArray:'3 5',interactive:false}).addTo(connectorLayer);}
  const d=formatDistance(result.personalized?.length_m);
  $('recommended-distance').textContent=d.value;$('recommended-unit').textContent=d.unit;
  $('result-title').textContent=view.feasible?'Your route':'More time needed';
  $('time-heading').textContent=view.feasible?'Estimated total time':'Minimum estimated time';
  $('estimated-time').textContent=view.display_minutes;
  $('budget-message').textContent=view.message;
  $('increase-budget').hidden=view.increase_to_minutes===null;
  $('increase-budget').textContent=view.increase_to_minutes===null?'':`Use ${view.increase_to_minutes} min and recalculate`;
  $('shortest-distance').textContent=distanceText(result.shortest.length_m);
  $('shortest-time').textContent=formatMinutes(result.shortest.time_estimate?.total_minutes);
  const delta=result.extra_distance_m,percent=result.comparison?.extra_distance_percent;
  $('extra-distance').textContent=!view.feasible?'Over your time limit':delta<.5?'Same distance':`+${distanceText(delta)}${Number.isFinite(percent)?` · ${percent.toFixed(1)}% farther`:''}`;
  document.querySelector('.shortest-stat>span').textContent=!view.feasible?'Shortest · reference':request.via?'Shortest via your stop':'Shortest route';
  $('route-reason').textContent=routeExplanation(result,request.profile_scores);
  $('snap-note').textContent=(maxGap>20?`The largest gap between a selected point and the path is ${Math.round(maxGap)} m. `:'')+'Distance covers the mapped path; estimated time also includes connections to your selected points.';
  $('result-state').textContent=view.feasible?'Within your time':'Over time';
  $('result-card').classList.toggle('infeasible',!view.feasible);$('result-card').hidden=false;$('download').disabled=false;
  renderTimeBreakdown(route.time_estimate,!view.feasible);renderMetrics(result.comparison);renderVenues(view.feasible?result.venue_candidates||[]:[],view.feasible);
}
function renderTimeBreakdown(estimate,reference=false){
  const box=$('time-breakdown');box.replaceChildren();
  if(!estimate){box.append(el('p','','Time breakdown unavailable.'));return;}
  const minutes=value=>Number.isFinite(value)?`${value.toFixed(1)} min`:'—';
  box.append(el('p','time-detail-caption',`${reference?'Shortest route reference':'Recommended route'} · Walking at ${estimate.walking_speed_kmh} km/h`));
  const list=el('dl','time-detail-list');
  for(const[label,value]of [['On the path',estimate.walking_minutes],['Connections to the path',estimate.connector_minutes],['Time at your stop',estimate.stop_minutes],['Buffer',estimate.buffer_minutes]])list.append(el('dt','',label),el('dd','',minutes(value)));
  box.append(list,el('p','',`Path ${distanceText(estimate.network_distance_m)} + connections ${distanceText(estimate.connector_distance_m)}. Rounded values may not add up exactly.`));
}
function renderMetrics(comparison){const wrap=el('div','metric-table');wrap.append(el('p','metric-table-caption','Recommended / shortest · model indicators, 0–100'));for(const key of ['open','green','still']){const row=el('div','metric-line'),bars=el('div','metric-bars'),values=[];row.append(el('span','',experienceLabels[key]));for(const kind of ['personalized','shortest']){const value=comparison?.[kind]?.model_indicators?.[key];values.push(Number.isFinite(value)?String(Math.round(value*100)):'—');const track=el('div','metric-bar-track'),bar=el('div',`metric-bar ${kind}`);bar.style.width=`${Number.isFinite(value)?Math.max(0,Math.min(100,value*100)):0}%`;track.append(bar);bars.append(track);}row.append(bars,el('span','metric-value',values.join(' / ')));wrap.append(row);}$('metric-comparison').replaceChildren(wrap);}
function chooseVenue(venue){setView('journey',{focus:true,fit:false});state.via={name:venue.name,lonlat:[...venue.lonlat],place_id:venue.place_id};updateEndpoints();map.closePopup();invalidate('Updating both routes to include your stop…');}
function renderVenues(venues,feasible=true){venueLayer.clearLayers();const list=$('places-list');list.replaceChildren();const shown=venues.slice(0,6);$('places-count').textContent=shown.length;if(!shown.length){list.append(el('p','empty-copy',feasible?'No matching places in our records near this route. Other places may still exist.':'Nearby places appear once a route fits your time.'));return;}for(const [i,venue]of shown.entries()){const row=el('article','place-card'),body=el('div'),button=el('button','',state.via?.place_id===venue.place_id?'Added':'Add stop');body.append(el('h3','place-name',venue.name),el('p','place-meta',`${categoryNames[venue.category]||'Local place'} · ${Math.round(venue.distance_to_route_m)} m from route, straight-line`));button.disabled=state.via?.place_id===venue.place_id;button.setAttribute('aria-label',`Add ${venue.name} as a stop`);button.addEventListener('click',()=>chooseVenue(venue));row.append(el('span','place-number',i+1),body,button);list.append(row);const icon=L.divIcon({className:'venue-pin',html:String(i+1),iconSize:[22,22],iconAnchor:[11,11]});const marker=L.marker(latlng(venue.lonlat),{icon,title:venue.name}).addTo(venueLayer),popup=el('div'),add=el('button','popup-button','Add this stop');popup.append(el('p','popup-title',venue.name),el('p','popup-meta',`${categoryNames[venue.category]||'Local place'} · Check hours and entrance`),add);add.addEventListener('click',()=>chooseVenue(venue));marker.bindPopup(popup);}updateLayerVisibility();}
function updateLayerVisibility(){for(const[id,layer]of [['show-shortest',shortestLayer],['show-personalized',personalizedLayer],['show-venues',venueLayer]]){if(!layer)return;$(id).checked?layer.addTo(map):map.removeLayer(layer);}}
function fitRoute(){
  if(!map||!state.config)return;
  const mobile=matchMedia('(max-width:760px)').matches,size=map.getSize();
  if(!state.pick&&state.view!=='explore'){
    map.setView(latlng(state.origin),16,{animate:false});
    return;
  }
  let left=34,top=mobile?150:100,right=mobile?42:85,bottom=mobile?100:105;
  if(!state.pick){
    if(mobile){
      const box=state.view==='explore'?$('map-results'):$('controls');
      if(!box.hidden){const r=box.getBoundingClientRect();bottom=Math.max(bottom,size.y-r.top+20);}
    }else if(state.view!=='explore')left=Math.min(size.x*.55,$('controls').getBoundingClientRect().right+45);
    else left=Math.min(size.x*.55,$('map-results').getBoundingClientRect().right+45);
  }
  top=Math.min(top,size.y*.25);bottom=Math.min(bottom,size.y-top-90);
  const padding={paddingTopLeft:[left,top],paddingBottomRight:[right,Math.max(20,bottom)],maxZoom:17,animate:false};
  const route=state.result?.personalized||state.result?.shortest;
  if(route)map.fitBounds(L.latLngBounds(route.route.geometry.coordinates.map(latlng)),padding);
  else{const b=state.config.study_bbox_wgs84;map.fitBounds([[b[1],b[0]],[b[3],b[2]]],padding);}
}
function buildMap(context){const bbox=context.metadata?.display_bbox||state.config.study_bbox_wgs84,bounds=L.latLngBounds([[bbox[1],bbox[0]],[bbox[3],bbox[2]]]);map=L.map('map',{zoomControl:false,preferCanvas:true,minZoom:14,maxZoom:19,zoomSnap:.25,maxBounds:bounds.pad(.75),maxBoundsViscosity:.8,attributionControl:true});L.control.zoom({position:'bottomright'}).addTo(map);L.control.scale({position:'bottomleft',imperial:false,maxWidth:80}).addTo(map);map.attributionControl.setPrefix(false);map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> · NYC Open Data');const styles={land:{fillColor:'#3e5865',fillOpacity:1,color:'#5c7681',weight:.7},park:{fillColor:'#41635f',fillOpacity:.9,color:'#5a7b73',weight:.6},building:{fillColor:'#526d78',fillOpacity:.55,color:'#75909a',weight:.35}};for(const[kind,index]of [['land',201],['park',210],['building',220],['road',230],['label',240],['boundary',250]]){map.createPane(kind).style.zIndex=index;map.getPane(kind).style.pointerEvents='none';}for(const kind of ['land','park','building'])L.geoJSON({type:'FeatureCollection',features:context.features.filter(f=>f.properties.kind===kind)},{pane:kind,style:styles[kind],interactive:false}).addTo(map);const roadStyle=f=>{const c=f.properties.class;return {color:['footway','path','steps','pedestrian'].includes(c)?'#72958f':'#93acb6',weight:['primary','secondary','tertiary'].includes(c)?2.8:['residential','unclassified','service'].includes(c)?1.6:.9,opacity:.75};};roadLayer=L.geoJSON({type:'FeatureCollection',features:context.features.filter(f=>f.properties.kind==='road')},{pane:'road',style:roadStyle,interactive:false}).addTo(map);for(const f of context.features.filter(f=>f.properties.kind==='label')){const park=f.properties.label_kind==='park',label=el('span',park?'street-label park-label':'street-label',f.properties.name),icon=L.divIcon({html:label,className:'',iconSize:park?[100,30]:[120,12],iconAnchor:park?[50,15]:[60,6]});labels.push(L.marker(latlng(f.geometry.coordinates),{icon,pane:'label',interactive:false,keyboard:false}).addTo(map));}const study=state.config.study_bbox_wgs84;L.rectangle([[study[1],study[0]],[study[3],study[2]]],{pane:'boundary',fill:false,color:'#9eb9b8',weight:.8,dashArray:'4 7',interactive:false}).addTo(map);shortestLayer=L.layerGroup().addTo(map);personalizedLayer=L.layerGroup().addTo(map);connectorLayer=L.layerGroup().addTo(map);venueLayer=L.layerGroup().addTo(map);endpointLayer=L.layerGroup().addTo(map);map.on('click',event=>{if(!state.pick)return;const point=[event.latlng.lng,event.latlng.lat];if(!insideBounds(point,study)){status('Choose a point inside the dotted boundary.',true);return;}const which=state.pick;state[which]=point;state[`${which}Name`]='Selected on map';setPick(null);updateEndpoints();invalidate();});map.on('zoomend',()=>{map.getPane('label').style.opacity=map.getZoom()<14.8?'0':'1';});fitRoute();let previousWidth=0;new ResizeObserver(entries=>{map.invalidateSize({pan:false});const width=entries[0].contentRect.width;if(previousWidth&&Math.abs(width-previousWidth)>20)fitRoute();previousWidth=width;}).observe($('map'));}
function wireEvents(){
  for(const button of document.querySelectorAll('[data-view-target]'))button.addEventListener('click',()=>setView(button.dataset.viewTarget,{focus:true}));
  $('skip-profile').addEventListener('click',()=>setView('journey',{focus:true}));
  $('edit-journey').addEventListener('click',()=>setView('journey',{focus:true}));
  $('open-places').addEventListener('click',()=>setView('places',{focus:true}));
  $('back-to-map').addEventListener('click',()=>setView('explore',{focus:true}));
  $('skip-controls').addEventListener('click',event=>{event.preventDefault();setView(state.view==='explore'?'journey':state.view,{focus:true,fit:false});});
  const viewport=window.visualViewport;
  if(viewport){
    const updateViewport=()=>{
      const layout=viewportLayout({layoutHeight:window.innerHeight,visualHeight:viewport.height,
        scale:viewport.scale,editableFocused:isKeyboardEditable(document.activeElement)});
      document.documentElement.style.setProperty('--app-height',`${layout.height}px`);
      document.body.classList.toggle('keyboard-open',layout.keyboardOpen);
    };
    viewport.addEventListener('resize',updateViewport);
    window.addEventListener('resize',updateViewport);
    document.addEventListener('focusin',updateViewport);
    // Wait for focus to settle so switching fields does not briefly hide the keyboard layout.
    document.addEventListener('focusout',()=>requestAnimationFrame(updateViewport));
    updateViewport();
  }
  for(const id of ['max-minutes','walking-speed','buffer-minutes','stop-minutes'])$(id).addEventListener('input',()=>invalidate('Updating your route for the new time…'));$('increase-budget').addEventListener('click',()=>{if(!state.result)return;const minutes=routeBudgetView(state.result).increase_to_minutes;if(minutes===null)return;$('max-minutes').value=String(minutes);invalidate('Updating your route with more time…');});for(const button of document.querySelectorAll('[data-preset]'))button.addEventListener('click',()=>{profileUI.manual();state.scores={...PRESETS[button.dataset.preset]};updateSliders();invalidate();});for(const which of ['origin','destination']){$(`${which}-location`).addEventListener('change',event=>{const place=state.config?.locations.find(p=>p.id===event.target.value);if(!place)return;state[which]=[...place.lonlat];state[`${which}Name`]=place.name;updateEndpoints();invalidate();});$(`pick-${which}`).addEventListener('click',()=>{if(state.ready)setPick(state.pick===which?null:which);});}$('swap').addEventListener('click',()=>{if(!state.ready)return;[state.origin,state.destination]=[state.destination,state.origin];[state.originName,state.destinationName]=[state.destinationName,state.originName];updateEndpoints();invalidate();});$('remove-via').addEventListener('click',()=>{state.via=null;updateEndpoints();invalidate();});$('cancel-pick').addEventListener('click',()=>setPick(null));document.addEventListener('keydown',event=>{if(event.key==='Escape')setPick(null);});$('calculate').addEventListener('click',async()=>{await calculate(false);if(state.result)setView('explore',{focus:true});});$('fit-map').addEventListener('click',fitRoute);$('reset').addEventListener('click',()=>{if(!state.ready)return;profileUI.clear();setView('profile',{focus:true,fit:false});const r=state.config.initial_request;state.origin=[...r.origin];state.destination=[...r.destination];state.originName='Near Bedford Ave';state.destinationName='East River waterfront';state.scores={...r.profile_scores};state.via=null;setTimeBudgetFields(r.time_budget||state.config.time_budget?.defaults);setPick(null);updateSliders();updateEndpoints();invalidate('',false);calculate(true);});$('coordinate-form').addEventListener('submit',event=>{event.preventDefault();if(!state.ready)return;try{const origin=parseLatLon($('origin-coordinate-input').value,state.config.study_bbox_wgs84),destination=parseLatLon($('destination-coordinate-input').value,state.config.study_bbox_wgs84);state.origin=origin;state.destination=destination;state.originName='Selected coordinates';state.destinationName='Selected coordinates';updateEndpoints();invalidate();}catch(error){status(error.message,true);}});for(const id of ['show-personalized','show-shortest','show-venues'])$(id).addEventListener('change',updateLayerVisibility);$('download').addEventListener('click',()=>{if(!state.result||!state.resultRequest)return;const json=routeDownload(state.result,state.resultRequest,state.resultSource),url=URL.createObjectURL(new Blob([JSON.stringify(json,null,2)],{type:'application/geo+json'})),link=el('a');link.href=url;link.download='luckypath-williamsburg.geojson';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});$('about-button').addEventListener('click',()=>$('about-dialog').showModal());$('about-dialog').addEventListener('click',event=>{if(event.target===$('about-dialog')){const r=$('about-dialog').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$('about-dialog').close();}});}
async function init(){externalUI=createExternalController({api,onChoose(which,place){if(!state.ready||!['origin','destination'].includes(which))return false;if(!insideBounds(place.lonlat,state.config.study_bbox_wgs84)){status('This place is outside the supported area. Choose inside the dotted boundary.',true);return false;}state[which]=[...place.lonlat];state[`${which}Name`]=place.name;setPick(null);updateEndpoints();invalidate('Updating your route to this place…');return true;}});profileUI=createProfileController({api,onIdentity:showIdentity,onApply(scores){state.scores=scores;updateSliders();invalidate('Applying your Saju and astrology preferences…');setView('journey',{focus:true});}});renderSliders();wireEvents();try{const[config,context]=await Promise.all([api('/explore/config'),api('/explore/context')]);if(!window.L)throw Error('Could not load the map. Please refresh.');state.config=config;state.origin=[...config.initial_request.origin];state.destination=[...config.initial_request.destination];state.scores={...config.initial_request.profile_scores};setTimeBudgetFields(config.initial_request.time_budget||config.time_budget?.defaults);updateSliders();buildMap(context);updateEndpoints();state.ready=true;profileUI.configure(config.profile);externalUI.configure(config);syncExternalJourney();$('map-loading').hidden=true;$('calculate').disabled=false;await calculate(true);}catch(error){status(friendlyError(error),true);$('map-loading').replaceChildren(el('strong','','Could not load the map'),el('span','',friendlyError(error)));const retry=el('button','small-button','Try again');retry.addEventListener('click',()=>location.reload());$('map-loading').append(retry);}}
init();
