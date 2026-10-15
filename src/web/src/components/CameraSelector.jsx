import React from 'react';

const CAMERAS = [
  { id: 'top', key: '1', label: 'Overhead' },
  { id: 'driver', key: '2', label: 'Driver' },
  { id: 'person', key: '3', label: 'Pedestrian' },
  { id: 'opposite', key: '4', label: 'Opposite' },
  { id: 'driver_b', key: '5', label: 'Car B' },
  { id: 'driver_c', key: '6', label: 'Car C' },
];

export function CameraSelector({ currentView, onViewChange, disabled, displayMode, onToggleDisplay, availableViews }) {
  const views = availableViews || ['top', 'driver', 'person', 'opposite'];
  return (
    <nav className="camera-selector-nav" aria-label="Camera Selection">
      <button type="button" className="camera-btn" onClick={onToggleDisplay} disabled={disabled} title="Toggle raw RGB / actual YOLO detections (Y)">{displayMode === 'yolo' ? 'YOLO' : 'RGB'}</button>
      {CAMERAS.filter(cam => views.includes(cam.id)).map((cam) => {
        const isActive = currentView === cam.id;
        return (
          <button
            key={cam.id}
            type="button"
            className={`camera-btn ${isActive ? 'active' : ''}`}
            onClick={() => onViewChange(cam.id)}
            disabled={disabled}
            title={`Switch to ${cam.label} camera (Shortcut: ${cam.key})`}
          >
            <span className="camera-key">{cam.key}</span>
            <span className="camera-label">{cam.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

export default CameraSelector;
