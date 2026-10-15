export function startPhoneAlarm(ctx, speak, timers = globalThis) {
  const sounds = new Set();
  const burst = () => {
    for (let i = 0; i < 3; i++) {
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      const at = ctx.currentTime + i * .25;
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(960, at);
      gain.gain.setValueAtTime(.001, at);
      gain.gain.exponentialRampToValueAtTime(.13, at + .015);
      gain.gain.exponentialRampToValueAtTime(.001, at + .16);
      oscillator.connect(gain); gain.connect(ctx.destination);
      sounds.add(oscillator);
      oscillator.onended = () => { sounds.delete(oscillator); oscillator.disconnect?.(); gain.disconnect?.(); };
      oscillator.start(at); oscillator.stop(at + .18);
    }
  };
  burst();
  const voice = timers.setTimeout(() => speak('Road hazard ahead. Slow down.'), 850);
  const repeat = timers.setInterval(burst, 2400);
  return () => {
    timers.clearTimeout(voice); timers.clearInterval(repeat);
    for (const sound of sounds) { try { sound.stop(); } catch {} }
    sounds.clear();
  };
}
