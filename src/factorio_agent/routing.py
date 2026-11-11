"""PCB-style orthogonal trunks with bounded turns, detours and fixed ports."""
from __future__ import annotations

import math


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


def route_trunks(start, end, *, occupied=(), bounds=None, start_direction=None,
                 end_direction=None, max_nodes=25000, max_turns=6,
                 max_detour_ratio=1.35, grid_pitch=8, candidate_validator=None):
    """Select whole straight trunk corridors, never incremental squiggles.

    Candidates consist of whole orthogonal trunks and crossbars with bounded
    straight port escapes. Port headings and obstacle ownership are hard constraints.
    Failure is explicit when no clean corridor fits; no spaghetti fallback.
    Survey/clearance and underground-layer planning are separate responsibilities.
    """
    failure = {"ok": False, "reason": "no_clean_trunk_corridor", "path": [], "segments": [], "visited": 0}
    try:
        a, b = point(start), point(end)
        if any(d is not None and d not in VECTORS for d in (start_direction, end_direction)):
            raise ValueError("port headings must be cardinal directions")
        if (type(grid_pitch) is not int or grid_pitch < 1 or type(max_turns) is not int or max_turns < 0
                or not math.isfinite(max_detour_ratio) or max_detour_ratio < 1):
            raise ValueError("invalid routing quality limits")
        if any(abs((b[i] - a[i]) - round(b[i] - a[i])) > 1e-6 for i in (0, 1)):
            raise ValueError("ports must share the same tile lattice")
        blocked = {point(p) for p in occupied}
        if a in blocked or b in blocked:
            raise ValueError("route endpoint occupied")
        limits = bounds or {"min_x": min(a[0], b[0])-24, "max_x": max(a[0], b[0])+24,
                           "min_y": min(a[1], b[1])-24, "max_y": max(a[1], b[1])+24}
        if a == b:
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
        # Parallel trunks with one crossbar permit independent port escapes.
        # Four nearby grid lanes, short local escapes and eight crossbars.
        # These are complete corridor candidates, never a free-cell maze search.
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
            # Keep end-adjacent crossbars: the midpoint can lie inside a large
            # production block while its destination has a narrow clear entry.
            bars=sorted(set(bars+sorted(trunk_lanes[other],key=lambda n:abs(n-pa[other]))[:2]
                                  +sorted(trunk_lanes[other],key=lambda n:abs(n-pb[other]))[:2]))
            for p in first:
                for q in last:
                    for bar in bars:
                        if axis==0:
                            skeletons.append([a,pa,(p,pa[1]),(p,bar),(q,bar),(q,pb[1]),pb,b])
                        else:
                            skeletons.append([a,pa,(pa[0],p),(bar,p),(bar,q),(pb[0],q),pb,b])
        skeletons.sort(key=lambda vs: sum(abs(p[0]-q[0])+abs(p[1]-q[1]) for p,q in zip(vs,vs[1:])))
        valid = [];seen_paths=set()
        visited = 0
        direct = abs(a[0]-b[0]) + abs(a[1]-b[1])
        for skeleton in skeletons:
            estimated = sum(abs(p[0]-q[0])+abs(p[1]-q[1]) for p,q in zip(skeleton,skeleton[1:]))
            if estimated > max_detour_ratio * direct + 4:
                continue
            path = expand(skeleton)
            identity=tuple(path)
            if identity in seen_paths:continue
            seen_paths.add(identity)
            visited += len(path)
            if visited > max_nodes:
                break
            if len(path) != len(set(path)) or any(p in blocked for p in path):
                continue
            if any(not (limits["min_x"] <= x <= limits["max_x"] and limits["min_y"] <= y <= limits["max_y"]) for x,y in path):
                continue
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
        if not valid:
            return {**failure, "visited": visited}
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
                "segments":segments,"visited":visited,"metrics":m,"policy":"pcb-trunks-v1"}
    except (ValueError, TypeError, KeyError) as exc:
        return {**failure, "reason":str(exc)}
