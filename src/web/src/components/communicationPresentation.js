export const recipientLabel = id => ({ hero_car: 'A', follower_b: 'B', follower_c: 'C', rsu_camera: 'RSU', rsu: 'RSU', bus_camera: 'Bus', bus_v2v: 'Bus', bus: 'Bus' }[id] || id || 'Unspecified');
export const channelLabel = id => ({ radio: 'Direct radio', cellular: 'Cellular', optical: 'Optical', pc5: 'PC5 radio', uu: 'Cellular Uu', vlc: 'Optical VLC', lifi: 'LiFi' }[id] || id || 'Unspecified');

export function latestLinks(links) {
  const unique = new Map();
  for (const link of [...(Array.isArray(links) ? links : [])].reverse()) {
    const key = `${link.sender_id}:${link.receiver_id}:${link.channel}`;
    if (!unique.has(key)) unique.set(key, link);
  }
  return [...unique.values()].slice(0, 24);
}

export function filterEvents(events, recipient, channel) {
  return (Array.isArray(events) ? events : [])
    .filter(event => (!recipient || event.receiver_id === recipient) && (!channel || event.channel === channel))
    .sort((a, b) => (b.at_s || 0) - (a.at_s || 0));
}
