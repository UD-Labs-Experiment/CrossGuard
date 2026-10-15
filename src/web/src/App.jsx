import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { useAudioHMI } from './hooks/useAudioHMI';
import { VideoCanvas } from './components/VideoCanvas';
import { CameraSelector } from './components/CameraSelector';
import { ControlBar } from './components/ControlBar';
import { TelemetryOverlay } from './components/TelemetryOverlay';
import { CoordinationOverlay } from './components/CoordinationOverlay';
import { PhoneOverlay } from './components/PhoneOverlay';
import { V2XOverlay } from './components/V2XOverlay';
import { HelpOverlay } from './components/HelpOverlay';
import { CenterOverlays } from './components/CenterOverlays';
import { selectCameraImage } from './components/cameraPresentation';

export function App() {
  // WebSocket connection & simulation status
  const { connected, latestPacket, status, sendCommand } = useWebSocket();
  const [showPhone, setShowPhone] = useState(false);

  // Audio HMI hook: using strict API {audioEnabled, lastAlert, toggleAudio, testAudio, audioError, speechAvailable}
  const audioHook = useAudioHMI({
    events: latestPacket?.events || [],
    trialId: status.trial_id,
    receiverMode: status.receiver_mode,
    simState: status.state,
    metrics: latestPacket?.metrics,
    phoneVisible: showPhone,
    simTime: status.sim_time_s,
  });

  const {
    audioEnabled,
    speechAvailable,
    lastAlert,
    toggleAudio,
    testAudio,
    audioError,
    enableAudio,
    clearAudioError,
  } = audioHook;

  // Cached camera frame to ensure frame retention on finished trial dismissal
  const [cachedPacket, setCachedPacket] = useState(null);
  useEffect(() => {
    if (latestPacket?.image) {
      setCachedPacket(latestPacket);
    }
  }, [latestPacket]);

  useEffect(() => {
    if (status.state === 'starting') {
      setCachedPacket(null);
    }
  }, [status.state]);
  useEffect(() => { setCachedPacket(null); }, [status.trial_id]);

  // Overlays state
  const [showTelemetry, setShowTelemetry] = useState(false);
  const [coordinationCollapsed, setCoordinationCollapsed] = useState(true);
  const [showV2X, setShowV2X] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [completionDismissed, setCompletionDismissed] = useState(false);
  const [commandError, setCommandError] = useState(null);

  // Reset completion dismissal whenever trial leaves 'finished' state
  useEffect(() => {
    if (status.state !== 'finished') {
      setCompletionDismissed(false);
    }
  }, [status.state]);

  const currentView = status.current_view || 'driver';
  const availableViews = status.available_views || ['top', 'driver', 'person', 'opposite'];
  const metrics = status.state === 'finished' ? (status.terminal_metrics || latestPacket?.metrics) : latestPacket?.metrics;
  const imageSrc = selectCameraImage(latestPacket, cachedPacket, status);
  const events = latestPacket?.events || [];
  const isStarting = status.state === 'starting';
  useEffect(() => {
    if (status.receiver_mode !== 'retrofit') setShowPhone(false);
  }, [status.receiver_mode]);

  // Command execution with visible error propagation
  const handleCommand = useCallback(async (action, params = {}) => {
    try {
      setCommandError(null);
      if (action === 'start' || action === 'reset') await enableAudio();
      await sendCommand(action, params);
    } catch (err) {
      console.error(`[App] Command '${action}' failed:`, err);
      setCommandError(err.message || String(err));
    }
  }, [sendCommand, enableAudio]);

  // View switcher
  const handleViewChange = useCallback((viewName) => {
    if (!availableViews.includes(viewName)) return;
    handleCommand('view', { name: viewName });
  }, [handleCommand, availableViews.join(',')]);

  const handleTestAudio = testAudio;

  // Comprehensive keyboard navigation
  useEffect(() => {
    const handleKeyDown = (e) => {
      // Do not intercept if user is typing in form controls
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;

      if (e.repeat) return;
      if (status.state === 'starting' && !['h', 'H', 'Escape', 'o', 'O', 'v', 'V'].includes(e.key)) return;
      // 1-4: Camera views
      if (e.key === '1') {
        e.preventDefault();
        handleViewChange('top');
      } else if (e.key === '2') {
        e.preventDefault();
        handleViewChange('driver');
      } else if (e.key === '3') {
        e.preventDefault();
        handleViewChange('person');
      } else if (e.key === '4') {
        e.preventDefault();
        handleViewChange('opposite');
      } else if (e.key === '5') {
        e.preventDefault();
        handleViewChange('driver_b');
      } else if (e.key === '6') {
        e.preventDefault();
        handleViewChange('driver_c');
      }
      // Space: Pause / Resume / Start
      else if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        if (status.state === 'running') {
          handleCommand('pause');
        } else if (status.state === 'paused') {
          handleCommand('resume');
        } else if (status.state === 'stopped') {
          handleCommand('start');
        }
      }
      // S: Cooperative system toggle
      else if (e.key === 's' || e.key === 'S') {
        e.preventDefault();
        handleCommand('system', { enabled: !status.system_enabled });
      }
      // C: Receiver toggle (autonomous / retrofit)
      else if (e.key === 'c' || e.key === 'C') {
        e.preventDefault();
        const nextMode = status.receiver_mode === 'autonomous' ? 'retrofit' : 'autonomous';
        handleCommand('receiver', { mode: nextMode });
      }
      // R: Replay / Reset
      else if (e.key === 'r' || e.key === 'R') {
        e.preventDefault();
        handleCommand('reset', { enabled: status.system_enabled });
      }
      else if (e.key.toLowerCase() === 'y') {
        handleCommand('display', {mode:status.display_mode === 'yolo' ? 'rgb' : 'yolo'});
      }
      // O: Telemetry overlay
      else if (e.key === 'o' || e.key === 'O') {
        e.preventDefault();
        setShowTelemetry((prev) => !prev);
      }
      // V: V2X overlay
      else if (e.key === 'v' || e.key === 'V') {
        e.preventDefault();
        setShowV2X((prev) => !prev);
      }
      // M: Audio toggle
      else if (e.key === 'm' || e.key === 'M') {
        e.preventDefault();
        toggleAudio();
      }
      // H: Help overlay
      else if (e.key === 'h' || e.key === 'H') {
        e.preventDefault();
        setShowHelp((prev) => !prev);
      }
      // Escape: Close overlays
      else if (e.key === 'Escape') {
        e.preventDefault();
        setShowTelemetry(false);
        setShowV2X(false);
        setShowPhone(false);
        setShowHelp(false);
        setCommandError(null);
        if (status.state === 'finished' && !completionDismissed) {
          setCompletionDismissed(true);
        }
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    handleViewChange,
    handleCommand,
    toggleAudio,
    status.state,
    status.system_enabled,
    status.receiver_mode,
    status.display_mode,
    completionDismissed,
  ]);

  return (
    <div className="full-window-root">
      {/* 1. Full-Window Primary Camera Canvas */}
      <VideoCanvas
        imageSrc={imageSrc}
        currentView={currentView}
        metrics={metrics}
        status={status}
        connected={connected}
        lastAlert={lastAlert}
        onStartClick={() => handleCommand('start')}
      />

      {/* 2. Top Minimal Translucent Camera Selection Bar */}
      <CameraSelector
        currentView={currentView}
        onViewChange={handleViewChange}
        disabled={isStarting}
        displayMode={status.display_mode || 'rgb'}
        onToggleDisplay={() => handleCommand('display', {mode:status.display_mode === 'yolo' ? 'rgb' : 'yolo'})}
        availableViews={availableViews}
      />

      {/* 3. Bottom Minimal Translucent Control HUD */}
      <ControlBar
        status={status}
        audioEnabled={audioEnabled}
        showTelemetry={showTelemetry}
        showV2X={showV2X}
        showPhone={showPhone}
        showHelp={showHelp}
        onCommand={handleCommand}
        onToggleAudio={toggleAudio}
        onToggleTelemetry={() => setShowTelemetry((prev) => !prev)}
        onToggleV2X={() => { setShowV2X(prev => !prev); setShowPhone(false); }}
        onTogglePhone={async () => { if (!showPhone) await enableAudio(); setShowPhone(prev => !prev); setShowV2X(false); }}
        onToggleHelp={() => setShowHelp((prev) => !prev)}
      />

      {/* 4. Selectable Telemetry Overlay (O key or bottom button) */}
      <aside className="left-instrument-dock" aria-label="Vehicle instruments">
        <CoordinationOverlay metrics={metrics} status={status} onCommand={handleCommand} collapsed={coordinationCollapsed} onToggleCollapsed={() => setCoordinationCollapsed(prev => !prev)} />
      {showTelemetry && (
        <TelemetryOverlay
          metrics={metrics}
          status={status}
          latestPacket={latestPacket}
          onClose={() => setShowTelemetry(false)}
        />
      )}
      </aside>

      {status.receiver_mode === 'retrofit' && showPhone && <PhoneOverlay key={status.trial_id} communication={metrics?.communication} status={status} onClose={() => setShowPhone(false)} />}

      {/* 5. Selectable V2X Message Log Overlay (V key or bottom button) */}
      {showV2X && (
        <V2XOverlay
          events={events}
          communication={metrics?.communication}
          status={status}
          onCommand={handleCommand}
          onClose={() => setShowV2X(false)}
        />
      )}

      {/* 6. Help & Setup Modal (H key or bottom button) */}
      {showHelp && (
        <HelpOverlay
          onClose={() => setShowHelp(false)}
          onTestAudio={handleTestAudio}
          audioError={audioError}
          speechAvailable={speechAvailable}
        />
      )}

      {/* 7. Centered Overlays: Offline, Starting, Simulation Error, Completion */}
      <CenterOverlays
        connected={connected}
        status={status}
        metrics={metrics}
        commandError={commandError || (audioError ? `Audio: ${audioError}` : null)}
        completionDismissed={completionDismissed}
        onDismissCompletion={() => setCompletionDismissed(true)}
        onClearCommandError={() => { setCommandError(null); clearAudioError(); }}
        onReplay={() => handleCommand('reset', { enabled: status.system_enabled })}
      />
    </div>
  );
}

export default App;
