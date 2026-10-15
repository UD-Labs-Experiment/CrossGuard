import React, { useEffect, useState } from 'react';
import { Car, ChevronDown, ChevronUp } from 'lucide-react';

const measurement = (value, digits = 1) => Number.isFinite(value) ? value.toFixed(digits) : '—';

export function CoordinationOverlay({ metrics, status, onCommand, collapsed, onToggleCollapsed }) {
  const m = metrics || {};
  const mode = status.group_mode || m.group_mode || 'single';
  const profile = status.communication_profile || m.communication?.profile || 'radio';
  const appliedGap = status.following_gap_m ?? 8;
  const [gap, setGap] = useState(String(appliedGap));
  const [applyingGap, setApplyingGap] = useState(false);
  useEffect(() => { setGap(String(appliedGap)); }, [appliedGap]);
  const gapValue = Number(gap);
  const validGap = gap.trim() !== '' && Number.isFinite(gapValue) && gapValue >= 3 && gapValue <= 30;
  const applyGap = async e => {
    e.preventDefault();
    if (!validGap || applyingGap) return;
    setApplyingGap(true);
    try { await onCommand('following_gap', { gap_m: gapValue }); }
    finally { setApplyingGap(false); }
  };
  const followers = m.group_vehicles || {};
  const cars = [{ id: 'hero_car', label: 'A', speed_kmh: m.speed_kmh, brake: m.brake, decision: m.decision_state },
    ...['follower_b', 'follower_c'].map((id, i) => ({ id, label: i === 0 ? 'B' : 'C', ...followers[id] }))];
  return (
    <section className="coordination-panel-overlay" aria-label="A/B/C coordination">
      <header className="overlay-header">
        <div className="overlay-title-group"><Car size={15} /><h3>A/B/C coordination</h3></div>
        <button className="overlay-close-btn" onClick={onToggleCollapsed} aria-label={collapsed ? 'Expand coordination' : 'Collapse coordination'} aria-expanded={!collapsed}>
          {collapsed ? <ChevronDown size={15} /> : <ChevronUp size={15} />}
        </button>
      </header>
      {!collapsed && <div className="coordination-content">
        <form className="following-gap-control" onSubmit={applyGap}>
          <label htmlFor="following-gap">Bumper gap · A–B and B–C (m)</label>
          <div className="following-gap-row">
            <input id="following-gap" type="number" min="3" max="30" step="0.5" required
              value={gap} onChange={e => setGap(e.target.value)} aria-describedby="following-gap-help"
              disabled={mode === 'single' || status.state === 'starting' || applyingGap} />
            <button type="submit" disabled={mode === 'single' || !validGap || gapValue === appliedGap || status.state === 'starting' || applyingGap}>
              {applyingGap ? 'Applying…' : 'Apply gap'}
            </button>
          </div>
          <p id="following-gap-help" className="coordination-note">{mode === 'single'
            ? 'Choose an A/B/C option in Experiment settings to adjust spacing.'
            : `Target ${appliedGap} m · range 3–30 m. Applying restarts the scenario. Actual gaps appear below.`}</p>
        </form>
        <table className="coordination-table">
          <caption className="sr-only">Live vehicle control and following gaps</caption>
          <thead><tr><th>Car</th><th>km/h</th><th>Brake</th><th>Gap m</th></tr></thead>
          <tbody>{cars.filter(car => mode !== 'single' || car.id === 'hero_car').map(car => <React.Fragment key={car.id}>
            <tr><th scope="row">{car.label}</th><td>{measurement(car.speed_kmh)}</td><td>{measurement(car.brake, 2)}</td><td>{measurement(car.gap_m)}</td></tr>
            <tr className="coordination-decision"><td colSpan="4">{car.label}: {car.decision || 'Waiting for telemetry'}{car.observation_age_s != null ? ` · evidence ${measurement(car.observation_age_s, 2)} s` : ''}</td></tr>
          </React.Fragment>)}</tbody>
        </table>
        {mode !== 'single' && <p className="coordination-note">Ideal ranging · secondary contact: <strong>{typeof m.secondary_collision !== 'boolean' ? 'Waiting for telemetry' : m.secondary_collision ? 'Recorded' : 'None recorded'}</strong></p>}
        <details className="experiment-settings">
          <summary>Experiment settings</summary>
          <div className="experiment-selectors">
            <label>Warning recipients
              <select value={mode} onChange={e => onCommand('group', { mode: e.target.value })} disabled={status.state === 'starting'}>
                <option value="single">A only · single car</option>
                <option value="lead-only">A only · A/B/C</option>
                <option value="cooperative">All relevant · A/B/C</option>
              </select>
            </label>
            <label>Communication model
              <select value={profile} onChange={e => onCommand('communication', { profile: e.target.value })} disabled={status.state === 'starting'}>
                <option value="radio">Direct radio</option><option value="cellular">Cellular</option>
                <option value="optical">Optical VLC / LiFi</option><option value="hybrid">Radio + cellular + optical</option>
              </select>
            </label>
          </div>
          <p className="coordination-note">Changing either selection resets the scenario.</p>
        </details>
      </div>}
    </section>
  );
}
