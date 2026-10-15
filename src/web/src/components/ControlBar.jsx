import React from 'react';
import {
  Play,
  Pause,
  Square,
  RotateCcw,
  Shield,
  ShieldAlert,
  Car,
  Cpu,
  Volume2,
  VolumeX,
  Gauge,
  MessageSquare,
  HelpCircle,
  Smartphone,
} from 'lucide-react';

export function ControlBar({
  status,
  audioEnabled,
  showTelemetry,
  showPhone,
  showV2X,
  showHelp,
  onCommand,
  onToggleAudio,
  onToggleTelemetry,
  onTogglePhone,
  onToggleV2X,
  onToggleHelp,
}) {
  const isStarting = status.state === 'starting';
  const isRunning = status.state === 'running';
  const isPaused = status.state === 'paused';
  const isStopped = status.state === 'stopped';
  const isFinished = status.state === 'finished';

  const handleStart = () => onCommand('start');
  const handlePauseResume = () => {
    if (isPaused) {
      onCommand('resume');
    } else {
      onCommand('pause');
    }
  };
  const handleStop = () => onCommand('stop');
  const handleReplay = () => onCommand('reset', { enabled: status.system_enabled });
  const handleToggleSystem = () => onCommand('system', { enabled: !status.system_enabled });
  const handleToggleReceiver = () => {
    const nextMode = status.receiver_mode === 'autonomous' ? 'retrofit' : 'autonomous';
    onCommand('receiver', { mode: nextMode });
  };

  return (
    <footer className="control-hud-bar" aria-label="Simulation Controls">
      {/* 1. Simulation Lifecycle Controls */}
      <div className="control-group">
        {isStopped ? (
          <button
            type="button"
            className="hud-btn btn-highlight"
            onClick={handleStart}
            disabled={isStarting}
            title="Start Simulation (Shortcut: Space)"
          >
            <Play size={14} fill="currentColor" />
            <span>Start</span>
          </button>
        ) : (
          <button
            type="button"
            className="hud-btn"
            onClick={handlePauseResume}
            disabled={isStarting || isFinished}
            title={isPaused ? "Resume Simulation (Shortcut: Space)" : "Pause Simulation (Shortcut: Space)"}
          >
            {isPaused ? <Play size={14} fill="currentColor" /> : <Pause size={14} />}
            <span>{isPaused ? 'Resume' : 'Pause'}</span>
          </button>
        )}

        <button
          type="button"
          className="hud-btn"
          onClick={handleStop}
          disabled={isStarting || isStopped}
          title="Stop Simulation"
        >
          <Square size={13} fill="currentColor" />
          <span>Stop</span>
        </button>

        <button
          type="button"
          className="hud-btn"
          onClick={handleReplay}
          disabled={isStarting}
          title="Replay / Reset Scenario (Shortcut: R)"
        >
          <RotateCcw size={13} />
          <span>Replay</span>
        </button>
      </div>

      <div className="hud-separator" />

      {/* 2. System and Receiver Mode Toggles */}
      <div className="control-group">
        <button
          type="button"
          className={`hud-btn ${status.system_enabled ? 'active-system' : 'inactive-system'}`}
          onClick={handleToggleSystem}
          disabled={isStarting}
          title="Toggle Cooperative V2X System (Shortcut: S)"
        >
          {status.system_enabled ? <Shield size={14} /> : <ShieldAlert size={14} />}
          <span>System: {status.system_enabled ? 'ON' : 'OFF'}</span>
        </button>

        <button
          type="button"
          className={`hud-btn ${status.receiver_mode === 'retrofit' ? 'active-retrofit' : 'active-autonomous'}`}
          onClick={handleToggleReceiver}
          disabled={isStarting}
          title="Toggle Vehicle Receiver Mode: Autonomous / Retrofit HMI (Shortcut: C)"
        >
          {status.receiver_mode === 'retrofit' ? <Car size={14} /> : <Cpu size={14} />}
          <span>{status.receiver_mode === 'retrofit' ? 'Retrofit' : 'Autonomous'}</span>
        </button>

        <button
          type="button"
          className={`hud-btn ${audioEnabled ? 'active-audio' : ''}`}
          onClick={onToggleAudio}
          disabled={isStarting}
          title={audioEnabled ? "Audio HMI Active (Shortcut: M to Mute)" : "Audio HMI Muted (Shortcut: M to Unmute)"}
        >
          {audioEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
          <span>{audioEnabled ? 'Audio on' : 'Audio off'}</span>
        </button>
      </div>

      <div className="hud-separator" />

      {/* 3. Overlay Toggles & Help */}
      <div className="control-group">
        <button
          type="button"
          className={`hud-btn hud-overlay-toggle ${showTelemetry ? 'active-overlay' : ''}`}
          onClick={onToggleTelemetry}
          title="Toggle Telemetry Overlay (Shortcut: O)"
        >
          <Gauge size={14} />
          <span>Telemetry</span>
        </button>

        {status.receiver_mode === 'retrofit' && <button
          type="button"
          className={`hud-btn hud-overlay-toggle ${showPhone ? 'active-overlay' : ''}`}
          onClick={onTogglePhone}
          aria-pressed={showPhone}
          title="Toggle simulated retrofit phone"
        >
          <Smartphone size={14} />
          <span>Phone</span>
        </button>}

        <button
          type="button"
          className={`hud-btn hud-overlay-toggle ${showV2X ? 'active-overlay' : ''}`}
          onClick={onToggleV2X}
          title="Toggle V2X Log Overlay (Shortcut: V)"
        >
          <MessageSquare size={14} />
          <span>V2X</span>
        </button>

        <button
          type="button"
          className={`hud-btn hud-overlay-toggle ${showHelp ? 'active-overlay' : ''}`}
          onClick={onToggleHelp}
          title="Toggle Controls & Help Overlay (Shortcut: H)"
        >
          <HelpCircle size={14} />
          <span>Help</span>
        </button>
      </div>
    </footer>
  );
}

export default ControlBar;
