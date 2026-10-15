import assert from 'node:assert/strict';
import {test} from 'node:test';
import {startPhoneAlarm} from '../src/hooks/phoneAlarm.js';

test('alarm beeps before speech, repeats, and cancels all scheduled sound on cleanup', () => {
  let scheduledVoice, scheduledRepeat, stops=0, voiceCleared=false, repeatCleared=false;
  const starts=[],spoken=[];
  const ctx={currentTime:10,destination:{},createOscillator:()=>({frequency:{setValueAtTime(){}},connect(){},start(at){starts.push(at)},stop(){stops++}}),createGain:()=>({gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}})};
  const timers={setTimeout(fn,ms){scheduledVoice=fn;assert.equal(ms,850);return 1},setInterval(fn,ms){scheduledRepeat=fn;assert.equal(ms,2400);return 2},clearTimeout(id){voiceCleared=id===1},clearInterval(id){repeatCleared=id===2}};
  const stop=startPhoneAlarm(ctx,text=>spoken.push(text),timers);
  assert.deepEqual(starts,[10,10.25,10.5]);
  assert.deepEqual(spoken,[]);
  scheduledVoice(); assert.deepEqual(spoken,['Road hazard ahead. Slow down.']);
  scheduledRepeat(); assert.equal(starts.length,6);
  stop(); assert.equal(voiceCleared,true);assert.equal(repeatCleared,true);assert.equal(stops,12);
});
