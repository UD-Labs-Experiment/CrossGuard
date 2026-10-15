"""Strict experiment inputs and transparent terminal rules."""
import math

def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Invalid {name}: expected finite number in [{low}, {high}]')
    return float(value)

def validate_follower_model(model, delay):
    if model not in ('ideal', 'delayed'):
        raise ValueError('Invalid follower model')
    number(delay, 'follower_delay_s', 0, 3)

def pedestrian_motion(behavior, elapsed, retreat_after, crossing_duration):
    if elapsed is None or behavior == 'wait':
        return 0.
    if behavior == 'retreat' and elapsed >= retreat_after:
        return -1.4 if elapsed < 2 * retreat_after else 0.
    return 1.4 if elapsed < crossing_duration else 0.

def trial_end_reason(now, crossing_at, all_clear, blocked_group, window):
    # An explicit observation window preserves stopped and post-impact followers.
    if window is not None:
        return 'observation_window_complete' if now >= window else None
    if all_clear:
        return 'route_exit'
    if blocked_group:
        return 'corridor_guard_stop'
    return 'watchdog_timeout' if now >= 45 else None

def experiment_config(params):
    allowed = {'town','vehicles','walkers','seed','weights','device','perception_mode','receiver_mode',
        'enabled','group_mode','following_gap_m','baseline_behavior','target_speed_kmh',
        'crossing_trigger_distance_m','pedestrian_behavior','retreat_after_s','scenario_definition',
        'scene_preflight','record_six_views','observation_window_s','follower_model','follower_delay_s',
        'communication_profile','optical_enabled','phone_available','study_options'}
    if set(params) - allowed:
        raise ValueError('Unknown experiment parameters: '+str(sorted(set(params)-allowed)))
    result = dict(params)
    if result.get('town','Town10HD') != 'Town10HD':
        raise ValueError('This study requires Town10HD')
    for key in ('enabled','record_six_views','scene_preflight','phone_available','optical_enabled'):
        if key in result and not isinstance(result[key],bool):
            raise ValueError('Invalid '+key)
    result.setdefault('enabled',False)
    result.setdefault('receiver_mode','autonomous')
    if result['receiver_mode'] not in ('autonomous','retrofit'):
        raise ValueError('Invalid receiver_mode')
    if result.get('perception_mode','rgb') != 'rgb':
        raise ValueError('Study perception must be RGB')
    validate_follower_model(result.get('follower_model','ideal'),result.get('follower_delay_s',1.))
    for key,low,high in [('target_speed_kmh',5,60),('following_gap_m',3,30),
                         ('crossing_trigger_distance_m',5,100),('observation_window_s',20,30)]:
        if key in result:
            result[key] = number(result[key],key,low,high)
    return result
