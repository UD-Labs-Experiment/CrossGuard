import React from 'react';
import { X, Gauge, CirclePause, Zap, Navigation, Clock, Video, Shield, Ruler, Timer, Cpu, Layers, Radio, Image, Hash, Camera } from 'lucide-react';

export function TelemetryOverlay({ metrics, status, latestPacket, onClose }) {
  const m = metrics || {};
  const time = m.sim_time_s ?? status.sim_time_s ?? 0;
  const value = (n, unit = '', digits = 1) => typeof n === 'number' ? `${n.toFixed(digits)} ${unit}`.trim() : 'Unavailable';
  const rows = [
    ['Speed', value(m.speed_kmh, 'km/h'), Gauge],
    ['Brake', value(m.brake, '', 2), CirclePause],
    ['Throttle', value(m.throttle, '', 2), Zap],
    ['Steering', value(m.steer, '', 2), Navigation],
    ['Simulation time', value(time, 's', 2), Clock],
    ['Camera FPS', value(latestPacket?.stream_fps ?? status.measured_fps), Video],
    ['Decision', m.decision_state || 'Unavailable', Shield],
    ['Stopping margin', value(m.stopping_margin_m, 'm'), Ruler],
    ['Conflict in', value(m.conflict_in_s, 's'), Timer],
    ['Inference latency', value(m.inference_latency_ms, 'ms'), Timer],
    ['Detector backend', m.detector_backend || 'Unavailable', Cpu],
    ['GPU', m.inference_gpu?.name || 'Unavailable', Cpu],
    ['RSU mount', m.device_mount?.type?.replace('_', ' ') || 'Not deployed', Radio],
    ['Completed batches', m.pipeline_completed_batches ?? 'Unavailable', Layers],
    ['Display', status.display_mode?.toUpperCase() || 'RGB', Image],
    ['Image frame', latestPacket?.image_frame_id ?? 'Unavailable', Hash],
  ];

  return (
    <section className="telemetry-panel-overlay" role="dialog" aria-label="Telemetry Overlay">
      <header className="overlay-header">
        <div className="overlay-title-group"><Gauge size={15} /><h3>Telemetry</h3></div>
        <button className="overlay-close-btn" onClick={onClose} aria-label="Close telemetry"><X size={15} /></button>
      </header>
      <div className="overlay-content telemetry-stack">
        {rows.map(([name, content, Icon]) => (
          <article className="telemetry-card" key={name}>
            <div className="telemetry-card-label"><Icon size={14} aria-hidden="true" /><span>{name}</span></div>
            <strong className="telemetry-card-value">{content}</strong>
            {name === 'Decision' && m.reason && <p className="telemetry-card-detail">{m.reason}</p>}
          </article>
        ))}
        {Object.entries(m.source_frames || {}).map(([source, frame]) => (
          <article className="telemetry-card" key={source}>
            <div className="telemetry-card-label"><Camera size={14} aria-hidden="true" /><span>{source}</span></div>
            <div className="telemetry-card-detail">Frame {frame.frame_id}</div>
            <div className="telemetry-card-detail">Capture age {value(Math.max(0, time - frame.captured_at_s) * 1000, 'ms', 0)}</div>
            <div className="telemetry-card-detail">Tracks {frame.ground_tracks ?? 0}</div>
          </article>
        ))}
      </div>
    </section>
  );
}
