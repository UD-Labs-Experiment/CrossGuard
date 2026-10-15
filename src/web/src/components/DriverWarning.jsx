import React from 'react';
import { AlertTriangle } from 'lucide-react';

export function DriverWarning({ metrics, lastAlert }) {
  const isEmergency = ['emergency','braking'].includes(metrics?.decision_state?.toLowerCase()) || metrics?.advisory_state === 'driver_responding';
  const isDecelerating = metrics?.decision_state?.toLowerCase() === 'advisory';
  const hasAlertText = Boolean(lastAlert && typeof lastAlert.text === 'string' && lastAlert.text.trim().length > 0);

  // Discreetly show ONLY when there is an active warning condition
  if (!hasAlertText && !isEmergency && !isDecelerating) {
    return null;
  }

  const warningText = hasAlertText
    ? lastAlert.text
    : isEmergency
    ? 'Hazard Detected: Collision Avoidance Braking'
    : 'Advisory Warning: Decelerating for Obstruction';

  return (
    <div className={`driver-warning-discreet ${isEmergency ? 'emergency' : 'advisory'}`} role="alert">
      <AlertTriangle size={16} className="warning-icon" />
      <span className="warning-text">{warningText}</span>
      {lastAlert?.timestamp !== undefined && (
        <span className="warning-time">t={Number(lastAlert.timestamp).toFixed(1)}s</span>
      )}
    </div>
  );
}

export default DriverWarning;
