import {createRevisionGate,readingRouteWeights} from './model.js';

const $=id=>document.getElementById(id);
const names={open:'Openness',green:'Greenery',still:'Quiet',social:'Social',access:'Amenities'};
const systems={saju:'Saju',astrology:'Astrology'};
const events={encounter:'Encounter',opportunity:'Opportunity',discovery:'Discovery'};
const qualities={activation:'Activation',receptivity:'Receptivity',clarity:'Clarity',expansion:'Expansion',grounding:'Grounding',flow:'Flow'};
const streetNames={green:'Greenery',water:'Water',sunlight:'Sunlight',open_sky:'Openness',quiet:'Quiet',vibrant:'Street activity',rest:'Rest',culture:'Culture'};
const percent=value=>Number.isFinite(value)?`${(value*100).toFixed(1)}%`:'—';
function readingSummary(reading){
  const event=Object.entries(reading?.combined?.event||{}).sort((a,b)=>b[1]-a[1])[0]?.[0];
  const top=Object.entries(reading?.combined?.quality||{}).sort((a,b)=>b[1]-a[1]).slice(0,2).map(([key])=>qualities[key]);
  return {event:events[event]||'Your reading',qualities:top.join(' + ')};
}

const applyLabel='Continue';
const storageKey='luckypath.profile.v1';
const savedFields=['name','birth_date','birth_time','birth_unknown','birth_timezone','birth_latitude','birth_longitude','birth_house_system','birth_fold'];
function dateValid(value,max){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||value<'1900-01-01'||value>(max||'2100-12-31'))return false;
  const date=new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime())&&date.toISOString().slice(0,10)===value;
}
function validSavedProfile(value,max){
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==savedFields.length||!savedFields.every(key=>Object.hasOwn(value,key)))return false;
  if(typeof value.name!=='string'||value.name.length>40||/[\u0000-\u001f\u007f]/.test(value.name)||!dateValid(value.birth_date,max)||typeof value.birth_unknown!=='boolean')return false;
  if(value.birth_unknown){if(value.birth_time!==null||value.birth_fold!==null)return false;}
  else if(typeof value.birth_time!=='string'||!/^([01]\d|2[0-3]):[0-5]\d$/.test(value.birth_time)||![null,0,1].includes(value.birth_fold))return false;
  if(typeof value.birth_timezone!=='string'||!value.birth_timezone||value.birth_timezone.length>80||value.birth_timezone!==value.birth_timezone.trim())return false;
  try{new Intl.DateTimeFormat('en',{timeZone:value.birth_timezone});}catch{return false;}
  const {birth_latitude:lat,birth_longitude:lon}=value;
  if(!(lat===null&&lon===null)&&!(Number.isFinite(lat)&&Number.isFinite(lon)&&lat>=-90&&lat<=90&&lon>=-180&&lon<=180))return false;
  return ['P','W'].includes(value.birth_house_system);
}
function node(tag,className,text){const element=document.createElement(tag);if(className)element.className=className;if(text!==undefined)element.textContent=text;return element;}
function number(value){return Number.isFinite(value)?value.toFixed(2):'—';}
function table(caption,columns,rows,className='analysis-table'){
  const wrap=node('div','analysis-table-wrap'),element=node('table',className);
  element.append(node('caption','',caption));
  const head=node('thead'),headRow=node('tr');
  for(const label of columns){const th=node('th','',label);th.scope='col';headRow.append(th);}
  head.append(headRow);element.append(head);
  const body=node('tbody');for(const values of rows){const row=node('tr');values.forEach((value,index)=>{const cell=node(index===0?'th':'td','',value??'—');if(index===0)cell.scope='row';row.append(cell);});body.append(row);}
  element.append(body);wrap.append(element);return wrap;
}
function validResponse(result){
  return result?.schema_version===2&&result.profile_version==='route-maker-reading-v1'&&result.systems?.saju&&result.systems?.astrology&&result.target?.date;
}
function readyToApply(result){
  try{readingRouteWeights(result?.combined?.routing_weights);}catch{return false;}
  return result?.route_maker_reading?.status==='ready'&&result?.profile_status==='ready'&&result.combined?.status==='ready'&&
    Object.keys(systems).every(key=>result.systems[key].profile?.status==='ready')&&
    Object.keys(names).every(key=>Number.isInteger(result.combined.profile_scores?.[key])&&result.combined.profile_scores[key]>=1&&result.combined.profile_scores[key]<=10);
}

