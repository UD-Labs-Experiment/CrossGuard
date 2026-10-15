import React from 'react';
import { VideoOff, Play } from 'lucide-react';
import { DriverWarning } from './DriverWarning';

export function VideoCanvas({
  imageSrc,
  currentView,
  metrics,
  status,
  connected,
  lastAlert,
  onStartClick,
}) {
  const isStopped = status.state === 'stopped';

  return (
    <div className="video-canvas-container">
      {/* 1. Camera Image Frame */}
      {imageSrc ? (
        <img
          src={imageSrc}
          alt={`Camera stream: ${currentView}`}
          className="camera-video-frame"
        />
      ) : null}

      {/* 2. Initial Stopped State Placeholder (only if no image has streamed yet) */}
      {!imageSrc && connected && isStopped && (
        <div className="canvas-empty-state">
          <button
            type="button"
            className="initial-start-btn"
            onClick={onStartClick}
            title="Start Simulation (Shortcut: Space)"
          >
            <Play size={40} fill="currentColor" />
            <span>Launch Simulation</span>
          </button>
        </div>
      )}

      {/* 3. Stream Waiting Placeholder (if running/paused but no frame received yet) */}
      {!imageSrc && connected && !isStopped && status.state !== 'starting' && (
        <div className="canvas-empty-state">
          <VideoOff size={32} />
          <span>Awaiting camera frame stream...</span>
        </div>
      )}

      {/* 4. Discreet Driver Warning (ONLY active when warning/alert occurs, NO permanent badges!) */}
      <DriverWarning metrics={metrics} lastAlert={lastAlert} />
    </div>
  );
}

export default VideoCanvas;
