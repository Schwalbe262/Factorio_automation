"""PCB-style orthogonal trunks with bounded turns, detours and fixed ports."""
from __future__ import annotations

import math
from bisect import bisect_left


VECTORS = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}


def point(value):
    p = (float(value["x"]), float(value["y"])) if isinstance(value, dict) else tuple(map(float, value))
    if len(p) != 2 or not all(math.isfinite(v) for v in p):
        raise ValueError("finite two-coordinate point required")
    return p


def expand(vertices):
    path = [vertices[0]]
    for end in vertices[1:]:
        x, y = path[-1]
        ex, ey = end
        if x != ex and y != ey:
            raise ValueError("trunk segments must be orthogonal")
        dx, dy = ((ex > x) - (ex < x)), ((ey > y) - (ey < y))
        count = int(round(abs(ex - x) + abs(ey - y)))
        path.extend((x + dx * n, y + dy * n) for n in range(1, count + 1))
    return path


def metrics(path):
    directions = [next(d for d, v in VECTORS.items() if v == (round(b[0]-a[0]), round(b[1]-a[1])))
                  for a, b in zip(path, path[1:])]
    turns = sum(a != b for a, b in zip(directions, directions[1:]))
    length = max(0, len(path) - 1)
    direct = abs(path[-1][0] - path[0][0]) + abs(path[-1][1] - path[0][1])
    return {"length": length, "turns": turns, "manhattan_distance": direct,
            "detour_ratio": length / direct if direct else 1.0}


def clearance_lanes(blocked, a, b, limits):
    """Bounded lanes beside hard component edges near either port corridor."""
    result=[]
    for axis in (0,1):
        other=1-axis;lo=limits['min_x' if axis==0 else 'min_y'];hi=limits['max_x' if axis==0 else 'max_y']
        candidates=set();outer=set()
        for anchor in (a,b):
            rows={}
            for p in blocked:
                if abs(p[other]-anchor[other])<=4 and lo<=p[axis]<=hi:
                    rows.setdefault(p[other],set()).add(p[axis])
            values=set().union(*rows.values()) if rows else set()
            if values:
                outer.update(min(values)-n for n in (2,3));outer.update(max(values)+n for n in (2,3))
            for values in rows.values():
                for value in values:
                    if value-1 not in values:candidates.update(value-n for n in (1,2,3))
                    if value+1 not in values:candidates.update(value+n for n in (1,2,3))
        outer={n for n in outer if lo<=n<=hi}
        near=sorted((n for n in candidates if lo<=n<=hi),key=lambda n:(min(abs(n-a[axis]),abs(n-b[axis])),n))[:24]
        result.append(outer|set(near))
    return result


def blocked_port_prefix(vertices, blocked, limit=4):
    """Reject known hard cells in a candidate's short forced port escapes."""
    for points in (vertices,list(reversed(vertices))):
        current=points[0];direction=None;checked=0
        for end in points[1:]:
            dx,dy=end[0]-current[0],end[1]-current[1]
            if not dx and not dy:continue
            step=((dx>0)-(dx<0),(dy>0)-(dy<0))
            if direction is not None and step!=direction:break
            direction=step;distance=int(abs(dx)+abs(dy))
            for offset in range(1,min(distance,limit-checked)+1):
                if (current[0]+step[0]*offset,current[1]+step[1]*offset) in blocked:return True
            checked+=distance
            if checked>=limit:break
            current=end
    return False


def obstacle_index(blocked):
    """Sorted row/column positions for cheap whole straight-segment rejection."""
    rows={};columns={}
    for x,y in blocked:
        rows.setdefault(y,[]).append(x);columns.setdefault(x,[]).append(y)
    return ({y:sorted(xs)for y,xs in rows.items()},{x:sorted(ys)for x,ys in columns.items()})


