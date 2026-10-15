// Camera identity travels with the image; status acknowledgments can precede
// the first frame from a new sensor, especially while simulation is paused.
export function selectCameraImage(packet, cachedPacket, status) {
  const matches = candidate => candidate?.image &&
    candidate.view === (status.current_view || 'driver') &&
    candidate.status?.trial_id === status.trial_id &&
    candidate.image_mode === (status.display_mode || 'rgb');
  if (matches(packet)) return packet.image;
  if (matches(cachedPacket)) return cachedPacket.image;
  return null;
}
