import assert from 'node:assert/strict';
import { test } from 'node:test';
import { phonePresentation, phoneAlertActive } from '../src/components/phonePresentation.js';

test('phone notifications follow the selected recipient and recorded delivery', () => {
  const c = { phone_advisories: [
    { receiver_id: 'hero_car', state: 'unavailable', at_s: 2, delivered_at_s: null },
    { receiver_id: 'follower_b', state: 'reacted', delivered_at_s: 0, at_s: 1 },
  ] };
  assert.equal(phonePresentation(c, 'hero_car').delivered, false);
  assert.equal(phonePresentation(c, 'follower_b').delivered, true);
  assert.equal(phonePresentation(c, 'follower_b').label, 'Driver reacted');
  assert.equal(phonePresentation(c, 'follower_c').state, 'waiting');
});

test('expired offers cannot fabricate delivery, and expired reactions retain historical delivery', () => {
  const before = { receiver_id: 'hero_car', state: 'expired', at_s: 5, delivered_at_s: null };
  assert.equal(phonePresentation({ phone_advisories: [before] }, 'hero_car').delivered, false);
  const after = { ...before, state: 'expired_before_reaction', delivered_at_s: 3 };
  const view = phonePresentation({ phone_advisories: [after] }, 'hero_car');
  assert.equal(view.delivered, true);
  assert.equal(view.label, 'Alert expired before reaction');
});

test('missing telemetry explicitly waits or shows unavailable without a notification', () => {
  assert.equal(phonePresentation(null, 'hero_car').state, 'waiting');
  assert.equal(phonePresentation({}, 'hero_car', false).state, 'unavailable');
  assert.equal(phonePresentation({}, 'hero_car', false).delivered, false);
});

test('audible phone alerts require fresh delivery to A, never just a follower or failed offer', () => {
  const c = {phone_advisories:[{receiver_id:'hero_car',state:'reacted',delivered_at_s:2}]};
  assert.equal(phoneAlertActive(c, 3), true);
  assert.equal(phoneAlertActive(c, 7), false);
  assert.equal(phoneAlertActive(c, 1), false);
  assert.equal(phoneAlertActive({phone_advisories:[{receiver_id:'follower_b',state:'delivered',delivered_at_s:2}]},3), false);
  assert.equal(phoneAlertActive({phone_advisories:[{receiver_id:'hero_car',state:'expired_before_reaction',delivered_at_s:2}]},3), false);
});
