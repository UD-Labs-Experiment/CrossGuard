import assert from 'node:assert/strict';
import {test} from 'node:test';
import React from 'react';
import {create,act} from 'react-test-renderer';
import {useAudioHMI} from '../src/hooks/useAudioHMI.js';

test('audio click unlocks playback; delivered warning survives immediate clearance and duplicate packets',async()=>{
 let spoken=[],canceled=0,chimes=0,api;
 global.window={speechSynthesis:{cancel(){canceled++;},resume(){},getVoices(){return [{lang:'en-US',localService:true}]},speak(u){spoken.push(u.text)}}};
 global.SpeechSynthesisUtterance=class{constructor(text){this.text=text}};
 window.SpeechSynthesisUtterance=global.SpeechSynthesisUtterance;
 window.AudioContext=class{
  state='suspended';currentTime=0;destination={};
  async resume(){this.state='running'}
  async close(){this.state='closed'}
  createOscillator(){return {frequency:{setValueAtTime(){}},connect(){},start(){chimes++},stop(){}}}
  createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}}}
 };
 function Harness(props){api=useAudioHMI(props);return null}
 const props={events:[],trialId:1,receiverMode:'retrofit',simState:'running'};
 let renderer;await act(async()=>{renderer=create(React.createElement(Harness,props))});
 await act(async()=>{await api.toggleAudio()});
 assert.equal(api.audioEnabled,true);assert.deepEqual(spoken,['Audio alerts enabled.']);
 const events=[{event:'notification_delivered',event_id:'real1',at_s:1,channel:'simulated_audio'},{event:'notification_cleared',event_id:'real1',at_s:1}];
 await act(async()=>{renderer.update(React.createElement(Harness,{...props,events}));});
 assert.equal(spoken.at(-1),'Pedestrian ahead. Slow down.');assert.equal(chimes,2);
 const count=canceled;
 await act(async()=>{renderer.update(React.createElement(Harness,{...props,events:[...events]}));});
 assert.equal(chimes,2);assert.equal(canceled,count);
 await act(async()=>{await api.testAudio()});assert.equal(chimes,3);assert.equal(spoken.at(-1),'Audio test. Pedestrian ahead. Slow down.');
 await act(async()=>{renderer.unmount()});
});

test('blocked Web Audio gives an actionable error instead of claiming audio is enabled',async()=>{
 let api;global.window={};
 function Harness(){api=useAudioHMI({events:[],trialId:1,receiverMode:'retrofit',simState:'running'});return null}
 let renderer;await act(async()=>{renderer=create(React.createElement(Harness))});
 await act(async()=>{await api.toggleAudio()});assert.equal(api.audioEnabled,false);assert.match(api.audioError,/Web Audio/);
 await act(async()=>{renderer.unmount()});
});
function audioHarness(props, preference = new Map()) {
 const spoken=[]; let api; let chimes=0; let stops=0;
 global.window={
  localStorage:{getItem:k=>preference.get(k),setItem:(k,v)=>preference.set(k,v)},
  speechSynthesis:{cancel(){},resume(){},getVoices(){return []},speak(u){spoken.push(u.text)}}
 };
 global.SpeechSynthesisUtterance=class{constructor(text){this.text=text}};
 window.SpeechSynthesisUtterance=global.SpeechSynthesisUtterance;
 window.AudioContext=class {
  state='suspended';currentTime=0;destination={};
  async resume(){this.state='running'} async close(){this.state='closed'}
  createOscillator(){return {frequency:{setValueAtTime(){}},connect(){},start(){chimes++},stop(){stops++}}}
  createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}}}
 };
 function Harness(p){api=useAudioHMI(p);return null}
 return {Harness,spoken,get api(){return api},get chimes(){return chimes},get stops(){return stops},props};
}

test('unmuting plays a recent warning that arrived while muted',async()=>{
 const props={events:[],trialId:1,receiverMode:'retrofit',simState:'running'};
 const h=audioHarness(props);let renderer;
 await act(async()=>{renderer=create(React.createElement(h.Harness,props))});
 const events=[{event:'notification_delivered',event_id:'muted-alert',at_s:1,channel:'simulated_audio'}];
 await act(async()=>{renderer.update(React.createElement(h.Harness,{...props,events}))});
 assert.equal(h.spoken.length,0);
 await act(async()=>{await h.api.toggleAudio()});
 assert.equal(h.spoken.at(-1),'Pedestrian ahead. Slow down.');
 await act(async()=>{renderer.update(React.createElement(h.Harness,{...props,events:[...events]}))});
 assert.equal(h.spoken.filter(t=>t==='Pedestrian ahead. Slow down.').length,1);
 await act(async()=>renderer.unmount());
});

