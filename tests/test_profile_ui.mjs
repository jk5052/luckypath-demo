// State-only unit tests: no browser, network, real storage or human birth data.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createProfileController} from '../luckypath/web/profile.js';

const keys = ['open', 'green', 'still', 'social', 'access'];

class Element {
  constructor() {
    this.children = []; this.listeners = {}; this.listenerRegistrations = []; this.value = ''; this.textContent = '';
    this.checked = false; this.hidden = false; this.disabled = false; this.required = false;
    this.classList = {toggle() {}};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  get firstChild() { return this.children[0]; }
  addEventListener(name, fn, options) {
    (this.listeners[name] ??= []).push(fn);
    this.listenerRegistrations.push({name, fn, options});
  }
  async emit(name, target) {
    for (const fn of this.listeners[name] ?? []) await fn({preventDefault() {}, target});
  }
  closest() { return this; }
  focus() { this.focused = true; }
}

const storageKey = 'luckypath.profile.v1';
function memoryStorage(entries = []) {
  const values = new Map(entries);
  return {values, getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
}

function fixture({storage = memoryStorage(), seed = true, optionalElements = true} = {}) {
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  globalThis.localStorage = storage;
  globalThis.document = {getElementById: id => !optionalElements && ['profile-name', 'profile-save',
    'profile-delete-saved', 'profile-storage-status'].includes(id) ? null : $(id), createElement: () => new Element()};
  $('birth-form').reset = () => {
    for (const id of ['birth-date', 'birth-time', 'birth-timezone', 'birth-fold',
      'birth-latitude', 'birth-longitude', 'profile-date', 'profile-name']) $(id).value = '';
    $('birth-house-system').value = 'P';
    $('birth-unknown').checked = false;
    // Native form.reset() does not reset the disabled property.
  };
  const requests = [], applied = [], identities = [];
  $('birth-house-system').value = 'P';
  const controller = createProfileController({
    api(path, options) {
      return new Promise((resolve, reject) => requests.push({path, options, resolve, reject}));
    },
    onApply(scores) { applied.push(scores); },
    onIdentity(identity) { identities.push(identity); },
  });
  controller.configure({schema_version: 2, version: 'independent-analysis-v2',
    default_target_date: '2026-10-13', birth_date_max: '2026-10-13'});
  if (seed) {
    $('birth-date').value = '1990-05-15';
    $('birth-time').value = '14:30';
    $('birth-timezone').value = 'Asia/Seoul';
    $('birth-house-system').value = 'P';
  }
  return {$, requests, applied, controller, identities, storage};
}

function profile(value = 7) {
  const scores = Object.fromEntries(keys.map(key => [key, value]));
  const independent = system => ({
    system, status: 'ready', analysis: {}, highlights: [], omissions: [],
    profile: {
      status: 'ready', policy_id: `${system}-experimental-v1`,
      profile_scores: {...scores},
      score_details: Object.fromEntries(keys.map(key => [key, {
        rounded: value, unrounded: value, signals: [], contributions: [],
      }])),
      policy: {id: `${system}-experimental-v1`, externally_validated: false},
    },
  });
  return {
    schema_version: 2, profile_version: 'independent-analysis-v2', profile_status: 'ready',
    target: {date: '2026-10-13', timezone: 'America/New_York', reference: '뉴욕 현지 시각 정오'},
    systems: {saju: independent('saju'), astrology: independent('astrology')},
    combined: {
      status: 'ready', policy_id: 'equal-experience-weights-v1', profile_scores: scores,
      score_details: Object.fromEntries(keys.map(key => [key, {
        saju: value, astrology: value, rounded: value, unrounded: value, difference: 0,
      }])),
      policy: {stage: 'after_independent_experience_weights',
        system_weights: {saju: .5, astrology: .5}, externally_validated: false},
    },
    notes: [],
  };
}

async function completeFirst(f, value = 7) {
  const pending = f.$('birth-form').emit('submit');
  f.requests.at(-1).resolve(profile(value));
  await pending;
}

test('profile applies only five numeric preferences and keeps raw birth outside route callback', async () => {
  const f = fixture();
  await completeFirst(f);
  assert.equal(f.requests[0].path, '/explore/profile');
  assert.deepEqual(JSON.parse(f.requests[0].options.body), {
    birth_date: '1990-05-15', birth_time: '14:30', birth_timezone: 'Asia/Seoul', target_date: '2026-10-13',
  });
  assert.deepEqual(f.applied, [Object.fromEntries(keys.map(key => [key, 7]))]);
  assert.equal(f.controller.mode(), 'automatic');
  assert.equal(f.$('profile-result').hidden, false);
  assert.equal(f.$('profile-apply').textContent, 'Continue');
  assert.match(f.$('profile-chart').children[0].textContent, /Noon in New York/);
});

test('manual slider or preset action beats an already pending profile response', async () => {
  const f = fixture();
  const pending = f.$('birth-form').emit('submit');
  f.controller.manual();
  assert.equal(f.requests[0].options.signal.aborted, true);
  // The fake transport deliberately resolves after abort to exercise the gate.
  f.requests[0].resolve(profile());
  await pending;
  assert.deepEqual(f.applied, []);
  assert.equal(f.controller.mode(), 'manual');
  assert.equal(f.$('profile-apply').disabled, false);
});

test('editing birth inputs invalidates pending response and a newer calculation wins', async () => {
  const f = fixture();
  const older = f.$('birth-form').emit('submit');
  f.$('birth-date').value = '1991-05-15';
  await f.$('birth-form').emit('input');
  const newer = f.$('birth-form').emit('submit');
  f.requests[1].resolve(profile(8));
  await newer;
  f.requests[0].resolve(profile(6));
  await older;
  assert.deepEqual(f.applied, [Object.fromEntries(keys.map(key => [key, 8]))]);
});

test('reapply is explicit after manual changes and blocked after birth input changes', async () => {
  const f = fixture();
  await completeFirst(f);
  f.controller.manual();
  assert.equal(f.$('profile-reapply').hidden, false);
  await f.$('profile-reapply').emit('click');
  assert.equal(f.applied.length, 2);
  f.$('birth-time').value = '16:30';
  await f.$('birth-form').emit('input');
  assert.equal(f.$('profile-reapply').hidden, true);
  await f.$('profile-reapply').emit('click');
  assert.equal(f.applied.length, 2);
});

test('manual change after birth edit is labelled manual while stale proposal stays unavailable', async () => {
  const f = fixture();
  await completeFirst(f);
  f.$('birth-date').value = '1991-05-15';
  await f.$('birth-form').emit('input');
  assert.equal(f.controller.mode(), 'stale');
  assert.match(f.$('profile-source').textContent, /Previous suggestion/);
  f.controller.manual();
  assert.equal(f.controller.mode(), 'manual');
  assert.match(f.$('profile-source').textContent, /Manually adjusted/);
  assert.doesNotMatch(f.$('profile-source').textContent, /Previous suggestion/);
  assert.equal(f.$('profile-reapply').hidden, true);
  await f.$('profile-reapply').emit('click');
  assert.equal(f.applied.length, 1);
});

test('clear removes birth/chart state, cancels pending work and restores known-time controls', async () => {
  const f = fixture();
  await completeFirst(f);
  f.$('birth-unknown').checked = true;
  await f.$('birth-unknown').emit('change');
  assert.equal(f.$('birth-fold').disabled, true);
  const pending = f.$('birth-form').emit('submit');
  f.controller.clear();
  f.requests.at(-1).resolve(profile(9));
  await pending;
  for (const id of ['birth-date', 'birth-time', 'birth-timezone', 'birth-fold',
    'birth-latitude', 'birth-longitude']) assert.equal(f.$(id).value, '');
  assert.equal(f.$('birth-unknown').checked, false);
  assert.equal(f.$('birth-time').disabled, false);
  assert.equal(f.$('birth-time').required, true);
  assert.equal(f.$('birth-fold').disabled, false);
  assert.equal(f.$('profile-date').value, '2026-10-13');
  assert.equal(f.$('profile-result').hidden, true);
  for (const id of ['profile-chart', 'profile-breakdown', 'profile-score-chips', 'profile-omissions']) {
    assert.equal(f.$(id).children.length, 0);
  }
  assert.equal(f.$('profile-summary').textContent, '');
  assert.equal(f.controller.mode(), 'manual');
  assert.equal(f.applied.length, 1); // Existing numeric sliders may remain; no late overwrite.
});

test('unknown time sends null rather than fabricating noon', async () => {
  const f = fixture();
  f.$('birth-unknown').checked = true;
  await f.$('birth-unknown').emit('change');
  await completeFirst(f);
  assert.equal(JSON.parse(f.requests[0].options.body).birth_time, null);
});

test('one unavailable provider never applies the other provider as a fallback', async () => {
  const f = fixture();
  const pending = f.$('birth-form').emit('submit');
  const partial = profile();
  partial.systems.astrology = {system: 'astrology', status: 'error', highlights: [],
    omissions: [{field: 'astrology', reason: '분석기를 실행하지 못했어요.'}],
    profile: {status: 'unavailable', profile_scores: null}};
  partial.profile_status = 'unavailable';
  partial.combined = null;
  f.requests[0].resolve(partial);
  await pending;
  assert.deepEqual(f.applied, []);
  assert.notEqual(f.controller.mode(), 'automatic');
  assert.equal(f.$('profile-reapply').hidden, true);
  assert.equal(f.$('profile-omissions').children[1].textContent, 'The analysis could not run.');
});

test('legacy blended responses cannot silently populate the new independent interface', async () => {
  const f = fixture();
  const pending = f.$('birth-form').emit('submit');
  f.requests[0].resolve({schema_version: 1, profile_version: 'saju-zodiac-symbolic-v1',
    profile_scores: Object.fromEntries(keys.map(key => [key, 7])),
    natal: {}, today: {date: '2026-10-13'}, score_details: {}, omissions: []});
  await pending;
  assert.deepEqual(f.applied, []);
  assert.notEqual(f.controller.mode(), 'automatic');
  assert.match(f.$('profile-status').textContent, /Unsupported|format|version/);
});

test('birth coordinates enter profile calculation only and never the route callback', async () => {
  const f = fixture();
  f.$('birth-latitude').value = '37.5665';
  f.$('birth-longitude').value = '126.9780';
  f.$('birth-house-system').value = 'W';
  await completeFirst(f);
  const body = JSON.parse(f.requests[0].options.body);
  assert.equal(body.birth_latitude, 37.5665);
  assert.equal(body.birth_longitude, 126.9780);
  assert.equal(body.house_system, 'W');
  assert.deepEqual(Object.keys(f.applied[0]), keys);
});

test('profile saves only on explicit action and restores identity and birth inputs without analysis', async () => {
  const f = fixture();
  assert.equal(f.$('profile-delete-saved').hidden, true);
  f.$('profile-name').value = ' Test traveler ';
  f.$('birth-latitude').value = '40.7';
  f.$('birth-longitude').value = '-73.9';
  f.$('birth-house-system').value = 'W';
  f.$('birth-fold').value = '1';
  await f.$('birth-form').emit('input');
  assert.equal(f.storage.values.size, 0);
  await completeFirst(f);
  assert.equal(f.storage.values.size, 0);
  assert.equal(Object.hasOwn(JSON.parse(f.requests[0].options.body), 'name'), false);
  f.$('profile-date').value = '2027-01-02';
  f.$('external-api-key').value = 'synthetic-key-not-for-storage';
  await f.$('profile-save').emit('click');
  assert.equal(f.$('profile-delete-saved').hidden, false);
  assert.deepEqual([...f.storage.values.keys()], [storageKey]);
  const record = JSON.parse(f.storage.getItem(storageKey));
  assert.deepEqual(record, {version: 1, profile: {
    name: 'Test traveler', birth_date: '1990-05-15', birth_time: '14:30',
    birth_unknown: false, birth_timezone: 'Asia/Seoul', birth_latitude: 40.7,
    birth_longitude: -73.9, birth_house_system: 'W', birth_fold: 1,
  }});
  assert.deepEqual(f.identities.at(-1), {name: 'Test traveler', saved: true});
  const restored = fixture({storage: f.storage, seed: false});
  assert.equal(restored.$('profile-delete-saved').hidden, false);
  assert.equal(restored.$('profile-name').value, 'Test traveler');
  assert.equal(restored.$('birth-date').value, '1990-05-15');
  assert.equal(restored.$('birth-time').value, '14:30');
  assert.equal(restored.$('birth-timezone').value, 'Asia/Seoul');
  assert.equal(restored.$('birth-latitude').value, '40.7');
  assert.equal(restored.$('birth-longitude').value, '-73.9');
  assert.equal(restored.$('birth-house-system').value, 'W');
  assert.equal(restored.$('birth-fold').value, '1');
  assert.equal(restored.$('profile-date').value, '2026-10-13');
  assert.deepEqual(restored.identities.at(-1), {name: 'Test traveler', saved: true});
  assert.deepEqual(restored.requests, []);
  assert.deepEqual(restored.applied, []);
  assert.equal(restored.controller.mode(), 'manual');
});

test('saved unknown time restores blank disabled controls rather than an invented time', async () => {
  const f = fixture();
  f.$('birth-unknown').checked = true;
  f.$('birth-fold').value = '1';
  await f.$('birth-unknown').emit('change');
  await f.$('profile-save').emit('click');
  const record = JSON.parse(f.storage.getItem(storageKey));
  assert.equal(record.profile.birth_time, null);
  assert.equal(record.profile.birth_fold, null);
  const restored = fixture({storage: f.storage, seed: false});
  assert.equal(restored.$('birth-unknown').checked, true);
  assert.equal(restored.$('birth-time').value, '');
  assert.equal(restored.$('birth-time').disabled, true);
  assert.equal(restored.$('birth-time').required, false);
  assert.equal(restored.$('birth-fold').value, '');
  assert.equal(restored.$('birth-fold').disabled, true);
  await completeFirst(restored);
  const body = JSON.parse(restored.requests[0].options.body);
  assert.equal(body.birth_time, null);
  assert.equal(Object.hasOwn(body, 'birth_fold'), false);
});

test('name edits update identity without invalidating analysis or automatically updating saved data', async () => {
  const f = fixture();
  f.$('profile-name').value = 'Traveler';
  await completeFirst(f);
  await f.$('profile-save').emit('click');
  const stored = f.storage.getItem(storageKey);
  f.$('profile-name').value = 'New traveler';
  await f.$('birth-form').emit('input', {id: 'profile-name'});
  assert.equal(f.controller.mode(), 'automatic');
  assert.deepEqual(f.identities.at(-1), {name: 'New traveler', saved: false});
  assert.equal(f.storage.getItem(storageKey), stored);
  assert.match(f.$('profile-storage-status').textContent, /Save again/);
  await f.$('profile-save').emit('click');
  assert.deepEqual(f.identities.at(-1), {name: 'New traveler', saved: true});
});

test('input clear leaves saved information visible in status; deletion removes storage and cancels analysis', async () => {
  const f = fixture();
  f.$('profile-name').value = 'Traveler';
  await f.$('profile-save').emit('click');
  await completeFirst(f);
  const stored = f.storage.getItem(storageKey);
  f.controller.clear();
  assert.equal(f.$('profile-name').value, '');
  assert.equal(f.storage.getItem(storageKey), stored);
  assert.match(f.$('profile-storage-status').textContent, /Saved details remain/);
  assert.equal(f.$('profile-delete-saved').disabled, false);
  assert.equal(f.$('profile-delete-saved').hidden, false);
  assert.deepEqual(f.identities.at(-1), {name: '', saved: false});
  // A later pending calculation must not repopulate the cleared analysis.
  f.$('birth-date').value = '1990-05-15';
  f.$('birth-time').value = '14:30';
  f.$('birth-timezone').value = 'Asia/Seoul';
  const pending = f.$('birth-form').emit('submit');
  await f.$('profile-delete-saved').emit('click');
  assert.equal(f.requests.at(-1).options.signal.aborted, true);
  f.requests.at(-1).resolve(profile(9));
  await pending;
  assert.equal(f.storage.getItem(storageKey), null);
  assert.equal(f.$('birth-date').value, '');
  assert.equal(f.$('profile-result').hidden, true);
  assert.equal(f.$('profile-delete-saved').disabled, true);
  assert.equal(f.$('profile-delete-saved').hidden, true);
  assert.equal(f.applied.length, 1);
  assert.equal(f.controller.mode(), 'manual');
  assert.match(f.$('profile-storage-status').textContent, /deleted/);
});

test('invalid dates, time zones, time, fold and unpaired or out-of-range coordinates cannot be saved', async () => {
  const invalid = [
    ['birth-date', '1990-02-30'], ['birth-date', '1899-12-31'], ['birth-date', '2027-01-01'],
    ['birth-time', '24:10'], ['birth-time', ''], ['birth-timezone', 'Not/A_Timezone'],
    ['birth-fold', '2'], ['birth-latitude', '40'], ['birth-longitude', '-74'],
    ['birth-house-system', 'unsupported'], ['profile-name', 'x'.repeat(41)],
  ];
  for (const [id, value] of invalid) {
    const f = fixture();
    f.$(id).value = value;
    await f.$('profile-save').emit('click');
    assert.equal(f.storage.getItem(storageKey), null, id);
    assert.match(f.$('profile-storage-status').textContent, /Check your/);
  }
  for (const [latitude, longitude] of [['91', '0'], ['0', '-181'], ['NaN', '0'], ['0x10', '0']]) {
    const f = fixture();
    f.$('birth-latitude').value = latitude;
    f.$('birth-longitude').value = longitude;
    await f.$('profile-save').emit('click');
    assert.equal(f.storage.getItem(storageKey), null);
  }
});

test('malformed, unknown-version and non-whitelisted saved records are discarded safely', async () => {
  const valid = fixture();
  await valid.$('profile-save').emit('click');
  const record = JSON.parse(valid.storage.getItem(storageKey));
  const invalid = ['{', 'null', JSON.stringify({...record, version: 2}),
    JSON.stringify({...record, extra: 'not-allowed'}),
    JSON.stringify({...record, profile: {...record.profile, api_key: 'synthetic'}}),
    JSON.stringify({...record, profile: {...record.profile, birth_date: '1990-02-30'}}),
    JSON.stringify({...record, profile: {...record.profile, birth_unknown: true}}),
    JSON.stringify({...record, profile: {...record.profile, birth_latitude: '40', birth_longitude: '-74'}})];
  for (const raw of invalid) {
    const f = fixture({storage: memoryStorage([[storageKey, raw]]), seed: false});
    assert.equal(f.storage.getItem(storageKey), null);
    assert.equal(f.$('birth-date').value, '');
    assert.deepEqual(f.requests, []);
    assert.match(f.$('profile-storage-status').textContent, /could not be loaded/);
  }
});

test('blocked storage and quota failures leave manual calculation available and never claim success', async () => {
  const blocked = {getItem() { throw Error('denied'); }, setItem() { throw Error('quota'); },
    removeItem() { throw Error('denied'); }};
  const f = fixture({storage: blocked});
  assert.match(f.$('profile-storage-status').textContent, /storage is unavailable/);
  await f.$('profile-save').emit('click');
  assert.match(f.$('profile-storage-status').textContent, /could not be saved/);
  assert.equal(f.$('birth-date').value, '1990-05-15');
  assert.equal(f.$('profile-apply').disabled, false);
  await completeFirst(f);
  assert.equal(f.applied.length, 1);
  const saved = fixture();
  await saved.$('profile-save').emit('click');
  saved.storage.removeItem = () => { throw Error('denied'); };
  await saved.$('profile-delete-saved').emit('click');
  assert.notEqual(saved.storage.getItem(storageKey), null);
  assert.match(saved.$('profile-storage-status').textContent, /could not be deleted/);
  assert.equal(saved.$('birth-date').value, '1990-05-15');
});

test('legacy UI without optional profile storage elements still calculates and clears', async () => {
  const f = fixture({optionalElements: false});
  await completeFirst(f);
  assert.equal(f.applied.length, 1);
  f.controller.clear();
  assert.equal(f.controller.mode(), 'manual');
});


test('Korean daylight-saving validation displays English and opens the repeated-time control', async () => {
  const f = fixture();
  const pending = f.$('birth-form').emit('submit');
  f.requests[0].reject(Error('서머타임 종료로 같은 시간이 두 번 있어요. 첫 번째인지 두 번째인지 선택해 주세요.'));
  await pending;
  assert.match(f.$('profile-status').textContent, /birth time occurs twice/);
  assert.doesNotMatch(f.$('profile-status').textContent, /[가-힣]/);
  assert.equal(f.$('birth-fold').open, true);
  assert.equal(f.$('birth-fold').focused, true);
  assert.equal(f.$('profile-apply').disabled, false);
  assert.equal(f.$('profile-apply').textContent, 'Continue');
  assert.deepEqual(f.applied, []);
});


test('unmapped Korean backend text uses an English fallback without applying a profile', async () => {
  const f = fixture();
  const pending = f.$('birth-form').emit('submit');
  f.requests[0].reject(Error('아직 번역되지 않은 새로운 분석 안내입니다.'));
  await pending;
  assert.equal(f.$('profile-status').textContent, 'This detail is not available in English.');
  assert.equal(f.$('profile-apply').disabled, false);
  assert.equal(f.$('profile-apply').textContent, 'Continue');
  assert.deepEqual(f.applied, []);
});


test('native validation reveals collapsed ancestors before focusing a field and keeps native messages', () => {
  const f = fixture();
  const registration = f.$('birth-form').listenerRegistrations.find(item => item.name === 'invalid');
  assert.equal(registration.options, true, 'invalid must be captured because it does not bubble');
  const outer = {open: false, parentElement: {closest() { return null; }}};
  const inner = {open: false, parentElement: {closest() { return outer; }}};
  const field = f.$('profile-date');
  field.value = '';
  field.closest = () => inner;
  let defaultPrevented = false;
  registration.fn({target: field, preventDefault() { defaultPrevented = true; }});
  assert.equal(inner.open, true);
  assert.equal(outer.open, true);
  assert.equal(defaultPrevented, false, 'the browser must still show its validation message');
  assert.equal(field.focused, undefined, 'native validation chooses the first invalid field');
  assert.equal(f.$('profile-status').textContent, 'Check the highlighted field to continue.');
  assert.equal(f.$('profile-apply').disabled, false);
  assert.deepEqual(f.requests, []);
});