// Only final numeric preferences and four supported weights enter routing. Explicit save keeps
// whitelisted birth inputs in this browser; calculations and keys are never saved.
export function createProfileController({api,onApply,onIdentity=()=>{},onInvalidate=()=>{}}){
  const gate=createRevisionGate();
  let controller=null,profile=null,ready=false,mode='manual',inputsChanged=false;
  let savedProfile=null,storageChecked=false;
  function storageMessage(text,error=false){const element=$('profile-storage-status');if(element){element.textContent=text;element.classList.toggle('error',error);}}
  function readInputs(){
    const unknown=$('birth-unknown').checked;
    const coordinate=id=>{const text=$(id).value.trim();return text===''?null:/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text)?Number(text):NaN;};
    return {name:($('profile-name')?.value||'').trim(),birth_date:$('birth-date').value,birth_time:unknown?null:$('birth-time').value,
      birth_unknown:unknown,birth_timezone:$('birth-timezone').value.trim(),birth_latitude:coordinate('birth-latitude'),birth_longitude:coordinate('birth-longitude'),
      birth_house_system:$('birth-house-system').value||'P',birth_fold:unknown||$('birth-fold').value===''?null:Number($('birth-fold').value)};
  }
  function identity(){
    const current=readInputs(),saved=!!savedProfile&&JSON.stringify(current)===JSON.stringify(savedProfile);
    onIdentity({name:current.name,saved});
    if($('profile-delete-saved')){$('profile-delete-saved').disabled=!savedProfile;$('profile-delete-saved').hidden=!savedProfile;}
    return saved;
  }
  function syncTimeControls(){const unknown=$('birth-unknown').checked;$('birth-time').disabled=unknown;$('birth-time').required=!unknown;$('birth-fold').disabled=unknown;}
  function restoreSaved(){
    if(storageChecked)return;storageChecked=true;
    try{
      const raw=globalThis.localStorage?.getItem(storageKey);
      if(!raw){storageMessage('Save to remember your details in this browser.');identity();return;}
      let stored;try{stored=JSON.parse(raw);}catch{/* Invalid records are discarded below. */}
      if(!stored||stored.version!==1||Object.keys(stored).length!==2||!validSavedProfile(stored.profile,ready.birth_date_max)){
        globalThis.localStorage.removeItem(storageKey);storageMessage('The saved details could not be loaded. Please enter them again.');identity();return;
      }
      savedProfile=Object.fromEntries(savedFields.map(key=>[key,stored.profile[key]]));
      if($('profile-name'))$('profile-name').value=savedProfile.name;
      for(const field of ['birth_date','birth_time','birth_timezone','birth_latitude','birth_longitude','birth_house_system','birth_fold'])$(field.replaceAll('_','-')).value=savedProfile[field]===null?'':String(savedProfile[field]);
      $('birth-unknown').checked=savedProfile.birth_unknown;syncTimeControls();identity();
      storageMessage('Your saved details are ready. Continue to calculate today’s profile.');
    }catch{storageMessage('Browser storage is unavailable. You can continue without saving.',true);identity();}
  }
  function save(){
    const value=readInputs();
    if(!ready||!validSavedProfile(value,ready.birth_date_max)){
      storageMessage('Check your birth date, time (or unknown time) and timezone. Enter both coordinates or leave both blank.',true);return;
    }
    try{
      if(!globalThis.localStorage)throw Error('Storage unavailable');
      globalThis.localStorage.setItem(storageKey,JSON.stringify({version:1,profile:value}));savedProfile=value;identity();
      storageMessage('Saved in this browser. On a shared device, delete your details when finished.');
    }catch{storageMessage('Your details could not be saved. You can still continue with the information entered.',true);}
  }
  function deleteSaved(){
    try{
      if(!globalThis.localStorage)throw Error('Storage unavailable');
      globalThis.localStorage.removeItem(storageKey);savedProfile=null;clear();storageMessage('Saved details, current inputs and analysis deleted. Your experience preferences are unchanged.');
    }catch{storageMessage('Saved details could not be deleted. You can remove them in this browser’s site data settings.',true);}
  }
  function message(text,error=false){$('profile-status').textContent=text;$('profile-status').classList.toggle('error',error);}
  function cancel(){gate.invalidate();controller?.abort();controller=null;$('profile-apply').disabled=!ready;$('profile-apply').textContent=applyLabel;}
  function showSource(){
    const automatic=mode==='automatic'&&!inputsChanged&&readyToApply(profile);
    const reading=profile?.route_maker_reading,summary=readingSummary(reading),card=$('reading-trip');
    if(card){card.hidden=!automatic;if(automatic){$('reading-trip-event').textContent=summary.event;$('reading-trip-qualities').textContent=summary.qualities;$('reading-trip-weights').textContent=Object.entries(profile.combined.routing_weights).map(([key,value])=>`${streetNames[key]} ${percent(value)}`).join(' · ');}}
    $('profile-source').textContent=automatic?`${profile.target.date} · Original reading applied`:profile?'Manually adjusted preferences':'Your selected preferences';
    if(inputsChanged&&profile&&mode==='stale')$('profile-source').textContent='Previous suggestion · recalculate after changing birth details or date';
    $('profile-state').textContent=inputsChanged?'Your details have changed. Recalculate to update the suggestions.':automatic?'Saju + astrology → six experience qualities → street weights → one route.':'These sliders reflect your manual preferences.';
    if(profile&&!readyToApply(profile))$('profile-state').textContent='Both analyses must be ready. Your sliders have not changed.';
    $('profile-reapply').hidden=!profile||inputsChanged||automatic||!readyToApply(profile);
  }
  function manual(){cancel();mode='manual';onInvalidate();showSource();message('Your selected preferences will shape the route.');}
  function apply(){
    if(!profile||inputsChanged||!readyToApply(profile))return;
    mode='automatic';onApply({...profile.combined.profile_scores});showSource();
    message('Preferences applied. Continue with one recommended route.');
  }
  function invalidateInputs(event){
    const saved=identity();
    if(savedProfile)storageMessage(saved?'Your current details are saved in this browser.':'Your details have changed. Save again to keep these changes.');
    if(event?.target?.id==='profile-name')return;
    cancel();inputsChanged=!!profile;if(mode==='automatic')mode='stale';onInvalidate();showSource();message(profile?'Your details have changed. Recalculate to apply the new values.':'Complete your details, then continue.');
  }
  function renderSystem(key,result){
    // Keep the exact astronomical/calendar facts while using the original
    // CSV rule IDs for interpretation evidence, not legacy five-score IDs.
    result=englishProfileSystem(key,result);
    const section=node('section',`system-analysis ${key}`),reading=profile.route_maker_reading?.[key];
    section.append(node('h3','system-analysis-title',systems[key]));
    if(result.provider){const provider=result.provider;section.append(node('p','provider-line',`${provider.name||systems[key]}${provider.version?` ${provider.version}`:''}${key==='astrology'?' · JPL ephemeris data':''}`));}
    const highlights=node('dl','analysis-highlights');
    for(const item of result.highlights||[])highlights.append(node('dt','',item.label),node('dd','',item.value));
    section.append(highlights);
    if(reading){
      section.append(table(`${systems[key]} interpretation`,['Quality','Score'],Object.entries(qualities).map(([axis,label])=>[label,number(reading.quality?.[axis])])));
      const evidence=node('details','analysis-evidence'),items=reading.evidence||[];
      evidence.append(node('summary','',`${items.length} original rule contributions`));
      for(const item of items){
        const row=node('article','evidence-row');row.append(node('strong','',item.detail||item.label||item.rule_id||item.id||'Rule contribution'),node('p','',item.explanation||item.fact||item.reason||item.description||''));
        if(typeof item.quality==='string')row.append(node('p','evidence-vector',`${events[item.event]||item.event} → ${qualities[item.quality]||item.quality} · contribution ${number(item.score)}`));
        if(Number.isFinite(item.rule_weight))row.append(node('p','field-help',`Rule weight ${number(item.rule_weight)} × signal strength ${number(item.strength)}`));
        if(item.reference){const reference=item.reference.startsWith('외부 출처 미확인')?'No external source verified. Internal implementation: saju_engine.py natal_chart / compute_saju; combine_reading.py interpret_saju.':profileText(item.reference);row.append(node('p','field-help',`Symbolic reference: ${reference}`));}
        if(item.verification_scope)row.append(node('p','field-help',item.verification_scope));
        const vector=item.quality||item.quality_vector||item.vector;
        if(vector&&typeof vector==='object')row.append(node('p','evidence-vector',Object.entries(qualities).filter(([axis])=>Number.isFinite(vector[axis])).map(([axis,label])=>`${label} ${number(vector[axis])}`).join(' · ')));
        if(Number.isFinite(item.weight))row.append(node('p','field-help',`Contribution weight ${number(item.weight)}`));
        if(item.rule_id)row.append(node('p','policy-label',`Rule: ${item.rule_id}`));
        evidence.append(row);
      }
      section.append(evidence);
      for(const omission of reading.omissions||[])section.append(node('p','field-help',typeof omission==='string'?omission:omission.reason||omission.label||''));
    }
    const facts=node('details','analysis-facts');facts.append(node('summary','',`Calculated ${systems[key]} facts`));
    for(const block of result.display_sections||[])facts.append(table(block.title,block.columns,block.rows));
    section.append(facts);
    return section;
  }
  function render(){
    $('profile-result').hidden=false;
    const usable=readyToApply(profile),reading=profile.route_maker_reading,summary=readingSummary(reading);
    $('profile-summary').textContent=usable?`${summary.event} · ${summary.qualities}`:'Review your analysis';
    $('profile-score-chips').replaceChildren(...(usable?Object.entries(events).map(([key,label])=>node('span','',`${label} ${number(reading.combined.event[key])}`)):[]));
    const breakdown=$('profile-breakdown');breakdown.replaceChildren();
    if(usable){
      breakdown.append(node('p','field-help','An experimental reading of the day: three themes and six experience qualities. These are design interpretations, not predictions.'));
      const details=node('details','profile-details');details.append(node('summary','','From two readings to one route'));
      for(const [title,labels,field] of [['Three event themes',events,'event'],['Six experience qualities',qualities,'quality']]){
        details.append(table(title,['Theme','Saju','Astrology','Combined'],Object.entries(labels).map(([key,label])=>[label,number(reading.saju[field]?.[key]),number(reading.astrology[field]?.[key]),number(reading.combined[field]?.[key])])));
      }
      details.append(node('p','field-help','Saju and astrology are averaged 50:50 at the event and quality stages. The six combined qualities enter the original quality-to-street matrix. Event themes describe the reading; the quality scores set the route weights.'));
      const routeWeights=reading.routing_weights,omitted=new Set((reading.coverage?.omitted||[]).map(item=>item.source));
      details.append(table('Eight original street weights',['Street quality','Original','Used for route'],Object.entries(reading.source_street_weights||{}).map(([key,value])=>[streetNames[key]||key,percent(value),omitted.has(key)?'Not measured':percent(routeWeights?.[key])])));
      details.append(node('p','field-help','The four supported indicators are renormalized for routing at full precision. Water, sunlight, rest and culture do not influence this demo route; missing indicators are not treated as zero-quality streets.'));
      for(const item of reading.coverage?.supported||[])details.append(node('p','field-help',`${item.label}: ${item.proxy}`));
      if(reading.coverage?.access)details.append(node('p','field-help',reading.coverage.access));
      for(const item of reading.coverage?.omitted||[])details.append(node('p','field-help',`${item.label||streetNames[item.source]||item.source}: ${item.reason}`));
      const provenance=reading.provenance;
      if(provenance){const source=node('details','analysis-facts');source.append(node('summary','','Rule sources & calculation scope'),node('p','field-help',provenance.origin),node('p','field-help',provenance.pipeline),node('p','field-help',provenance.rounding));for(const difference of provenance.differences_from_original||[])source.append(node('p','field-help',difference));details.append(source);}
      breakdown.append(details);
    }
    const chart=$('profile-chart');chart.replaceChildren(node('p','field-help',`${profile.target.date} · ${profileText(profile.target.reference)||'Noon in New York'}`));
    for(const key of Object.keys(systems))chart.append(renderSystem(key,profile.systems[key]));
    const omissions=$('profile-omissions');omissions.replaceChildren();
    for(const key of Object.keys(systems)){
      const items=profile.systems[key].omissions||[];
      if(items.length){omissions.append(node('h3','',`Not included in ${systems[key]}`));for(const reason of new Set(items.map(item=>profileText(item.reason))))omissions.append(node('p','',reason));}
    }
    showSource();
  }
  async function calculate(event){
    event.preventDefault();if(!ready)return;
    cancel();if(mode==='automatic'){mode='stale';inputsChanged=true;onInvalidate();showSource();}const token=gate.invalidate();controller=new AbortController();
    const payload={birth_date:$('birth-date').value,birth_time:$('birth-unknown').checked?null:$('birth-time').value,birth_timezone:$('birth-timezone').value.trim(),target_date:$('profile-date').value};
    if($('birth-fold').value!=='')payload.birth_fold=Number($('birth-fold').value);
    const latitude=$('birth-latitude').value.trim(),longitude=$('birth-longitude').value.trim();
    if(latitude!==''||longitude!==''){
      if(latitude===''||longitude===''){message('Enter both birthplace coordinates, or leave both blank.',true);return;}
      payload.birth_latitude=Number(latitude);payload.birth_longitude=Number(longitude);payload.house_system=$('birth-house-system').value||'P';
    }
    $('profile-apply').disabled=true;$('profile-apply').textContent='Calculating…';message('Reading Saju relationships, your natal chart and today’s transits.');
    try{
      const result=await api('/explore/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal});
      if(!gate.isCurrent(token))return;
      if(!validResponse(result))throw Error('Unsupported analysis format. Refresh the app and check the server version.');
      profile=result;inputsChanged=false;mode='manual';render();
      if(readyToApply(profile))apply();else message('Both analyses are not yet ready. Review the omitted information and engine status.',true);
    }catch(error){
      if(error.name==='AbortError'||!gate.isCurrent(token))return;
      message(error.message==='Failed to fetch'?'The calculation server is unavailable. Check that it is running.':profileText(error.message),true);
      if(error.message.includes('중복')||error.message.includes('두 번')||/repeated birth time|birth time occurs twice/.test(error.message)){$('birth-fold').closest('details').open=true;$('birth-fold').focus();}
    }finally{if(gate.isCurrent(token)){$('profile-apply').disabled=false;$('profile-apply').textContent=applyLabel;}}
  }
  function clear(){
    cancel();$('birth-form').reset();$('profile-date').value=ready.default_target_date||'';$('birth-time').disabled=false;$('birth-time').required=true;$('birth-fold').disabled=false;
    if($('profile-name'))$('profile-name').value='';
    profile=null;inputsChanged=false;mode='manual';onInvalidate();$('profile-result').hidden=true;
    for(const id of ['profile-chart','profile-breakdown','profile-score-chips','profile-omissions'])$(id).replaceChildren();
    $('profile-summary').textContent='';showSource();message('Birth inputs and analysis cleared. You can still adjust the current sliders.');identity();
    storageMessage(savedProfile?'Current inputs cleared. Saved details remain in this browser; use Delete saved details to remove them.':'Save to remember your details in this browser.');
  }
  // Native validation runs before submit and invalid does not bubble. Reveal
  // collapsed fields in capture phase; the browser retains focus and messaging.
  $('birth-form').addEventListener('invalid',event=>{
    for(let details=event.target.closest('details');details;details=details.parentElement?.closest('details'))details.open=true;
    message('Check the highlighted field to continue.',true);
  },true);
  $('birth-form').addEventListener('submit',calculate);
  $('birth-form').addEventListener('input',invalidateInputs);
  $('birth-unknown').addEventListener('change',syncTimeControls);
  $('profile-reapply').addEventListener('click',apply);$('profile-clear').addEventListener('click',clear);
  $('profile-save')?.addEventListener('click',save);$('profile-delete-saved')?.addEventListener('click',deleteSaved);
  return {manual,clear,mode:()=>mode,routeWeights(){return mode==='automatic'&&!inputsChanged&&readyToApply(profile)?readingRouteWeights(profile.combined.routing_weights):null;},routeSource(){return mode==='automatic'&&!inputsChanged&&readyToApply(profile)?{kind:'route-maker-reading',analysis_version:profile.profile_version,saju_policy:profile.systems.saju.profile.policy_id,astrology_policy:profile.systems.astrology.profile.policy_id,fusion_policy:profile.combined.policy_id}:{kind:'manual'};},configure(config){ready=config;$('profile-date').value=config.default_target_date;$('birth-date').max=config.birth_date_max;$('profile-apply').disabled=false;restoreSaved();}};
}

