export function phonePresentation(communication, receiverId, available = true) {
  const advisories = Array.isArray(communication?.phone_advisories) ? communication.phone_advisories : [];
  const advisory = advisories.find(item => item.receiver_id === receiverId);
  if (!advisory) return { state: available ? 'waiting' : 'unavailable', delivered: false, advisory: null };
  // Failed offers have timestamps too: only a recorded delivery counts as a notification.
  const delivered = Number.isFinite(advisory.delivered_at_s);
  const labels = { delivered: 'Alert delivered · reaction pending', reacted: 'Driver reacted',
    unavailable: 'Phone unavailable', expired: 'Alert expired before delivery',
    expired_before_reaction: 'Alert expired before reaction' };
  return { advisory, delivered, state: advisory.state,
    label: labels[advisory.state] || 'Waiting for phone evidence' };
}

export function phoneAlertActive(communication, simTime) {
  const view = phonePresentation(communication, 'hero_car');
  return view.delivered && ['delivered', 'reacted'].includes(view.state)
    && Number.isFinite(simTime) && simTime >= view.advisory.delivered_at_s
    && simTime - view.advisory.delivered_at_s <= 4;
}