test('autonomous predicted conflict plays a warning once per hazard episode',async()=>{
 const props={events:[],trialId:1,receiverMode:'autonomous',simState:'running',metrics:{decision_state:'normal',sim_time_s:0}};
 const h=audioHarness(props);let renderer;
 await act(async()=>{renderer=create(React.createElement(h.Harness,props))});
 await act(async()=>{await h.api.enableAudio()});
 const metrics={decision_state:'advisory',conflict_in_s:2.1,sim_time_s:1};
 await act(async()=>renderer.update(React.createElement(h.Harness,{...props,metrics})));
 await act(async()=>renderer.update(React.createElement(h.Harness,{...props,metrics:{...metrics,sim_time_s:1.1}})));
 assert.equal(h.spoken.filter(t=>t==='Pedestrian ahead. Slow down.').length,1);
 await act(async()=>renderer.update(React.createElement(h.Harness,{...props,metrics:{decision_state:'normal',sim_time_s:2}})));
 await act(async()=>renderer.update(React.createElement(h.Harness,{...props,metrics:{...metrics,sim_time_s:4}})));
 assert.equal(h.spoken.filter(t=>t==='Pedestrian ahead. Slow down.').length,2);
 // Stop/Start may retain the server trial id; reset the simulation-time rate limit.
 await act(async()=>renderer.update(React.createElement(h.Harness,{...props,simState:'stopped',metrics:{...metrics,sim_time_s:4}})));
 await act(async()=>renderer.update(React.createElement(h.Harness,{...props,metrics:{...metrics,sim_time_s:1}})));
 assert.equal(h.spoken.filter(t=>t==='Pedestrian ahead. Slow down.').length,3);
 await act(async()=>renderer.unmount());
});

test('an explicit mute persists after remount and Start does not undo it',async()=>{
 const prefs=new Map();const props={events:[],trialId:1,receiverMode:'autonomous',simState:'stopped'};
 let h=audioHarness(props,prefs);let renderer;
 await act(async()=>{renderer=create(React.createElement(h.Harness,props))});
 await act(async()=>{await h.api.enableAudio()});assert.equal(h.api.audioEnabled,true);
 await act(async()=>{await h.api.toggleAudio()});assert.equal(prefs.get('crossguard_audio'),'muted');
 await act(async()=>renderer.unmount());
 h=audioHarness(props,prefs);
 await act(async()=>{renderer=create(React.createElement(h.Harness,props))});
 await act(async()=>{await h.api.enableAudio()});assert.equal(h.api.audioEnabled,false);assert.equal(h.spoken.length,0);
 await act(async()=>renderer.unmount());
});

test('fresh phone alarm does not restart per packet and stops on pause or close',async()=>{
 const props={events:[],trialId:1,receiverMode:'retrofit',simState:'running',phoneVisible:true,simTime:3,metrics:{communication:{phone_advisories:[{receiver_id:'hero_car',state:'delivered',delivered_at_s:2}]}}};
 const h=audioHarness(props);let renderer;
 await act(async()=>{renderer=create(React.createElement(h.Harness,props))});
 await act(async()=>{await h.api.toggleAudio()});
 assert.equal(h.chimes,4,'unlock chime plus three alarm beeps');
 await act(async()=>{renderer.update(React.createElement(h.Harness,{...props,simTime:3.1,metrics:{communication:{phone_advisories:[{receiver_id:'hero_car',state:'delivered',delivered_at_s:2.1}]}}}))});
 assert.equal(h.chimes,4,'stream updates must not retrigger the burst');
 const stops=h.stops;
 await act(async()=>{renderer.update(React.createElement(h.Harness,{...props,simState:'paused'}))});
 assert.equal(h.stops,stops+3,'pause cancels every scheduled oscillator');
 await act(async()=>{renderer.update(React.createElement(h.Harness,props))});
 assert.equal(h.chimes,7);
 const beforeClose=h.stops;
 await act(async()=>{renderer.update(React.createElement(h.Harness,{...props,phoneVisible:false}))});
 assert.equal(h.stops,beforeClose+3);
 await act(async()=>{renderer.unmount()});
});
