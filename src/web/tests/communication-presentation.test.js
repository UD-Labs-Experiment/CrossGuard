import assert from 'node:assert/strict';
import { test } from 'node:test';
import { latestLinks, filterEvents, recipientLabel } from '../src/components/communicationPresentation.js';

test('redundant transport paths stay separate while a newer attempt replaces the same path', () => {
  const older = { sender_id: 'rsu', receiver_id: 'follower_b', channel: 'optical', state: 'sent' };
  const radio = { ...older, channel: 'radio', state: 'received' };
  const newer = { ...older, state: 'blocked', reason: 'vehicle_occlusion' };
  assert.deepEqual(latestLinks([older, radio, newer]), [newer, radio]);
  assert.equal(recipientLabel('follower_b'), 'B');
});

test('addressed log filters preserve recipient and transport identity and sort by simulation time', () => {
  const events = [
    { at_s: 2, receiver_id: 'follower_b', channel: 'radio', event: 'sent' },
    { at_s: 3, receiver_id: 'follower_c', channel: 'radio', event: 'received' },
    { at_s: 4, receiver_id: 'follower_b', channel: 'optical', event: 'blocked' },
    { at_s: 5, receiver_id: 'follower_b', channel: 'radio', event: 'received' },
  ];
  assert.deepEqual(filterEvents(events, 'follower_b', 'radio').map(e => e.at_s), [5, 2]);
  assert.deepEqual(filterEvents(events, '', 'optical').map(e => e.event), ['blocked']);
  assert.equal(events[0].at_s, 2, 'filtering must not reorder input evidence');
});

test('unavailable telemetry produces explicit empty presentation rather than invented paths', () => {
  assert.deepEqual(latestLinks(null), []);
  assert.deepEqual(filterEvents(null, '', ''), []);
  assert.equal(recipientLabel(undefined), 'Unspecified');
});
