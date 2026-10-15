import React from 'react';
import { AlertTriangle, CheckCircle, Loader2, X, RotateCcw, Eye } from 'lucide-react';

export function CenterOverlays({
  connected,
  status,
  metrics,
  commandError,
  completionDismissed,
  onDismissCompletion,
  onClearCommandError,
  onReplay,
}) {
  const isStarting = status.state === 'starting';
  const isFinished = status.state === 'finished';
  const isError = status.state === 'error';
  const m = metrics || {};

  return (
    <>
      {/* 1. Command Error Toast Notification (Top Center) */}
      {commandError && (
        <div className="command-error-toast" role="alert">
          <AlertTriangle size={15} />
          <span className="toast-message">Command failed: {commandError}</span>
          <button
            type="button"
            className="toast-close-btn"
            onClick={onClearCommandError}
            title="Dismiss error"
          >
            <X size={13} />
          </button>
        </div>
      )}

      {/* 2. Connection Offline Overlay */}
      {!connected && (
        <div className="center-overlay-backdrop">
          <div className="center-overlay-card">
            <Loader2 size={32} className="spinning-loader" />
            <h3>Connecting to Simulation Stream</h3>
            <p>Establishing WebSocket link to simulation backend (127.0.0.1:8000)...</p>
          </div>
        </div>
      )}

      {/* 3. Scenario Initializing Overlay */}
      {connected && isStarting && (
        <div className="center-overlay-backdrop">
          <div className="center-overlay-card">
            <Loader2 size={32} className="spinning-loader" />
            <h3>Initializing Scenario & Sensors</h3>
            <p>Spawning actors, configuring CARLA world, and warming inference pipeline...</p>
          </div>
        </div>
      )}

      {/* 4. Simulation Error Overlay */}
      {connected && isError && (
        <div className="center-overlay-backdrop">
          <div className="center-overlay-card error-card">
            <AlertTriangle size={36} className="error-icon" />
            <h3>Simulation Error</h3>
            <p>{status.error || 'CARLA or sensor communication error encountered.'}</p>
            <div className="overlay-actions">
              <button
                type="button"
                className="hud-btn btn-highlight"
                onClick={onReplay}
              >
                <RotateCcw size={13} />
                <span>Retry / Reset</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 5. Completion Overlay (Closable, retains last frame on dismissal) */}
      {connected && isFinished && !completionDismissed && (
        <div className="center-overlay-backdrop" role="dialog" aria-label="Trial Completion">
          <div className="center-overlay-card completion-card">
            <CheckCircle size={36} className="completion-icon" />
            <h3>Scenario Finished</h3>
            <p className="completion-reason">
              {m.end_reason === 'route_exit' ? 'Target vehicle left the crossing scene.' : (m.end_reason || 'Scenario ended.')}
            </p>

            <div className="completion-stats-grid">
              <div className="completion-stat">
                <span className="c-stat-label">Elapsed Time</span>
                <span className="c-stat-val">{Number(m.sim_time_s ?? status.sim_time_s ?? 0).toFixed(2)}s</span>
              </div>
              <div className="completion-stat">
                <span className="c-stat-label">Pedestrian Collision</span>
                <span className="c-stat-val">
                  {m.pedestrian_collision === true ? 'Recorded' : m.pedestrian_collision === false ? 'None recorded' : '--'}
                </span>
              </div>
              <div className="completion-stat">
                <span className="c-stat-label">Final Decision</span>
                <span className="c-stat-val">{m.decision_state || 'NOMINAL'}</span>
              </div>
            </div>

            <div className="overlay-actions">
              <button
                type="button"
                className="hud-btn"
                onClick={onDismissCompletion}
                title="Dismiss overlay to inspect final camera frame (Shortcut: Esc)"
              >
                <Eye size={13} />
                <span>Dismiss (Inspect Last Frame)</span>
              </button>

              <button
                type="button"
                className="hud-btn btn-highlight"
                onClick={onReplay}
                title="Replay trial scenario (Shortcut: R)"
              >
                <RotateCcw size={13} />
                <span>Replay Scenario</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export default CenterOverlays;
