import { useEffect, useRef, useState, useCallback } from 'react';

export function useWebSocket() {
  const [connected, setConnected] = useState(false);
  const [latestPacket, setLatestPacket] = useState(null);
  const [status, setStatus] = useState({
    state: 'stopped',
    system_enabled: true,
    receiver_mode: 'autonomous',
    perception_mode: 'rgb',
    current_view: 'driver',
    trial_id: 1,
    sim_time_s: 0.0,
    measured_fps: 0.0,
    error: null,
  });

  const wsRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);

  const connect = useCallback(() => {
    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // When served via Vite proxy or direct port
    const host = window.location.port === '5173' ? '127.0.0.1:8000' : window.location.host;
    const wsUrl = `${protocol}//${host}/ws`;

    console.log('[WebSocket] Connecting to:', wsUrl);
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log('[WebSocket] Connected');
      setConnected(true);
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'frame') {
          setLatestPacket(msg);
          if (msg.status) {
            setStatus((prev) => ({
              ...prev,
              ...msg.status,
              current_view: msg.view || prev.current_view,
              sim_time_s: msg.sim_time_s ?? prev.sim_time_s,
              measured_fps: msg.stream_fps ?? prev.measured_fps,
            }));
          }
        } else if (msg.type === 'status_update') {
          if (msg.status) {
            setLatestPacket((prev) => {
              if (['starting', 'stopped'].includes(msg.status.state) ||
                  (prev && prev.status?.trial_id !== msg.status.trial_id)) return null;
              return prev;
            });
            setStatus((prev) => ({ ...prev, ...msg.status }));
          }
        } else if (msg.type === 'ack') {
          if (msg.status === 'error') {
            console.error('[WebSocket] Command error acknowledged:', msg.error);
          }
        }
      } catch (err) {
        console.error('[WebSocket] Error parsing message:', err);
      }
    };

    ws.onerror = (err) => {
      console.warn('[WebSocket] Socket error observed');
    };

    ws.onclose = () => {
      console.log('[WebSocket] Closed. Scheduling reconnect...');
      setConnected(false);
      wsRef.current = null;
      reconnectTimeoutRef.current = setTimeout(connect, 2000);
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  const sendCommand = useCallback(async (action, params = {}) => {
    // Commands use HTTP acknowledgments so failures reach the caller.
    // The WebSocket remains the bounded live frame/telemetry stream.
    try {
      const resp = await fetch('/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, params }),
      });
      if (!resp.ok) {
        const errJson = await resp.json().catch(() => ({}));
        throw new Error(errJson.detail || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      if (data.acknowledged_state) {
        setStatus((prev) => ({ ...prev, ...data.acknowledged_state }));
      }
      return data;
    } catch (err) {
      console.error('[API] Command dispatch failed:', err);
      throw err;
    }
  }, []);

  return {
    connected,
    latestPacket,
    status,
    sendCommand,
  };
}
