"""Choose an unoccupied native lane spawn before creating an actor; never relocate it."""
def solve_projected_distance(query, project, target, initial, tolerance=.01):
    """Arc length and longitudinal physical clearance differ on curved lane segments."""
    low,high=0.,max(initial*1.5,initial+10.)
    for _ in range(40):
        candidates=query(high)
        if not candidates:raise RuntimeError('Native upstream route unavailable for physical placement')
        if project(candidates[0])>=target:break
        high*=1.5
    else:raise RuntimeError('Cannot bracket native physical placement')
    for _ in range(45):
        mid=(low+high)/2
        candidates=query(mid)
        if not candidates:raise RuntimeError('Native upstream route unavailable for physical placement')
        pose=candidates[0];distance=project(pose)
        if abs(distance-target)<=tolerance:return pose,mid
        if distance<target:low=mid
        else:high=mid
    raise RuntimeError('Physical lane placement did not converge')