// Presentation-only English adapter. The server's calculated facts and scores
// remain unchanged; raw birth inputs never enter this module or route requests.
const pillars={year:'Year',month:'Month',day:'Day',hour:'Hour'};
const planets=['Sun','Moon','Mercury','Venus','Mars','Jupiter','Saturn','Uranus','Neptune','Pluto'];
const signs=['Aries','Taurus','Gemini','Cancer','Leo','Virgo','Libra','Scorpio','Sagittarius','Capricorn','Aquarius','Pisces'];
const signNames=Object.fromEntries(['양자리','황소자리','쌍둥이자리','게자리','사자자리','처녀자리','천칭자리','전갈자리','사수자리','염소자리','물병자리','물고기자리'].map((name,index)=>[name,signs[index]]));
const planetNames=Object.fromEntries(['태양','달','수성','금성','화성','목성','토성','천왕성','해왕성','명왕성'].map((name,index)=>[name,planets[index]]));
const tenGods={
  peer_same:['Peer','the same element and polarity'],
  peer_opposite:['Rob Wealth','the same element, with opposite polarity'],
  expression_same:['Eating God','an element produced by the Day Master, with the same polarity'],
  expression_opposite:['Hurting Officer','an element produced by the Day Master, with opposite polarity'],
  wealth_same:['Indirect Wealth','an element controlled by the Day Master, with the same polarity'],
  wealth_opposite:['Direct Wealth','an element controlled by the Day Master, with opposite polarity'],
  authority_same:['Seven Killings','an element that controls the Day Master, with the same polarity'],
  authority_opposite:['Direct Officer','an element that controls the Day Master, with opposite polarity'],
  resource_same:['Indirect Resource','an element that produces the Day Master, with the same polarity'],
  resource_opposite:['Direct Resource','an element that produces the Day Master, with opposite polarity'],
};
const stages=Object.fromEntries(['장생','목욕','관대','건록','제왕','쇠','병','사','묘','절','태','양'].map((name,index)=>[name,['Birth','Bathing','Crowning','Maturity','Peak','Decline','Illness','Death','Tomb','Severance','Conception','Nurturing'][index]]));
const seasons={봄:'spring',여름:'summer',가을:'autumn',겨울:'winter'};
const cap=value=>value?value[0].toUpperCase()+value.slice(1):'—';
const godName=value=>value?tenGods[value.key]?.[0]||value.chinese:'Uncertain';
const sign=point=>Number.isInteger(point?.sign_index)?signs[point.sign_index]:signNames[point?.sign]||point?.sign||'Varies with birth time';
const house=value=>value?String(value).replaceAll('_',' '):'—';
const sentences={
  '뉴욕 현지 시각 정오':'Noon in New York',
  '뉴욕 정오 기준':'Noon in New York',
  '뉴욕 현지 정오':'Noon in New York',
  '분석기를 실행하지 못했어요.':'The analysis could not run.',
  '출생 시간을 몰라 시주를 제외했습니다.':'The hour pillar is omitted because the birth time is unknown.',
  '일간이 확정되지 않아 십신·12운성·통근 비교와 산책 점수를 보류했습니다.':'The Day Master is uncertain. Ten Gods, life stages, root comparisons and experience scores are unavailable.',
  '출생 시간을 몰라 정확한 오브와 접근·분리 여부를 확정할 수 없어 출생 각과 개인 트랜싯은 생략했어요.':'Without a birth time, exact orbs and applying or separating motion are uncertain. Natal aspects and personal transits are omitted.',
  '정확한 출생 시간과 출생지 좌표가 모두 있어야 하우스·상승점·위치에 따른 위계를 계산해요.':'Houses, the Ascendant and location-dependent dignities require both an exact birth time and birthplace coordinates.',
  '출생지 위도와 경도를 함께 입력하거나 둘 다 비워 주세요.':'Enter both birthplace coordinates, or leave both blank.',
  '출생지 위도와 경도를 함께 입력하거나 모두 비워 주세요.':'Enter both birthplace coordinates, or leave both blank.',
  '출생 정보를 JSON 객체로 입력해 주세요.':'The profile request must be a JSON object.',
  '지원하지 않는 출생 정보 항목이 있어요.':'The profile request contains an unsupported field.',
  '생년월일, 출생지 시간대와 분석 날짜를 입력해 주세요.':'Enter your birth date, birth timezone and analysis date.',
  '이 지도는 뉴욕 시간대를 기준으로 분석해요.':'This map uses the New York timezone for the analysis date.',
  '중복 출생 시간은 첫 번째 또는 두 번째를 선택해 주세요.':'Select the first or second occurrence of the repeated birth time.',
  '하우스 방식은 P 또는 W를 선택해 주세요.':'Choose Placidus (P) or Whole Sign (W) houses.',
  '출생 시간은 현재보다 이전이어야 해요.':'The birth time must be in the past.',
  '생년월일은 1900년부터 오늘까지 입력할 수 있어요.':'The birth date must be between 1900 and today.',
  '분석 날짜는 1900년부터 2100년까지 입력할 수 있어요.':'The analysis date must be between 1900 and 2100.',
  '출생지 시간대를 선택해 주세요.':'Select the birth timezone.',
  '출생지 시간대를 확인해 주세요.':'Check the birth timezone.',
  '출생 시간을 HH:MM 형식으로 입력하거나 모름을 선택해 주세요.':'Enter a birth time as HH:MM, or select “I don’t know.”',
  '출생 시간의 시와 분을 확인해 주세요.':'Check the hours and minutes of the birth time.',
  '서머타임 변경으로 존재하지 않는 출생 시간이에요. 시간을 확인해 주세요.':'This birth time falls in a daylight-saving gap. Check the time.',
  '서머타임 종료로 같은 시간이 두 번 있어요. 첫 번째인지 두 번째인지 선택해 주세요.':'This birth time occurs twice when daylight saving ends. Select the first or second occurrence.',
  '선택한 시간대에서 존재하지 않는 출생 날짜예요. 날짜와 시간대를 확인해 주세요.':'This date does not exist in the selected timezone. Check the date and timezone.',
  '출생 날짜는 현재보다 이전이어야 해요. 출생지 시간대를 확인해 주세요.':'The birth date must be in the past. Check the birth timezone.',
  '천체 위치를 계산하지 못했어요. 입력을 다시 확인해 주세요.':'Celestial positions could not be calculated. Check your inputs.',
  '별자리 계산 엔진 설정을 확인해 주세요.':'Check the astrology engine configuration.',
  '검증된 로컬 천체 데이터가 없어 별자리 분석을 중단했어요.':'Astrology analysis stopped because reviewed local ephemeris data is unavailable.',
  '로컬 별자리 계산을 완료하지 못했어요. 입력 범위와 천체 데이터 설정을 확인해 주세요.':'Local astrology analysis failed. Check the input range and ephemeris data configuration.',
  '별자리 계산 데이터의 출처를 확인하지 못했어요.':'The source of the astrology calculation data could not be verified.',
  '별자리 계산에 유효하지 않은 수치가 있어요.':'The astrology calculation contains an invalid number.',
  '두 분석의 가중치가 모두 준비되어야 적용할 수 있어요.':'Both analyses must be ready before their weights can be applied.',
  '분석 가중치가 유효하지 않아 적용하지 않았어요.':'The analysis weights are invalid and were not applied.',
  '일간 기준 십신 관계를 산책 주제로 연결하는 실험 규칙':'An experimental mapping from Ten Gods relationships to experience preferences.',
};
export function profileText(value){
  if(typeof value!=='string')return value;
  if(sentences[value])return sentences[value];
  if(!/[가-힣]/.test(value))return value;
  let match;
  if((match=value.match(/^출생 시각 범위 안에서 (연주|월주|일주|시주)가 바뀌어 제외했습니다\.$/)))return `The ${({'연주':'year','월주':'month','일주':'day','시주':'hour'})[match[1]]} pillar changes within the possible birth-time window and is omitted.`;
  if((match=value.match(/^출생일 중 (.+) 별자리가 바뀌어 점수에서 제외했어요\.$/)))return `${planetNames[match[1]]||match[1]} changes signs during the birth date and is omitted from the scores.`;
  if((match=value.match(/^출생일 중 (.+)의 순행·역행이 바뀌어 확정하지 않았어요\.$/)))return `${planetNames[match[1]]||match[1]} changes between direct and retrograde motion during the birth date, so its motion is uncertain.`;
  if((match=value.match(/^해당 위도에서는 요청한 하우스 계산이 성립하지 않아 (.+) 방식으로 대체했어요\.$/)))return `The requested house system cannot be calculated at this latitude. The ${match[1]} system was used instead.`;
  if((match=value.match(/^(사주|점성술) 분석기를 실행하지 못했어요\. 설치와 계산 데이터 상태를 확인해 주세요\.$/)))return `${match[1]==='사주'?'Saju':'Astrology'} analysis could not run. Check the installation and calculation data.`;
  if((match=value.match(/^출생지 (위도|경도)는 -([\d]+)부터 ([\d]+) 사이의 유한한 숫자로 입력해 주세요\.$/)))return `Birthplace ${match[1]==='위도'?'latitude':'longitude'} must be a finite number between −${match[2]} and ${match[3]}.`;
  if((match=value.match(/^(생년월일|분석 날짜)를 YYYY-MM-DD 형식으로 입력해 주세요\.$/)))return `Enter the ${match[1]==='생년월일'?'birth':'analysis'} date as YYYY-MM-DD.`;
  if((match=value.match(/^(생년월일|분석 날짜)에 실제로 존재하는 날짜를 입력해 주세요\.$/)))return `Enter a valid ${match[1]==='생년월일'?'birth':'analysis'} date.`;
  return 'This detail is not available in English.';
}
function sajuPresentation(result){
  const {natal,today}=result.analysis||{};
  if(!natal||!today)return null;
  const master=natal.day_master,month=natal.month_context,day=today.pillars.day;
  const highlights=[
    {label:'Day Master',value:master?`${master.text} · ${master.yin_yang==='양'?'Yang':'Yin'} ${cap(master.element)}`:'Uncertain'},
    {label:'Natal pillars',value:Object.values(natal.pillars).map(p=>p?.text||'Uncertain').join(' · ')},
    {label:'Seasonal context',value:month?`${month.month_branch} month · traditional ${seasons[month.season]||month.season}`:'Month pillar uncertain'},
    {label:'Same-element hidden stems',value:(natal.root_evidence||[]).map(r=>`${pillars[r.pillar]}: ${r.hidden_stem} in ${r.branch}`).join(', ')||(master?'None identified':'Day Master uncertain')},
    {label:'Today’s day pillar',value:`${day.text} · ${godName(day.stem_ten_god)}`},
    {label:'Analysis scope',value:`${natal.available_pillar_count} confirmed natal pillars · strength and useful-god judgments excluded`},
  ];
  const display_sections=[['Natal Saju',natal],['Today, relative to the natal Day Master',today]].map(([title,chart])=>({title,
    columns:['Pillar','Stem & branch','Stem Ten God','Hidden stems & Ten Gods','12 life stages'],
    rows:Object.entries(chart.pillars).map(([key,p])=>p?[pillars[key],p.text,p.is_day_master?'Day Master':godName(p.stem_ten_god),
      p.hidden_stems.map(h=>`${h.text} ${godName(h.ten_god)}`).join(', '),p.twelve_stage?`${stages[p.twelve_stage.korean]||p.twelve_stage.chinese} (${p.twelve_stage.chinese})`:'Uncertain']:[pillars[key],'Uncertain / omitted','—','—','—'])}));
  const evidence=(result.profile?.evidence||[]).map(item=>{
    const [,component,position,kind,order]=item.id.split('.'),chart=component==='natal'?natal:today,p=chart.pillars[position];
    const hidden=kind==='hidden'?p.hidden_stems.find(h=>h.order_in_library===Number(order)):null;
    const stem=hidden?.text||p.stem,god=hidden?.ten_god||p.stem_ten_god;
    return {...item,label:`${component==='natal'?'Natal':'Today’s'} ${position} ${hidden?`hidden stem ${stem} in ${p.branch}`:`stem ${stem}`}`,
      fact:`Relative to Day Master ${master?.text||'—'}, ${stem} is ${godName(god)} (${god?.chinese||'—'}): ${tenGods[god?.key]?.[1]||'relationship uncertain'}.`};
  });
  return {highlights,display_sections,evidence};
}
function astrologyPresentation(result){
  const {natal,today,transits=[]}=result.analysis||{};
  if(!natal||!today)return null;
  const highlights=[
    {label:'Natal Sun',value:sign(natal.planets.Sun)},
    {label:'Natal Moon',value:sign(natal.planets.Moon)},
    {label:'Natal major aspects',value:natal.time_known?String(natal.aspects.length):'Omitted · birth time unknown'},
    {label:'Today’s personal transits',value:natal.time_known?`${transits.length} · 3° orb`:'Omitted · birth time unknown'},
    {label:'Today’s Moon',value:sign(today.planets.Moon)},
    {label:'Houses',value:natal.house_system||'Birth time and coordinates needed'},
  ];
  const display_sections=[{title:'Natal planets',columns:['Body','Sign','Longitude','Motion','House'],rows:Object.entries(natal.planets).map(([name,p])=>[name,
    p.sign?sign(p):(p.alternatives||[]).map(value=>signNames[value]||value).join(' / '),
    Number.isFinite(p.longitude_deg)?`${p.longitude_deg.toFixed(3)}°`:'Calculated as a range',p.retrograde===true?'Retrograde':p.retrograde===false?'Direct':'Uncertain',house(p.house)])},
    {title:'Today → natal transits',columns:['Today','Natal','Aspect','Orb','Motion'],rows:transits.map(p=>[p.first,p.second,cap(p.aspect),`${p.orb_deg.toFixed(3)}°`,p.movement])}];
  const evidence=(result.profile?.evidence||[]).map(item=>{
    let label,fact;
    if(item.source_fact){const p=item.source_fact;label=item.id.startsWith('transit-')?'Today → natal transit':'Natal major aspect';fact=`${p.first}–${p.second} ${p.aspect} · orb ${p.orb_deg.toFixed(2)}° · ${p.movement}${p.retrograde_planets?.length?` · ${p.retrograde_planets.join(' · ')} retrograde`:''}`;}
    else {const isToday=item.id.startsWith('today-'),name=item.id.split('-')[1],point=(isToday?today:natal).planets[name];label=`${isToday?'Today’s':'Natal'} ${name} placement`;fact=`${name} · ${sign(point)}`;}
    return {...item,label,fact};
  });
  return {highlights,display_sections,evidence};
}
export function englishProfileSystem(key,result){
  const original=!!result.profile?.policy_id?.startsWith('route-maker-');
  const facts=original?{...result,profile:{...result.profile,evidence:[]}}:result;
  const presentation=key==='saju'?sajuPresentation(facts):astrologyPresentation(facts);
  return {...result,
    highlights:presentation?.highlights||(result.highlights||[]).map(item=>({label:profileText(item.label),value:profileText(item.value)})),
    display_sections:presentation?.display_sections||result.display_sections||[],
    omissions:(result.omissions||[]).map(item=>({...item,reason:profileText(item.reason)})),
    profile:result.profile?{...result.profile,evidence:original?(result.profile.evidence||[]):(presentation?.evidence||result.profile.evidence||[]),
      policy:{...result.profile.policy,description:original?result.profile.policy?.description:key==='saju'?'An experimental mapping from Ten Gods relationships to experience preferences.':'An experimental mapping from natal placements, today’s Moon and close major aspects to experience preferences.'}}:result.profile};
}
