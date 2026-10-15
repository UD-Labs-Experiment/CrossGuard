import { useState, useEffect, useRef, useCallback } from 'react';
import { phoneAlertActive } from '../components/phonePresentation.js';
import { startPhoneAlarm } from './phoneAlarm.js';

export function useAudioHMI({ events, trialId, receiverMode, simState, metrics, phoneVisible = false, simTime }) {
  const [audioEnabled, setAudioEnabled] = useState(false);
  const [audioError, setAudioError] = useState('');
  const [lastAlert, setLastAlert] = useState(null);
  const [speechAvailable] = useState(() => 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window);
  const processed = useRef(new Set());
  const context = useRef(null);
  const utterance = useRef(null);
  const alertTimer = useRef(null);
  const lastSpokenAt = useRef(-Infinity);
  const pendingWarning = useRef(null);
  const generation = useRef(0);
  const autonomousHazard = useRef(false);
  const alarmActive = phoneVisible && receiverMode === 'retrofit' && simState === 'running'
    && phoneAlertActive(metrics?.communication, simTime);
  const wanted = useRef((() => {
    try { return window.localStorage?.getItem('crossguard_audio') !== 'muted'; }
    catch { return true; }
  })());
  const remember = useCallback((enabled) => {
    wanted.current = enabled;
    try { window.localStorage?.setItem('crossguard_audio', enabled ? 'enabled' : 'muted'); } catch {}
  }, []);

  const cancelSpeech = useCallback(() => {
    generation.current += 1;
    pendingWarning.current = null;
    window.speechSynthesis?.cancel();
    utterance.current = null;
    setLastAlert(null);
    clearTimeout(alertTimer.current);
  }, []);

  const unlock = useCallback(async () => {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) throw new Error('This browser does not provide Web Audio.');
    if (!context.current || context.current.state === 'closed') context.current = new AudioContext();
    await context.current.resume();
    if (context.current.state !== 'running') throw new Error('Sound is blocked. Allow site audio and click Audio again.');
    return context.current;
  }, []);

  const chime = useCallback((ctx) => {
    const oscillator = ctx.createOscillator();
    const gain = ctx.createGain();
    const now = ctx.currentTime;
    oscillator.type = 'sine';
    oscillator.frequency.setValueAtTime(880, now);
    oscillator.frequency.setValueAtTime(660, now + .15);
    gain.gain.setValueAtTime(.18, now);
    gain.gain.exponentialRampToValueAtTime(.001, now + .4);
    oscillator.connect(gain); gain.connect(ctx.destination);
    oscillator.start(now); oscillator.stop(now + .4);
  }, []);

  const speak = useCallback((text) => {
    if (!speechAvailable) { setAudioError('Speech is unavailable in this browser. Chime and text remain available.'); return; }
    window.speechSynthesis.cancel();
    const message = new SpeechSynthesisUtterance(text);
    const voices = window.speechSynthesis.getVoices();
    const voice = voices.find(v => v.lang.startsWith('en') && v.localService) || voices.find(v => v.lang.startsWith('en'));
    if (voice) message.voice = voice;
    message.lang = voice?.lang || 'en-US'; message.rate = 1; message.volume = 1;
    // Keep a strong reference until playback ends (some browser engines require it).
    utterance.current = message;
    message.onerror = (e) => {
      if (!['interrupted','canceled'].includes(e.error)) setAudioError(`Speech failed: ${e.error}. Allow site sound, or use a browser with an installed English voice.`);
    };
    message.onend = () => { if (utterance.current === message) utterance.current = null; };
    window.speechSynthesis.resume();
    window.speechSynthesis.speak(message);
  }, [speechAvailable]);

  const playWarning = useCallback(async (warning) => {
    if (!warning || Date.now() - warning.receivedAt > 4000 || warning.at_s - lastSpokenAt.current < 2) return;
    const token = generation.current;
    lastSpokenAt.current = warning.at_s;
    try {
      const ctx = await unlock();
      if (token !== generation.current) return;
      pendingWarning.current = null;
      chime(ctx);
      speak('Pedestrian ahead. Slow down.');
    } catch (e) { setAudioError(e.message); }
  }, [unlock, chime, speak]);

  const enableAudio = useCallback(async () => {
    if (!wanted.current || audioEnabled) return;
    setAudioError('');
    try {
      // Speech request stays inside the originating click/keyboard gesture.
      const ready = unlock();
      speak('Audio alerts enabled.');
      const ctx = await ready;
      setAudioEnabled(true);
      remember(true);
      chime(ctx);
      await playWarning(pendingWarning.current);
    } catch (e) { setAudioEnabled(false); setAudioError(e.message); }
  }, [audioEnabled, unlock, speak, chime, remember, playWarning]);

  const testAudio = useCallback(async () => {
    setAudioError('');
    try {
      const ready = unlock();
      speak('Audio test. Pedestrian ahead. Slow down.');
      const ctx = await ready;
      setAudioEnabled(true); remember(true); chime(ctx);
      return true;
    } catch (e) { setAudioEnabled(false); setAudioError(e.message); return false; }
  }, [unlock, chime, speak, remember]);

  const toggleAudio = useCallback(async () => {
    if (audioEnabled) { remember(false); setAudioEnabled(false); cancelSpeech(); return; }
    remember(true);
    await enableAudio();
  }, [audioEnabled, enableAudio, remember, cancelSpeech]);

  useEffect(() => { cancelSpeech(); processed.current.clear(); autonomousHazard.current = false; lastSpokenAt.current = -Infinity; }, [trialId, cancelSpeech]);
  useEffect(() => {
    if (simState === 'stopped') {
      cancelSpeech();
      processed.current.clear();
      autonomousHazard.current = false;
      lastSpokenAt.current = -Infinity;
    }
  }, [simState, cancelSpeech]);
  useEffect(() => {
    if (receiverMode !== 'retrofit' || !['running', 'paused'].includes(simState)) return;
    for (const event of events || []) {
      if (!['notification_delivered','notification_cleared'].includes(event.event)) continue;
      const key = `${trialId}:${event.event}:${event.event_id}:${event.at_s}`;
      if (processed.current.has(key)) continue;
      processed.current.add(key);
      if (event.event === 'notification_cleared') {
        // Clearance must not cancel speech in the same render as delivery.
        // The delivered warning finishes; the visual expires on its own timer.
        continue;
      }
      if (event.channel !== 'simulated_audio') continue;
      const text = 'Pedestrian ahead. Slow down.';
      setLastAlert({text, eventId:event.event_id, timestamp:event.at_s, source:event.source_id});
      clearTimeout(alertTimer.current);
      alertTimer.current = setTimeout(() => setLastAlert(null), 4000);
      const warning = {...event, receivedAt: Date.now()};
      pendingWarning.current = warning;
      if (audioEnabled && !alarmActive) playWarning(warning);
    }
  }, [events, trialId, receiverMode, simState, audioEnabled, playWarning, alarmActive]);
  useEffect(() => {
    if (!alarmActive || !audioEnabled) return;
    let canceled = false;
    let stop = null;
    unlock().then(ctx => {
      if (!canceled) stop = startPhoneAlarm(ctx, speak);
    }).catch(error => { if (!canceled) setAudioError(error.message); });
    return () => {
      canceled = true;
      stop?.();
      window.speechSynthesis?.cancel();
    };
  }, [alarmActive, audioEnabled, trialId, unlock, speak]);
  useEffect(() => {
    if (receiverMode !== 'autonomous' || simState !== 'running') return;
    const hazard = Number.isFinite(metrics?.conflict_in_s) && metrics?.decision_state !== 'normal';
    if (!hazard) { autonomousHazard.current = false; return; }
    if (autonomousHazard.current) return;
    autonomousHazard.current = true;
    const warning = {at_s: metrics?.sim_time_s ?? metrics?.time_s ?? 0, receivedAt: Date.now()};
    pendingWarning.current = warning;
    setLastAlert({text:'Pedestrian ahead. Slow down.', source:'vehicle_policy', timestamp:warning.at_s});
    clearTimeout(alertTimer.current);
    alertTimer.current = setTimeout(() => setLastAlert(null), 4000);
    if (audioEnabled) playWarning(warning);
  }, [metrics, receiverMode, simState, audioEnabled, playWarning]);
  useEffect(() => () => { clearTimeout(alertTimer.current); window.speechSynthesis?.cancel(); context.current?.close(); }, []);
  return {audioEnabled, speechAvailable, lastAlert, audioError, toggleAudio, testAudio, enableAudio,
    clearAudioError: () => setAudioError(''), cancelSpeech};
}
