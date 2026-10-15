import React, { useState } from 'react';
import { X, Radio, Smartphone } from 'lucide-react';
import { recipientLabel, channelLabel, latestLinks, filterEvents } from './communicationPresentation';

const num = (v, digits = 0) => Number.isFinite(v) ? v.toFixed(digits) : '—';

function LinkDiagram({ links }) {
  const nodes = [
    { id: 'RSU', x: 38, y: 32 }, { id: 'Bus', x: 38, y: 96 },
    { id: 'A', x: 258, y: 24 }, { id: 'B', x: 258, y: 64 }, { id: 'C', x: 258, y: 104 },
  ];
  const point = id => nodes.find(node => node.id === recipientLabel(id));
  return <svg className="link-diagram" viewBox="0 0 300 136" role="img" aria-label="Latest modeled communication paths. Solid lines are delivered, dashed lines pending, dotted lines blocked or dropped. Details appear below.">
    {links.map((link, i) => {
      const a = point(link.sender_id), b = point(link.receiver_id);
      if (!a || !b) return null;
      const path = a.x === b.x ? `M ${a.x - 14} ${a.y} Q 204 ${(a.y + b.y) / 2} ${b.x - 14} ${b.y}` : `M ${a.x + 14} ${a.y} L ${b.x - 14} ${b.y}`;
      return <path key={i} d={path} className={`link-line ${link.state || 'pending'}`}><title>{`${recipientLabel(link.sender_id)} to ${recipientLabel(link.receiver_id)} · ${channelLabel(link.channel)} · ${link.state}: ${link.reason || 'No reason supplied'}`}</title></path>;
    })}
    {nodes.map(node => <g key={node.id}><circle cx={node.x} cy={node.y} r="14"/><text x={node.x} y={node.y + 4}>{node.id}</text></g>)}
    <text className="diagram-caption" x="150" y="132">Latest attempts · geometry is schematic</text>
  </svg>;
}

