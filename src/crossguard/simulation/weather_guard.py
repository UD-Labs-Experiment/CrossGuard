"""Weather readiness contract used only by the sole CARLA-owning bridge.

Warm-up frames happen before trial actors/recording. Observers must not call this.
"""
import math
import time


class WeatherReadinessError(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__('Weather readiness failed: ' + report['reason'])


def ensure_weather(world, parameters, expected, *, attempts=3, stable_frames=2,
                   tolerance=0.01, tick_timeout=10.0, clock=time.monotonic):
    if not isinstance(expected, dict) or not expected:
        raise ValueError('Weather must declare at least one numeric field')
    if attempts < 1 or stable_frames < 2 or tolerance < 0:
        raise ValueError('Invalid weather verification limits')
    for key, value in expected.items():
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Invalid requested weather field: ' + str(key))
    report = {'status': 'PENDING', 'expected': dict(expected), 'attempts': [],
              'tolerance': tolerance, 'required_stable_frames': stable_frames,
              'recording_started': False}
    start = clock()
    previous_frame = None
    for attempt in range(1, attempts + 1):
        entry = {'attempt': attempt, 'samples': []}
        report['attempts'].append(entry)
        consecutive = 0
        try:
            world.set_weather(parameters(**expected))
            for _ in range(stable_frames):
                frame = world.tick(tick_timeout)
                if isinstance(frame, bool) or not isinstance(frame, int) or (previous_frame is not None and frame <= previous_frame):
                    raise RuntimeError('CARLA did not advance to a fresh verification frame')
                previous_frame = frame
                weather = world.get_weather()
                actual = {}
                differences = {}
                for key, wanted in expected.items():
                    value = getattr(weather, key, None)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        differences[key] = 'missing or non-finite readback'
                    else:
                        actual[key] = value
                        if abs(value - wanted) > tolerance:
                            differences[key] = {'expected': wanted, 'actual': value}
                entry['samples'].append({'frame': frame, 'actual': actual,
                                         'differences': differences})
                consecutive = consecutive + 1 if not differences else 0
            if consecutive == stable_frames:
                report.update(status='VERIFIED', verified_frame=previous_frame,
                              elapsed_s=clock() - start)
                return report
        except Exception as exc:
            entry['error'] = str(exc)
    report.update(status='FAILED', elapsed_s=clock() - start,
                  reason=f'Actual weather did not match on {stable_frames} fresh frames after {attempts} bounded attempts')
    raise WeatherReadinessError(report)
