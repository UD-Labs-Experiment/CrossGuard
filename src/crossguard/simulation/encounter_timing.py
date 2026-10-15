"""One-shot physical approach timing. No input to pedestrian risk perception."""
import math

def arrival_time(distance, speed, acceleration, cruise_speed):
    """Measured free-flow acceleration continued to the declared cruise ceiling."""
    if distance <= 0: return 0.
    if speed <= 0: return None
    acceleration=max(0., acceleration)
    ceiling=max(speed, cruise_speed)
    if acceleration < .05 or speed >= ceiling: return distance/speed
    ramp=(ceiling-speed)/acceleration
    ramp_distance=speed*ramp+.5*acceleration*ramp*ramp
    if distance <= ramp_distance:
        return (math.sqrt(speed*speed+2*acceleration*distance)-speed)/acceleration
    return ramp+(distance-ramp_distance)/ceiling

def predict_encounter(car, pedestrian, lane_origin, forward, right, speed, acceleration,
                      cruise_speed, pedestrian_speed, actuator_delay_s=1/60,
                      pedestrian_acceleration_mps2=None):
    # Walking direction need not be perpendicular to A's lane (occluder heading differs).
    walk=(-right[0],-right[1])
    cross=lambda a,b:a[0]*b[1]-a[1]*b[0]
    denominator=cross(walk,forward)
    if abs(denominator)<1e-6: raise ValueError('Walking path parallel to vehicle lane')
    lateral=cross((lane_origin[0]-pedestrian[0],lane_origin[1]-pedestrian[1]),forward)/denominator
    point=[pedestrian[i]+lateral*walk[i] for i in range(2)]
    distance=sum((point[i]-car[i])*forward[i] for i in range(2))
    pedestrian_distance=lateral
    a=arrival_time(distance,speed,acceleration,cruise_speed)
    p=pedestrian_distance/pedestrian_speed+actuator_delay_s
    if pedestrian_acceleration_mps2 is not None:
        if pedestrian_acceleration_mps2 <= 0: raise ValueError('Positive walking acceleration required')
        ramp_distance=pedestrian_speed**2/(2*pedestrian_acceleration_mps2)
        p=(math.sqrt(2*max(0.,pedestrian_distance)/pedestrian_acceleration_mps2)
           if pedestrian_distance <= ramp_distance else
           pedestrian_distance/pedestrian_speed+pedestrian_speed/(2*pedestrian_acceleration_mps2)) + actuator_delay_s
    return {'conflict_center_xy_m':point,'A_distance_m':distance,'A_speed_mps':speed,
            'A_acceleration_mps2':acceleration,'A_predicted_arrival_after_s':a,
            'pedestrian_distance_m':pedestrian_distance,'pedestrian_predicted_arrival_after_s':p,
            'pedestrian_startup_acceleration_mps2':pedestrian_acceleration_mps2,
            'arrival_difference_s':None if a is None else a-p}

class EncounterTrigger:
    def __init__(self, tolerance_s=.25, minimum_speed_mps=6.):
        self.tolerance=tolerance_s; self.minimum_speed=minimum_speed_mps
        self.event=None; self.failure=None
    def update(self, prediction, time_s, frame, brake):
        if self.event is not None or self.failure is not None: return None
        delta=prediction['arrival_difference_s']
        if brake > .05 and prediction['A_speed_mps'] >= self.minimum_speed:
            self.failure={'reason':'A braked before synchronized trigger; no pedestrian retiming',
                          'time_s':time_s,'frame':frame,'prediction':prediction}
            return None
        if delta is None or prediction['A_speed_mps'] < self.minimum_speed: return None
        if delta < -self.tolerance:
            self.failure={'reason':'approach missed declared synchronization window',
                          'time_s':time_s,'frame':frame,'prediction':prediction}
        elif delta <= 0:
            self.event={'event':'scenario_trigger','at_s':time_s,'frame':frame,**prediction,
                        'A_predicted_arrival_s':time_s+prediction['A_predicted_arrival_after_s'],
                        'pedestrian_predicted_arrival_s':time_s+prediction['pedestrian_predicted_arrival_after_s'],
                        'timing_tolerance_s':self.tolerance,'one_shot':True}
            return self.event
        return None