def clear_skeleton(vertices,rows,columns,bounds,max_turns):
    """Canonical valid polyline, without walking or allocating any tile path."""
    points=[];directions=[]
    for p in vertices:
        if points and p==points[-1]:continue
        if points:
            dx,dy=p[0]-points[-1][0],p[1]-points[-1][1]
            if dx and dy:return None
            direction=((dx>0)-(dx<0),(dy>0)-(dy<0))
            if directions and direction==tuple(-v for v in directions[-1]):return None
            if directions and direction==directions[-1]:points[-1]=p;continue
            directions.append(direction)
        points.append(p)
    if len(directions)-1>max_turns:return None
    if any(not(bounds['min_x']<=x<=bounds['max_x']and bounds['min_y']<=y<=bounds['max_y'])for x,y in points):return None
    segments=list(zip(points,points[1:]))
    for a,b in segments:
        horizontal=a[1]==b[1];values=rows.get(a[1],[])if horizontal else columns.get(a[0],[])
        low,high=sorted((a[0],b[0])if horizontal else(a[1],b[1]));i=bisect_left(values,low)
        if i<len(values)and values[i]<=high:return None
    # A repeated tile would also be rejected after expansion; reject crossings now.
    for i,(a,b)in enumerate(segments):
        for c,d in segments[i+2:]:
            ah=a[1]==b[1];ch=c[1]==d[1]
            if ah and ch:
                hit=a[1]==c[1]and max(min(a[0],b[0]),min(c[0],d[0]))<=min(max(a[0],b[0]),max(c[0],d[0]))
            elif not ah and not ch:
                hit=a[0]==c[0]and max(min(a[1],b[1]),min(c[1],d[1]))<=min(max(a[1],b[1]),max(c[1],d[1]))
            else:
                h,v=((a,b),(c,d))if ah else((c,d),(a,b))
                hit=min(h[0][0],h[1][0])<=v[0][0]<=max(h[0][0],h[1][0])and min(v[0][1],v[1][1])<=h[0][1]<=max(v[0][1],v[1][1])
            if hit:return None
    return tuple(points)


def return_jog_skeletons(a,b,limits,start_direction,end_direction,rows,columns,trunk_lanes,clearance,max_turns):
    """Bounded local source return, followed by complete clearance trunks.

    A nearby parallel consumer can trap a straight port escape between two
    producers. One short sideways step and return can reach a different trunk
    without moving either port. These are whole six-turn candidates, not a
    tile search, and are considered only after the established families fail.
    """
    if(max_turns<6 or start_direction not in VECTORS or end_direction not in VECTORS
            or start_direction%8!=end_direction%8):return []
    axis=0 if start_direction%8==4 else 1;other=1-axis
    forward=VECTORS[start_direction][axis];arrival=VECTORS[end_direction][axis]
    def coords(u,v):
        p=[0,0];p[axis]=u;p[other]=v;return tuple(p)
    first=coords(a[axis]+forward,a[other])
    if clear_skeleton([a,first],rows,columns,limits,0)is None:return []
    bars=sorted(set(trunk_lanes[other])|clearance[other])
    low,high=limits['min_x'if axis==0 else'min_y'],limits['max_x'if axis==0 else'max_y']
    lanes=[a[axis]+i for i in range(math.ceil(low-a[axis]),math.floor(high-a[axis])+1)
           if(b[axis]-a[axis]-i)*arrival>0]
    lanes.sort(key=lambda lane:(abs(lane-b[axis]),lane))
    destinations={}
    for bar in bars:
        clear=[]
        for lane in lanes:
            tail=[coords(lane,bar),coords(lane,b[other]),b]
            if clear_skeleton(tail,rows,columns,limits,2)is not None:
                clear.append(lane)
                if len(clear)==16:break
        if clear:destinations[bar]=clear
    candidates=[]
    for escape in range(1,5):
        pe=coords(a[axis]+escape*forward,a[other])
        for jog in(-4,-3,-2,-1,1,2,3,4):
            corner=coords(pe[axis],a[other]+jog)
            for back in range(1,5):
                returned=coords(pe[axis]-back*forward,corner[other])
                for bar,ends in destinations.items():
                    prefix=[a,pe,corner,returned,coords(returned[axis],bar)]
                    if clear_skeleton(prefix,rows,columns,limits,4)is None:continue
                    candidates.extend(prefix+[coords(lane,bar),coords(lane,b[other]),b]for lane in ends)
    return candidates


