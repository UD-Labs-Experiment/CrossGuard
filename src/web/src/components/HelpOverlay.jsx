import React, { useState } from 'react';
import { X, HelpCircle, Volume2, CheckCircle2, AlertCircle } from 'lucide-react';

const SHORTCUTS = [
  { key: '1', desc: 'Overhead camera viewpoint' },
  { key: '2', desc: 'Driver cabin viewpoint' },
  { key: '3', desc: 'Pedestrian viewpoint' },
  { key: '4', desc: 'Opposite approach viewpoint' },
  { key: '5 / 6', desc: 'Car B / C driver views (A/B/C scenarios)' },
  { key: 'Space', desc: 'Pause / Resume simulation' },
  { key: 'S', desc: 'Toggle Cooperative V2X System (ON/OFF)' },
  { key: 'C', desc: 'Toggle Receiver Mode (Autonomous / Retrofit)' },
  { key: 'R', desc: 'Replay / Reset scenario' },
  { key: 'Y', desc: 'Toggle raw RGB / YOLO bounding boxes for selected camera' },
  { key: 'O', desc: 'Toggle Telemetry overlay' },
  { key: 'V', desc: 'Toggle V2X message log overlay' },
  { key: 'M', desc: 'Toggle Audio HMI (Mute/Unmute)' },
  { key: 'H', desc: 'Toggle Help & Controls dialog' },
  { key: 'Esc', desc: 'Close open overlay dialogs' },
];

export function HelpOverlay({
  onClose,
  onTestAudio,
  audioError,
  speechAvailable,
}) {
  const [testStatus, setTestStatus] = useState(null);

  const handleTestClick = async () => {
    try {
      if (onTestAudio) {
        const res = await onTestAudio();
        if (res === false) { setTestStatus(null); return; }
        if (res && typeof res.then === 'function') {
          await res;
        }
        setTestStatus('Test requested. Listen for the chime and spoken warning; any playback error appears below.');
      }
    } catch (e) {
      setTestStatus(`Test failed: ${e.message || String(e)}`);
    }
  };

  return (
    <div className="help-modal-backdrop" role="dialog" aria-modal="true" aria-label="Help and Setup">
      <div className="help-modal-card">
        <div className="overlay-header">
          <div className="overlay-title-group">
            <HelpCircle size={16} />
            <h3>Simulation Help & Setup</h3>
          </div>
          <button
            type="button"
            className="overlay-close-btn"
            onClick={onClose}
            title="Close Help (Shortcut: H or Esc)"
          >
            <X size={15} />
          </button>
        </div>

        <div className="help-modal-content">
          {/* Section 1: Keyboard Shortcuts */}
          <section className="help-section">
            <h4 className="section-label">Keyboard Controls</h4>
            <div className="shortcuts-grid">
              {SHORTCUTS.map((item) => (
                <div key={item.key} className="shortcut-item">
                  <kbd className="shortcut-kbd">{item.key}</kbd>
                  <span className="shortcut-desc">{item.desc}</span>
                </div>
              ))}
            </div>
          </section>

          {/* Section 2: Audio HMI Verification */}
          <section className="help-section">
            <h4 className="section-label">Audio HMI & Speech Synthesis</h4>
            <div className="audio-test-box">
              <div className="audio-test-header">
                <button
                  type="button"
                  className="hud-btn btn-highlight"
                  onClick={handleTestClick}
                >
                  <Volume2 size={14} />
                  <span>Test Sound & Voice</span>
                </button>
                <span className="speech-support-badge">
                  SpeechSynthesis: {speechAvailable ? 'Supported' : 'Unavailable'}
                </span>
              </div>

              <p className="audio-explanation">
                <strong>Audio Output Requirements:</strong> Web Audio chime playback and SpeechSynthesis voice alerts
                require an initial user gesture (click) to satisfy browser autoplay security policies. Ensure your browser tab
                audio / site sound permissions and system speaker output are unmuted.
                <br />
                <em>Note: Microphone access is <strong>NOT</strong> requested or used (audio is output-only).</em>
              </p>

              {testStatus && (
                <div className="audio-test-feedback success">
                  <CheckCircle2 size={13} />
                  <span>{testStatus}</span>
                </div>
              )}

              {audioError && (
                <div className="audio-test-feedback error">
                  <AlertCircle size={13} />
                  <span>Audio System Error: {String(audioError)}</span>
                </div>
              )}
            </div>
          </section>

          {/* Section 3: Architecture & System Setup */}
          <section className="help-section">
            <h4 className="section-label">Simulation Architecture</h4><p className="help-body-text">RGB fills the window. YOLO shows the exact processed camera capture with real bounding boxes; it can lag live telemetry slightly. Y toggles between them.</p>
            <p className="help-body-text">
              The CARLA simulation runs server-side with a fixed 60 Hz simulation step. Driver, bus, and traffic-light RGB cameras feed GPU YOLO11n ONNX inference. Overhead follows car A; B and C cameras become available in group scenarios.
              Display FPS is measured separately from the simulation step. When occluded pedestrians or hazards
              are detected, cooperative V2X advisory messages are published to connected vehicles.
            </p>
            <ul className="help-bullet-list">
              <li><strong>A/B/C coordination:</strong> Starts collapsed; expand the top-left panel to inspect vehicles and experiment settings. Changing settings resets the scenario. Follower ranging is an ideal local sensor model.</li>
              <li><strong>Retrofit phone:</strong> Phone opens a full-screen hazard alert on the right-side handset. Enable Audio for repeated beeps followed by “Road hazard ahead. Slow down.” The handset represents A; per-car delivery and reaction timing appears in V2X. Pause, mute, close Phone or leave retrofit mode to stop the alarm.</li>
              <li><strong>Communication:</strong> Open V to inspect modeled links, deliveries, blocked optical paths, braking messages and phone advisories. These are simulated channels; no physical radio, LiFi device or public emergency-alert network is connected.</li>
              <li><strong>Autonomous Mode:</strong> Vehicle policy decodes incoming V2X kinematics and automatically decelerates or halts.</li>
              <li>Audio is initialized when you click Start or Replay, unless you previously muted it. Refreshing retains your mute preference. Autonomous speech follows a predicted crossing conflict; retrofit speech follows a delivered notification. Both use the same chime and voice.</li>
              <li><strong>Retrofit Mode:</strong> Simulates human-driven vehicles retrofitted with in-cabin audio HMI alert ("Pedestrian ahead. Slow down.").</li>
            </ul>
          </section>
        </div>
      </div>
    </div>
  );
}

export default HelpOverlay;
