import assert from 'node:assert/strict';
import { test } from 'node:test';
import { selectCameraImage } from '../src/components/cameraPresentation.js';

const status = { current_view: 'driver', trial_id: 1, display_mode: 'rgb', state: 'paused' };
const frame = { image: 'image-A', view: 'driver', image_mode: 'rgb', status: { trial_id: 1 } };

test('paused camera switch waits for the new sensor instead of relabeling the old frame', () => {
  const changed = { ...status, current_view: 'driver_b' };
  assert.equal(selectCameraImage(frame, frame, changed), null);
  assert.equal(selectCameraImage({ ...frame, image: 'image-B', view: 'driver_b' }, frame, changed), 'image-B');
});

test('finished trial retains its last matching image and rejects prior trial or display mode frames', () => {
  assert.equal(selectCameraImage(null, frame, { ...status, state: 'finished' }), 'image-A');
  assert.equal(selectCameraImage(frame, frame, { ...status, trial_id: 2 }), null);
  assert.equal(selectCameraImage(frame, frame, { ...status, display_mode: 'yolo' }), null);
  assert.equal(selectCameraImage({ image: 'unattributed' }, null, status), null);
});