def forward_jog_skeletons(a,b,limits,start_direction,end_direction,rows,columns,trunk_lanes,clearance,
                          max_turns,maximum_length,minimum_start_run=None):
    """Fourth family: small local jog, forward trunk and a complete crossbar.

    Middle trunks come from the existing absolute grid/native clearance edges.
    Only after the full source prefix and middle perpendicular trunk are clear do we keep
    up to sixteen nearest end columns with clear crossbar/arrival segments.
    No path nodes are allocated here and no expansion budget is reset.
    """
    if(max_turns<6 or start_direction not in VECTORS or end_direction not in VECTORS
            or start_direction%8!=end_direction%8):return []
    axis=0 if start_direction%8==4 else 1;other=1-axis
    forward=VECTORS[start_direction][axis];arrival=VECTORS[end_direction][axis]
    def coords(u,v):
        p=[0,0];p[axis]=u;p[other]=v;return tuple(p)
    low,high=limits['min_x'if axis==0 else'min_y'],limits['max_x'if axis==0 else'max_y']
    ends=[a[axis]+i for i in range(math.ceil(low-a[axis]),math.floor(high-a[axis])+1)
          if(b[axis]-a[axis]-i)*arrival>0]
    ends.sort(key=lambda lane:(abs(lane-b[axis]),lane))
    bars=sorted(set(trunk_lanes[other])|clearance[other])
    middles=sorted(set(trunk_lanes[axis])|clearance[axis])
    tails={bar:[lane for lane in ends
                if clear_skeleton([coords(lane,bar),coords(lane,b[other]),b],rows,columns,limits,2)is not None]
           for bar in bars}
    candidates=[]
    # A longer source escape is reserved for an explicitly verified six-cell
    # native prefix. Values seven/eight can still filter the older families.
    maximum_escape=6 if minimum_start_run==6 else 5
    for escape in range(max(1,minimum_start_run or 1),maximum_escape+1):
        pe=coords(a[axis]+escape*forward,a[other])
        for jog in(-4,-3,-2,-1,1,2,3,4):
            corner=coords(pe[axis],a[other]+jog)
            if clear_skeleton([a,pe,corner],rows,columns,limits,1)is None:continue
            for middle in middles:
                if(middle-pe[axis])*forward<=0:continue
                for bar in bars:
                    prefix=[a,pe,corner,coords(middle,corner[other]),coords(middle,bar)]
                    if clear_skeleton(prefix,rows,columns,limits,3)is None:continue
                    accepted=0
                    for lane in tails[bar]:
                        vertices=prefix+[coords(lane,bar),coords(lane,b[other]),b]
                        length=sum(abs(p[0]-q[0])+abs(p[1]-q[1])for p,q in zip(vertices,vertices[1:]))
                        if length>maximum_length:continue
                        canonical=clear_skeleton(vertices,rows,columns,limits,max_turns)
                        if canonical is None:continue
                        candidates.append(canonical);accepted+=1
                        if accepted==16:break
    return candidates


