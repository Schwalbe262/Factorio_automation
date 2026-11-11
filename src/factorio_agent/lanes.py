"""Explicit lane contracts and ordinary output distribution across both lanes."""
from __future__ import annotations

from factorio_ai.deterministic_builder import _plan
from .board import entity
from .transport import VECTORS


def inserter_output_lane(pickup_direction,belt_direction,*,mirrored=False,straight=True):
    """Default 2.1 straight-belt rule; corners require native inspection."""
    if not straight:raise ValueError('curved belt drops require native lane evidence')
    if pickup_direction not in VECTORS or belt_direction not in VECTORS:
        raise ValueError('cardinal inserter and belt directions required')
    drop=VECTORS[(pickup_direction+8)%16];flow=VECTORS[belt_direction]
    if drop[0]*flow[0]+drop[1]*flow[1]:return 'left' if mirrored else 'right'
    left=(flow[1],-flow[0])
    return 'left' if drop[0]*left[0]+drop[1]*left[1]>0 else 'right'


def lane_balancer(x,y,item,direction=4):
    """Split the belt, then side-load opposite sides of a straight output.

    A splitter alone preserves lane identity. The two side-load junctions do
    the actual lane distribution. The empty west stub keeps the target straight.
    This accepts either or both input lanes of ONE declared item.
    """
    es=[entity('transport-belt',x,y,4),dict(entity('splitter',x+1,y-.5,4),_width=2,_height=1)]
    es += [entity('transport-belt',x+dx,y-1,4 if dx<5 else 8) for dx in range(2,6)]
    es += [entity('transport-belt',x+2,y,8)]
    es += [entity('transport-belt',x+dx,y+1,4 if dx<5 else 0) for dx in range(2,6)]
    es += [entity('transport-belt',x+dx,y,4) for dx in (4,5,6,7)]
    turns=((direction-4)%16)//4
    if direction not in (0,4,8,12):raise ValueError('cardinal lane-balancer direction required')
    def rotate(dx,dy):
        for _ in range(turns):dx,dy=-dy,dx
        return x+dx,y+dy
    for e in es:
        ex,ey=rotate(e['position']['x']-x,e['position']['y']-y)
        e['position']={'x':ex,'y':ey};e['direction']=(e['direction']+4*turns)%16
    ox,oy=rotate(7,0)
    ports=[{'kind':'item','item':item,'direction':'input','position':{'x':x,'y':y},'facing':direction,
            'lane_policy':'either-or-both lanes, same item'},
           {'kind':'item','item':item,'direction':'output','position':{'x':ox,'y':oy},'facing':direction,
            'lane_policy':'both lanes, same item','lane_items':{'left':item,'right':item}}]
    return _plan(es,ports,template='one_belt_output_lane_distribution',layout_policy='pcb-lane-distribution-v1',
                 scope='output lane distribution; not a claim of equal input-lane draw or saturated throughput')
