import React from 'react';
import { X, Battery, LockKeyhole, TriangleAlert } from 'lucide-react';
import { phonePresentation } from './phonePresentation';

export function PhoneOverlay({ communication, status, onClose }) {
  const view = phonePresentation(communication, 'hero_car', status.phone_available !== false);
  const seconds = Math.max(0, Math.floor(status.sim_time_s || 0));
  const clock = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  return <section className="retrofit-phone-panel" aria-label="Phone alert display">
    <button type="button" className="phone-close" aria-label="Close phone" onClick={onClose}><X size={16} /></button>
    <div className="phone-device">
      <div className="phone-camera-island" aria-hidden="true"><i /></div>
      <div className={`phone-screen ${view.delivered ? 'phone-screen-alert' : ''}`}>
        <div className="phone-status-bar"><span>{clock}</span><Battery size={17} aria-hidden="true" /></div>
        {view.delivered ? <div className="phone-full-alert" role="alert">
          <TriangleAlert size={58} strokeWidth={1.5} aria-hidden="true" />
          <h2>Road hazard<br />ahead</h2>
          <p>Slow down</p>
        </div> : <div className="phone-standby">
          <LockKeyhole size={18} aria-hidden="true" />
          <strong>{clock}</strong>
          {view.state === 'unavailable' && <p>Alerts unavailable</p>}
        </div>}
        <span className="phone-home-indicator" aria-hidden="true" />
      </div>
    </div>
  </section>;
}