def route_trunks(start, end, *, occupied=(), bounds=None, start_direction=None,
                 end_direction=None, max_nodes=25000, max_turns=6,
                 max_detour_ratio=1.35, grid_pitch=8, candidate_validator=None,minimum_start_run=None):
    """Select whole straight trunk corridors, never incremental squiggles.

    Candidates consist of whole orthogonal trunks and crossbars with bounded
    straight port escapes. Port headings and obstacle ownership are hard constraints.
    Failure is explicit when no clean corridor fits; no spaghetti fallback.
    Survey/clearance and underground-layer planning are separate responsibilities.
    minimum_start_run optionally rejects a short first straight segment before
    expansion; it grants no obstacle, heading, quality or budget exemption.
    """
    failure = {"ok": False, "reason": "no_clean_trunk_corridor", "path": [], "segments": [], "visited": 0}
    try:
        a, b = point(start), point(end)
        if any(d is not None and d not in VECTORS for d in (start_direction, end_direction)):
            raise ValueError("port headings must be cardinal directions")
        if minimum_start_run is not None and(type(minimum_start_run)is not int or not 1<=minimum_start_run<=8
                or type(start_direction)is not int or start_direction not in VECTORS):
            raise ValueError("minimum_start_run requires an integer in 1..8 and a cardinal start heading")
        if (type(grid_pitch) is not int or grid_pitch < 1 or type(max_turns) is not int or max_turns < 0
                or not math.isfinite(max_detour_ratio) or max_detour_ratio < 1):
            raise ValueError("invalid routing quality limits")
        if any(abs((b[i] - a[i]) - round(b[i] - a[i])) > 1e-6 for i in (0, 1)):
            raise ValueError("ports must share the same tile lattice")
        blocked = {point(p) for p in occupied}
        hard_rows,hard_columns=obstacle_index(blocked)
        if a in blocked or b in blocked:
            raise ValueError("route endpoint occupied")
        limits = bounds or {"min_x": min(a[0], b[0])-24, "max_x": max(a[0], b[0])+24,
                           "min_y": min(a[1], b[1])-24, "max_y": max(a[1], b[1])+24}
        if a == b:
            if minimum_start_run is not None:raise ValueError("minimum_start_run requires distinct ports")
            if start_direction is not None and end_direction is not None and start_direction != end_direction:
                raise ValueError("coincident ports have conflicting headings")
            return {"ok": True, "path": [{"x": a[0], "y": a[1]}],
                    "segments": [{"position": {"x": a[0], "y": a[1]}, "direction": end_direction or start_direction or 0}],
                    "visited": 1, "metrics": metrics([a]), "reason": ""}
        av = VECTORS.get(start_direction, (0, 0))
        bv = VECTORS.get(end_direction, (0, 0))
        pa, pb = (a[0]+av[0], a[1]+av[1]), (b[0]-bv[0], b[1]-bv[1])
        skeletons = [[a, pa, (pb[0], pa[1]), pb, b], [a, pa, (pa[0], pb[1]), pb, b]]
        # Use a shared absolute grid so adjacent jobs select the same trunk lanes.
        trunk_lanes = []
        for axis in (0, 1):
            lo, hi = limits["min_x" if axis == 0 else "min_y"], limits["max_x" if axis == 0 else "max_y"]
            fraction = a[axis] - math.floor(a[axis])
            lanes = {pa[axis], pb[axis]}
            lanes.update(n * grid_pitch + fraction for n in range(math.ceil((lo-fraction)/grid_pitch),
                                                                math.floor((hi-fraction)/grid_pitch)+1))
            # Keep the search bounded even for accidental huge envelopes.
            if len(lanes) > 128:
                raise ValueError("trunk corridor search exceeds 128 lanes per axis")
            trunk_lanes.append(sorted(lanes))
            for lane in sorted(lanes):
                if axis == 0:
                    skeletons.append([a, pa, (lane, pa[1]), (lane, pb[1]), pb, b])
                else:
                    skeletons.append([a, pa, (pa[0], lane), (pb[0], lane), pb, b])
        # Two perpendicular reserved trunks handle separate local escapes at
        # source and destination without falling back to a free-form maze path.
        for x in trunk_lanes[0]:
            for y in trunk_lanes[1]:
                skeletons.append([a,pa,(pa[0],y),(x,y),(x,pb[1]),pb,b])
                skeletons.append([a,pa,(x,pa[1]),(x,y),(pb[0],y),pb,b])
        # A one-tile escape can be trapped by a nearby pole even when a clean
        # corridor exists. A fixed four-tile escape remains a straight port
        # connection; the same global turn/detour/obstacle gates still apply.
        pa4=(a[0]+4*av[0],a[1]+4*av[1])
        pb4=(b[0]-4*bv[0],b[1]-4*bv[1])
        for x in sorted(set(trunk_lanes[0]+[pa4[0],pb4[0]])):
            for y in sorted(set(trunk_lanes[1]+[pa4[1],pb4[1]])):
                skeletons.append([a,pa4,(pa4[0],y),(x,y),(x,pb4[1]),pb4,b])
                skeletons.append([a,pa4,(x,pa4[1]),(x,y),(pb4[0],y),pb4,b])
        # Preserve the established corridor family and its ranking first. A
        # shorter new escape may occupy a channel needed by the next connection
        # in an existing bundle, even when both choices are individually valid.
        for axis in (0,1):
            other=1-axis
            lanes=trunk_lanes[axis]
            first=set(sorted(lanes,key=lambda n:abs(n-pa[axis]))[:4])
            last=set(sorted(lanes,key=lambda n:abs(n-pb[axis]))[:4])
            first.update(a[axis]+av[axis]*n for n in (2,3,4))
            last.update(b[axis]-bv[axis]*n for n in (2,3,4))
            if av[axis]==0:first.update(pa[axis]+n for n in (-2,-1,1,2))
            if bv[axis]==0:last.update(pb[axis]+n for n in (-2,-1,1,2))
            midpoint=(pa[other]+pb[other])/2
            bars=sorted(trunk_lanes[other],key=lambda n:(abs(n-pa[other])+abs(n-pb[other]),abs(n-midpoint)))[:8]
            bars=sorted(set(bars+sorted(trunk_lanes[other],key=lambda n:abs(n-pa[other]))[:2]
                                  +sorted(trunk_lanes[other],key=lambda n:abs(n-pb[other]))[:2]))
            for p in first:
                for q in last:
                    for bar in bars:
                        if axis==0:
                            skeletons.append([a,pa,(p,pa[1]),(p,bar),(q,bar),(q,pb[1]),pb,b])
                        else:
                            skeletons.append([a,pa,(pa[0],p),(bar,p),(bar,q),(pb[0],q),pb,b])
        legacy_skeletons=skeletons
        skeletons=[]
        # Only if that family has no accepted whole corridor, add obstacle-edge
        # clearance and independent short endpoint escapes.
        # Every candidate still consists of complete trunks/crossbars, with
        # exactly the same turn, length, hard-obstacle and crossing-layer gates.
        clearance=clearance_lanes(blocked,a,b,limits)
        escape_pairs=((1,1),) if not blocked and candidate_validator is None else ((1,1),(1,2),(1,3),(1,4),(2,1),(3,1),(4,1),(4,4))
        for start_escape,end_escape in escape_pairs:
          pe=(a[0]+start_escape*av[0],a[1]+start_escape*av[1])
          qe=(b[0]-end_escape*bv[0],b[1]-end_escape*bv[1])
          for axis in (0,1):
            other=1-axis
            lanes=trunk_lanes[axis]
            first=set(sorted(lanes,key=lambda n:abs(n-pe[axis]))[:4]+sorted(lanes,key=lambda n:abs(n-qe[axis]))[:2])
            last=set(sorted(lanes,key=lambda n:abs(n-qe[axis]))[:4]+sorted(lanes,key=lambda n:abs(n-pe[axis]))[:2])
            lower,upper=min(a[axis],b[axis]),max(a[axis],b[axis])
            outside=sorted((n for n in clearance[axis] if n<lower),reverse=True)[:4]+sorted(n for n in clearance[axis] if n>upper)[:4]
            first.update(outside);last.update(outside)
            first.update(a[axis]+av[axis]*n for n in (2,3,4))
            last.update(b[axis]-bv[axis]*n for n in (2,3,4))
            if av[axis]==0:first.update(pe[axis]+n for n in (-4,-3,-2,-1,1,2,3,4))
            if bv[axis]==0:last.update(qe[axis]+n for n in (-4,-3,-2,-1,1,2,3,4))
            midpoint=(pe[other]+qe[other])/2
            bars=sorted(trunk_lanes[other],key=lambda n:(abs(n-pe[other])+abs(n-qe[other]),abs(n-midpoint)))[:8]
            # Keep end-adjacent crossbars: the midpoint can lie inside a large
            # production block while its destination has a narrow clear entry.
            bars=sorted(set(bars+sorted(trunk_lanes[other],key=lambda n:abs(n-pe[other]))[:2]
                                  +sorted(trunk_lanes[other],key=lambda n:abs(n-qe[other]))[:2])|clearance[other]|{pe[other],qe[other]})
            for p in first:
                for q in last:
                    for bar in bars:
                        if axis==0:
                            skeletons.append([a,pe,(p,pe[1]),(p,bar),(q,bar),(q,qe[1]),qe,b])
                        else:
                            skeletons.append([a,pe,(pe[0],p),(bar,p),(bar,q),(qe[0],q),qe,b])
        valid = [];seen_skeletons=set()
        visited = 0;rejected_skeletons=0
        direct = abs(a[0]-b[0]) + abs(a[1]-b[1])
        for family in range(4):
            if visited >= max_nodes:break
            if family==0:candidates=legacy_skeletons
            elif family==1:candidates=skeletons
            elif family==2:candidates=(return_jog_skeletons(a,b,limits,start_direction,end_direction,hard_rows,hard_columns,
                                                trunk_lanes,clearance,max_turns)if minimum_start_run is None or minimum_start_run<=4 else [])
            else:candidates=forward_jog_skeletons(a,b,limits,start_direction,end_direction,hard_rows,hard_columns,
                                                trunk_lanes,clearance,max_turns,max_detour_ratio*direct+4,minimum_start_run)
            candidates.sort(key=lambda vs: sum(abs(p[0]-q[0])+abs(p[1]-q[1]) for p,q in zip(vs,vs[1:])))
            for skeleton in candidates:
                estimated = sum(abs(p[0]-q[0])+abs(p[1]-q[1]) for p,q in zip(skeleton,skeleton[1:]))
                if estimated > max_detour_ratio * direct + 4:
                    continue
                canonical=clear_skeleton(skeleton,hard_rows,hard_columns,limits,max_turns)
                if canonical is None:rejected_skeletons+=1;continue
                if minimum_start_run is not None:
                    dx,dy=canonical[1][0]-a[0],canonical[1][1]-a[1]
                    if abs(dx)+abs(dy)<minimum_start_run or((dx>0)-(dx<0),(dy>0)-(dy<0))!=av:
                        rejected_skeletons+=1;continue
                if canonical in seen_skeletons:continue
                seen_skeletons.add(canonical)
                # One shared budget covers every materialized node in ALL
                # families. Impossible polylines are rejected by indexed ranges
                # before expand(), rather than spending the fallback's budget.
                if visited+int(estimated)+1>max_nodes:break
                path = expand(canonical)
                visited += len(path)
                m = metrics(path)
                if m["turns"] <= max_turns:
                    if candidate_validator:
                        segments=[]
                        for index,p in enumerate(path):
                            d=end_direction
                            if index+1<len(path):
                                q=path[index+1];d=next(d for d,v in VECTORS.items() if v==(round(q[0]-p[0]),round(q[1]-p[1])))
                            elif d is None:d=segments[-1]['direction'] if segments else start_direction or 0
                            segments.append({'position':{'x':p[0],'y':p[1]},'direction':d})
                        if not candidate_validator({'ok':True,'segments':segments,'metrics':m}):continue
                    valid.append(((m["turns"], m["length"], tuple(path)), path, m))
            if valid:break
        if not valid:
            return {**failure, "visited": visited,"rejected_skeletons":rejected_skeletons}
        _, path, m = min(valid)
        segments = []
        for index, (x,y) in enumerate(path):
            direction = end_direction
            if index+1 < len(path):
                nx,ny = path[index+1]
                direction = next(d for d,v in VECTORS.items() if v == (round(nx-x), round(ny-y)))
            elif direction is None:
                direction = segments[-1]["direction"] if segments else start_direction or 0
            segments.append({"position": {"x":x,"y":y}, "direction":direction})
        return {"ok":True,"reason":"","path":[{"x":x,"y":y} for x,y in path],
                    "segments":segments,"visited":visited,"metrics":m,"policy":"pcb-trunks-v1",
                    "rejected_skeletons":rejected_skeletons}
    except (ValueError, TypeError, KeyError) as exc:
        return {**failure, "reason":str(exc)}
