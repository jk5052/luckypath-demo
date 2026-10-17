// Small pure helpers shared by the browser and focused Node checks.
export const PRESETS = {
  green: {open:5,green:10,still:5,social:4,access:4},
  quiet: {open:4,green:6,still:10,social:3,access:4},
  open: {open:10,green:5,still:4,social:5,access:5},
};
export const TIME_BUDGET_DEFAULTS = {
  max_minutes:30,walking_speed_kmh:4.5,buffer_minutes:3,stop_minutes:0,
};
export function parseTimeBudget(values,hasVia=false) {
  const fields=[['max_minutes',1,240,'Available time'],['walking_speed_kmh',2,6,'Walking pace'],
    ['buffer_minutes',0,30,'Time buffer'],['stop_minutes',0,120,'Time at your stop']];
  const result={};
  for(const [key,min,max,label] of fields){
    if(key==='stop_minutes'&&!hasVia){result[key]=0;continue;}
    const raw=values?.[key],value=typeof raw==='string'&&raw.trim()!==''?Number(raw):raw;
    if(typeof value!=='number'||!Number.isFinite(value)||value<min||value>max)
      throw Error(`${label} must be a number between ${min} and ${max}.`);
    result[key]=value;
  }
  return result;
}
export function formatMinutes(value) {
  return typeof value==='number'&&Number.isFinite(value)&&value>=0?`${Math.ceil(value)} min`:'—';
}
export function routeBudgetView(result) {
  const budget=result?.budget,status=budget?.status;
  if(!['feasible','infeasible'].includes(status)||!result?.shortest?.route)
    throw Error('Could not read the route timing. Please try again.');
  const feasible=status==='feasible',tolerance=1e-9;
  const valid=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
  const consistentEstimate=(route,expected)=>{
    const estimate=route?.time_estimate;
    if(estimate==null)return true;
    if(!valid(estimate.total_minutes)||Math.abs(estimate.total_minutes-expected)>tolerance)return false;
    const parts=['walking_minutes','connector_minutes','stop_minutes','buffer_minutes'];
    const provided=parts.filter(key=>key in estimate);
    if(provided.some(key=>!valid(estimate[key])))return false;
    return provided.length!==parts.length||Math.abs(parts.reduce((sum,key)=>sum+estimate[key],0)-estimate.total_minutes)<=tolerance;
  };
  if(!valid(budget.minimum_required_minutes)||!valid(budget.max_minutes)||
      (feasible&&(!result.personalized?.route||!valid(budget.estimated_total_minutes)))||
      (!feasible&&result.personalized!==null)||
      (feasible&&(budget.minimum_required_minutes>budget.max_minutes+tolerance||budget.estimated_total_minutes>budget.max_minutes+tolerance))||
      !consistentEstimate(result.shortest,budget.minimum_required_minutes)||
      (feasible&&!consistentEstimate(result.personalized,budget.estimated_total_minutes)))
    throw Error('Could not verify the estimated time. Please try again.');
  const minimum=Math.ceil(budget.minimum_required_minutes);
  return {feasible,minimum_minutes:minimum,
    display_minutes:feasible?formatMinutes(budget.estimated_total_minutes):formatMinutes(budget.minimum_required_minutes),
    increase_to_minutes:!feasible&&minimum>budget.max_minutes&&minimum<=240?minimum:null,
    message:feasible?`Fits your ${formatMinutes(budget.max_minutes)} budget. Actual travel time may vary.`:
      `Allow at least ${formatMinutes(budget.minimum_required_minutes)}. Add time or change your start, destination, or stop.`,
  };
}
export const READING_ROUTE_KEYS = ['green','quiet','open_sky','vibrant'];
export function readingRouteWeights(value) {
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==4||
    !READING_ROUTE_KEYS.every(key=>typeof value[key]==='number'&&Number.isFinite(value[key])&&value[key]>=0&&value[key]<=1)||
    Math.abs(READING_ROUTE_KEYS.reduce((sum,key)=>sum+value[key],0)-1)>1e-8)
    throw Error('The reading weights are invalid. Recalculate your profile.');
  return Object.fromEntries(READING_ROUTE_KEYS.map(key=>[key,value[key]]));
}
export function routeRequestSnapshot({origin,destination,scores,via,time_budget,reading_weights}) {
  const request={origin:[...origin],destination:[...destination],profile_scores:{...scores},
    lucky_limit:0,time_budget:parseTimeBudget(time_budget,!!via)};
  if(reading_weights!=null)request.reading_weights=readingRouteWeights(reading_weights);
  if(via)request.via=[...via.lonlat];
  return request;
}
export function routeDownload(result,request,source) {
  // Read the exact successful request, never live controls or birth inputs.
  const profile_source=['independent-analyses','route-maker-reading'].includes(source?.kind)?{
    kind:source.kind,analysis_version:source.analysis_version,saju_policy:source.saju_policy,
    astrology_policy:source.astrology_policy,fusion_policy:source.fusion_policy,
  }:{kind:'manual'};
  return {...result.geojson,properties:{...result.geojson?.properties,
    model_version:result.model_version,profile_source,profile_scores:{...request.profile_scores},
    ...(request.reading_weights?{reading_weights:readingRouteWeights(request.reading_weights)}:{}),
    origin:[...request.origin],destination:[...request.destination],via:request.via?[...request.via]:null,
    time_budget:{...request.time_budget},budget:result.budget,
    time_estimates:{personalized:result.personalized?.time_estimate??null,shortest:result.shortest?.time_estimate??null},
    distances_exclude_snap_connectors:true,empirical_quality_verified:false,
  }};
}
export function insideBounds(point,bbox) {
  return Array.isArray(point)&&point.length===2&&point.every(Number.isFinite)&&point[0]>=bbox[0]&&point[0]<=bbox[2]&&point[1]>=bbox[1]&&point[1]<=bbox[3];
}
export function parseLatLon(text,bbox) {
  const parts=text.split(',').map(s=>s.trim());
  if(parts.length!==2||parts.some(s=>s==='')) throw Error('Enter latitude, longitude separated by a comma.');
  const point=[Number(parts[1]),Number(parts[0])];
  if(!insideBounds(point,bbox)) throw Error('Choose coordinates inside the dashed Williamsburg boundary.');
  return point;
}
export function formatDistance(metres) {
  if(!Number.isFinite(metres)||metres<0) return {value:'—',unit:'m'};
  return metres>=1000?{value:(metres/1000).toFixed(2),unit:'km'}:{value:String(Math.round(metres)),unit:'m'};
}
export function distanceText(metres) { const d=formatDistance(metres);return `${d.value} ${d.unit}`; }
export function isKeyboardEditable(element) {
  if(!element||element.disabled||element.readOnly)return false;
  if(element.isContentEditable)return true;
  const tag=element.tagName?.toLowerCase();
  if(tag==='textarea')return true;
  return tag==='input'&&!['button','checkbox','color','file','hidden','image','radio','range','reset','submit'].includes(element.type);
}
export function viewportLayout({layoutHeight,visualHeight,scale=1,editableFocused=false}) {
  // Both pinch zoom and a keyboard shrink the visual viewport. Only an editable
  // focus at normal zoom should reflow the app around a keyboard.
  const unzoomed=Number.isFinite(scale)&&Math.abs(scale-1)<.01;
  const keyboardOpen=!!editableFocused&&unzoomed&&Number.isFinite(visualHeight)&&
    visualHeight>0&&visualHeight<layoutHeight*.78;
  return {height:keyboardOpen?visualHeight:layoutHeight,keyboardOpen};
}
export function createRevisionGate() {
  let revision=0;
  return {invalidate:()=>++revision,current:()=>revision,isCurrent:token=>token===revision};
}
export function routeExplanation(result,scores,reading_weights=null) {
  if(result.budget?.status==='infeasible') return 'No route fits your available time. The shortest route is shown for reference and also exceeds your budget.';
  if(!result.personalized) return 'Could not calculate a route. Check your selections and try again.';
  const viaNote=result.explicit_via?' Both routes pass near your selected stop.':'';
  if(result.personalized.length_m<1e-6) return 'Your start and destination share the same walking point. Try another destination.'+viaNote;
  const keys=reading_weights?READING_ROUTE_KEYS:['open','green','still'],names={open:'openness',green:'greenery',still:'quiet',quiet:'quiet',open_sky:'openness',vibrant:'street activity'};
  if(reading_weights)scores=reading_weights;
  const active=keys.filter(k=>scores[k]>(reading_weights?0:1)).sort((a,b)=>scores[b]-scores[a]);
  if(!active.length) return 'All route preferences are set to 1, so this route prioritizes distance.'+viaNote;
  const same=JSON.stringify(result.shortest.route.geometry)===JSON.stringify(result.personalized.route.geometry);
  if(same) return 'Your recommended and shortest routes are the same. Change your preferences or endpoints to compare.'+viaNote;
  const priority=active.filter(k=>scores[k]===scores[active[0]]).map(k=>names[k]).join(' + ');
  return reading_weights?`Your reading prioritizes ${priority} among four available street indicators, balanced with distance and your time budget.${viaNote}`:`This route gives more weight to ${priority}, balanced with distance.${viaNote}`;
}
export function friendlyError(error) {
  const text=String(error?.message||error);
  if(/snap distance|walking network|maximum snap/i.test(text)) return 'This point is too far from a walking path. Move it closer to a street.';
  if(/no (?:connected )?(?:walking )?route|disconnected|not connected|cannot connect|no path/i.test(text)) return 'No connected walking route was found. Move a point to a nearby street.';
  if(/bbox|study.*(?:area|bound)|outside|범위|구역/i.test(text)) return 'Choose a point inside the dashed Williamsburg boundary.';
  if(/fetch|networkerror|load failed|HTTP 5/i.test(text)) return 'Could not reach the server. Keep it running and try again.';
  return /[\u3131-\u318e\uac00-\ud7a3]/.test(text)?'Could not complete the request. Check your inputs and try again.':text;
}