export function V2XOverlay({ events, communication, status, onCommand, onClose }) {
  const [recipient, setRecipient] = useState('');
  const [channel, setChannel] = useState('');
  const eventList = Array.isArray(events) ? events : [];
  const c = communication || {};
  const links = latestLinks(c.links);
  const shown = filterEvents(eventList, recipient, channel);
  const recipients = [...new Set(eventList.map(event => event.receiver_id).filter(Boolean))].sort();
  const channels = [...new Set(eventList.map(event => event.channel).filter(Boolean))].sort();
  const phones = Array.isArray(c.phone_advisories) ? c.phone_advisories : [];
  return (
    <section className="v2x-panel-overlay" aria-label="Communication model and message log">
      <header className="overlay-header">
        <div className="overlay-title-group"><Radio size={15} /><h3>Communication</h3><span className="overlay-badge">Model</span></div>
        <button type="button" className="overlay-close-btn" onClick={onClose} aria-label="Close communication"><X size={15} /></button>
      </header>
      <div className="overlay-content communication-content">
        <section className="communication-section">
          <h4>Delivery paths · {c.profile || status?.communication_profile || 'Waiting'}</h4>
          <p className="communication-note">Simulated radio, network and optical links. No physical transceiver or LiFi hardware.</p>
          <div className="communication-fault-controls">
            <label><input type="checkbox" checked={status?.optical_enabled !== false} disabled={status?.state === 'starting'} onChange={e => onCommand('communication', { optical_enabled: e.target.checked })} /> Optical transmitter available</label>
            <label><input type="checkbox" checked={status?.phone_available !== false} disabled={status?.state === 'starting'} onChange={e => onCommand('communication', { phone_available: e.target.checked })} /> Phone app available</label>
          </div>
          <p className="communication-note">Availability changes start a fresh scenario.</p>
          <LinkDiagram links={links} />
          <p className="communication-note">Solid: delivered · dashed: pending · dotted: unavailable</p>
          {(c.channels || []).length > 0 ? <div className="channel-table-scroll"><table className="channel-table"><caption className="sr-only">Channel delivery counts</caption>
            <thead><tr><th>Channel</th><th>Sent</th><th>Delivered</th><th>Lost</th><th>Blocked</th><th>Pending</th></tr></thead>
            <tbody>{c.channels.map(item => <tr key={item.id}><th scope="row">{item.label || channelLabel(item.id)}</th><td>{item.sent ?? 0}</td><td>{item.delivered ?? 0}</td><td>{item.dropped ?? 0}</td><td>{item.blocked ?? 0}</td><td>{item.pending ?? 0}</td></tr>)}</tbody>
          </table></div> : <p className="communication-note">Channel totals appear when the scenario starts.</p>}
          <div className="link-evidence-list">{links.slice(0, 9).map((link, i) => <div className="link-evidence" key={i}>
            <strong>{recipientLabel(link.sender_id)} → {recipientLabel(link.receiver_id)} · {channelLabel(link.channel)} · {link.state}</strong>
            <span>{link.reason || 'No failure reason'} · {num(link.distance_m, 1)} m · {num(link.latency_ms)} ms</span>
          </div>)}</div>
        </section>
        <section className="communication-section">
          <h4><Smartphone size={14} /> Phone advisory model</h4>
          <p className="communication-note">Active-app delivery and modeled driver reaction. Public emergency cell broadcast is a deployment discussion, not a connected service.</p>
          {phones.length ? phones.slice(-3).reverse().map((phone, i) => <article className="phone-advisory" key={i}>
            <strong>{recipientLabel(phone.receiver_id)} · {phone.state || phone.event || 'Advisory'}</strong>
            <p>{phone.message || 'Pedestrian ahead. Slow down.'}</p>
            <span>Channel: {channelLabel(phone.channel)} · observed t={num(phone.observed_at_s, 2)} s</span>
            <span>App delivery t={num(phone.delivered_at_s, 2)} s · age at delivery {num(Number.isFinite(phone.delivered_at_s) && Number.isFinite(phone.observed_at_s) ? phone.delivered_at_s - phone.observed_at_s : null, 2)} s</span>
            <span>{phone.state === 'reacted' ? `Driver reacted t=${num(phone.at_s, 2)} s · reaction delay ${num(Number.isFinite(phone.delivered_at_s) ? phone.at_s - phone.delivered_at_s : null, 2)} s` : `Reaction due t=${num(phone.response_due_at_s, 2)} s · reaction not recorded`}</span>
            {phone.reason && <span>{phone.reason}</span>}
          </article>) : <p className="communication-note">No phone advisory delivered in this trial.</p>}
          <p className="communication-note">The handset displays car A's alert. A/B/C delivery and reaction evidence is listed here. Audio on enables the handset alarm during fresh alerts; pause, mute or close Phone to stop it.</p>
        </section>
        <section className="communication-section">
          <h4>Braking coordination</h4>
          <p className="communication-note">{c.braking_event_count ?? (Array.isArray(c.braking_events) ? c.braking_events.length : 0)} modeled braking-message events this trial.</p>
        </section>
        <section className="communication-section">
          <h4>Addressed message log <span className="overlay-badge">{shown.length}</span></h4>
          <div className="message-filters">
            <label>Recipient<select value={recipient} onChange={e => setRecipient(e.target.value)}><option value="">All recipients</option>{recipients.map(id => <option key={id} value={id}>{recipientLabel(id)}</option>)}</select></label>
            <label>Channel<select value={channel} onChange={e => setChannel(e.target.value)}><option value="">All channels</option>{channels.map(id => <option key={id} value={id}>{channelLabel(id)}</option>)}</select></label>
          </div>
          {shown.length ? <div className="v2x-events-container">{shown.slice(0, 100).map((event, i) => <article className="v2x-event-item" key={`${event.event_id}:${event.event}:${event.at_s}:${i}`}>
            <div className="v2x-event-head"><strong className="v2x-event-name">{event.event || 'Event'}</strong><span className="v2x-event-time">t={num(event.at_s, 2)} s</span></div>
            <div className="v2x-event-meta"><span>{recipientLabel(event.sender_id || event.source_id)} → <strong>{recipientLabel(event.receiver_id)}</strong></span><span>{channelLabel(event.channel)}</span></div>
            {event.reason && <p className="communication-note">{event.reason}</p>}
            {event.event_id && <div className="v2x-event-id">Message <code>{event.event_id}</code></div>}
          </article>)}</div> : <p className="communication-note">No messages match these recipients and channels yet.</p>}
          {shown.length > 100 && <p className="communication-note">Showing the latest 100 matching events. Full evidence is saved with the run.</p>}
        </section>
      </div>
    </section>
  );
}

export default V2XOverlay;
