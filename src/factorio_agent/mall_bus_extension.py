"""Paid, add-only NORTH frontier for surplus construction consumers."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from factorio_agent.core.deterministic_builder import _plan
from .board import entity
from .bus_tap import BusTap, _hash, preserve_bus_ports, reconstruct_bus, signature, extend_bus
from .main_bus import plan_main_bus
from .planning import preflight
from .transport import underground_proof, xy, unbuilt_external_keepouts


def extend_frontier(bus, north_y):
    """Pure compilation; all old entities, layers and descriptors stay fixed."""
    materials, taps, merges, north, south, east = reconstruct_bus(bus)
    if type(north_y) not in (int, float) or north_y % 1 != .5 or not 0 < north - north_y <= 48:
        raise ValueError('one to forty-eight NORTH extension tiles required')
    compiled = plan_main_bus(materials, taps, merges=merges, north_y=north_y, south_y=south, port_x=east)
    if not compiled.get('ok'):
        return compiled
    additions = [entity('transport-belt', row['x'], y, 0) for row in bus['trunks']
                 for y in (north - i for i in range(1, int(north - north_y) + 1))]
    result = deepcopy(compiled)
    result['entities'] = deepcopy(bus['entities']) + additions
    result['underground_pairs'] = deepcopy(bus['underground_pairs'])
    result = preserve_bus_ports(bus, result)
    # Required items must describe the real paid plan, including old tunnels.
    result['required_items'] = dict(Counter(q['name'] for q in result['entities']))
    return {'ok': True, 'bus': result, 'added_entities': additions}


def propose_mall_taps(old, demands, product, obs, builder, board, *, maximum_span=5):
    """Finite frontier rows with an empty first EAST cell, never an implicit merge.

    Trial frontiers always compile from the actual old bus. Rejected trials do
    not invent historical aliases or native identities.
    """
    occupied=builder._occupied_by_plan(obs['entities'])
    future=unbuilt_external_keepouts(builder,obs,board['blocks'])
    areas=[q['reserved_area']for q in board['blocks'].values()if q.get('reserved_area')]
    east=next(q['position']['x']for q in old['ports']if q.get('priority'))
    original_north=old['trunks'][0]['north_y'];rejected=[]
    for depth in(0,12,24,36,48):
        north=original_north-depth
        if depth:
            extension=extend_frontier(old,north)
            if not extension.get('ok'):return extension
            bus=extension['bus']
        else:bus=deepcopy(old)
        tasks=[]
        for item,peak in demands.items():
            name='mall-'+product+'-'+item
            if any(q['name']==name for q in old['ports']):return {'ok':False,'reason':'mall_unclaimed_tap_name_already_exists'}
            stations=[q['splitter']['position']['y']for q in bus['stations']]
            critical=[q['splitter']['position']['y']for q in bus['stations']if q['item']==item and q.get('priority')=='critical']
            upper=min(critical)-3 if critical else min(stations)-3
            chosen=None
            for i in range(max(0,int(upper-north-1))):
                y=north+2+i
                if any(abs(y-other)<3 for other in stations):continue
                # An immediately adjacent foreign belt cannot be crossed with
                # a legal clear underground mouth before the crossing.
                apron={(east,y-1),(east+1,y-1)}
                if apron&(occupied|future)or any(any(a[0][0]<x+.49 and a[1][0]>x-.49 and a[0][1]<py+.49 and a[1][1]>py-.49 for a in areas)for x,py in apron):
                    rejected.append({'row':y,'reason':'tap_source_or_first_outward_cell_occupied'});continue
                parameters=dict(name=name,item=item,splitter_y=y,priority='surplus',demand_per_minute=peak)
                result=extend_bus(bus,parameters,obs,maximum_span=maximum_span)
                if not result.get('ok'):
                    rejected.append({'row':y,'reason':result.get('reason')});continue
                chosen=result;tasks.append(dict(parameters=parameters,added_entities=result['added_entities'],tap_port=result['tap_port']));bus=result['bus'];break
            if chosen is None:break
        else:
            return dict(ok=True,bus=bus,tasks=tasks,north_y=north if depth else None,station_rejections=rejected[-8:])
    return {'ok':False,'reason':'no_bounded_free_surplus_station_and_source_apron','station_rejections':rejected[-8:]}


class MallBusExtension(BusTap):
    def __init__(self, game, builder, catalog, **components):
        super().__init__(game, builder, catalog, **components)
        self.path = Path(game.cfg.runtime_dir) / 'mall-bus-extension.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def prepare(self, obs, north_y):
        try:
            docs = self._documents(); old = docs['factory']['bus']
            if any(q.get('world_id') != obs['world_id'] or q.get('catalog_fingerprint') != self.catalog.fingerprint for q in docs.values()):
                return {'ok': False, 'reason': 'mall_extension_world_or_catalog_changed'}
            if docs['factory'].get('phase') != 'verified' or docs['board'].get('shared_main_bus') != old:
                return {'ok': False, 'reason': 'mall_extension_requires_commissioned_matching_bus'}
            actual = {(q['name'], *xy(q)): q for q in obs['entities']}; retained = []
            for spec in old['entities']:
                live = actual.get((spec['name'], *xy(spec)))
                if not live or not isinstance(live.get('unit_number'), int) or any(live.get(k) != spec.get(k) for k in ('direction', 'belt_to_ground_type', 'splitter_input_priority', 'splitter_output_priority')):
                    return {'ok': False, 'reason': 'mall_extension_existing_bus_changed', 'position': spec['position']}
                retained.append({k: live[k] for k in ('name', 'position', 'unit_number')})
            if not underground_proof(obs, old):
                return {'ok': False, 'reason': 'mall_extension_old_tunnels_unverified'}
            extension = extend_frontier(old, north_y)
            if not extension.get('ok'):
                return extension
            if any((q['name'],*xy(q))in actual for q in extension['added_entities']):
                return {'ok':False,'reason':'mall_extension_new_frontier_already_occupied'}
            check = preflight(self.game, self.builder, obs, extension['added_entities'])
            if not check.get('ok'):
                return check
            return dict(extension, world_id=obs['world_id'], catalog_fingerprint=self.catalog.fingerprint,
                        phase='stock', old_bus=old, new_bus=extension['bus'], old_hash=_hash(old), new_hash=_hash(extension['bus']),
                        retained_units=retained, parameters={'north_y': north_y}, required_stock=dict(Counter(q['name'] for q in extension['added_entities'])),
                        clearance=_plan([], [], clear_obstacles=check['clear_obstacles'], clearance_world_id=obs['world_id']))
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return {'ok': False, 'reason': 'mall_extension_context_invalid', 'detail': str(exc)}

    def bind_added(self,obs):
        """Normal controller execution only: pin each newly paid frontier unit.

        Preparation required these tiles empty. Resuming a partial build may
        first observe its completed prefix; subsequent replacements fail closed.
        """
        s=self.state
        if(s.get('world_id')!=obs.get('world_id')or s.get('catalog_fingerprint')!=self.catalog.fingerprint
                or self._retained_problem(obs)):
            return {'status':'blocked','reason':'mall_extension_bound_bus_changed'}
        actual={(q['name'],*xy(q)):q for q in obs['entities']};bindings=s.setdefault('added_bindings',{});changed=False
        for spec in s['added_entities']:
            key=str(xy(spec));live=actual.get((spec['name'],*xy(spec)));bound=bindings.get(key)
            if bound and(not live or live.get('unit_number')!=bound):
                return {'status':'blocked','reason':'mall_extension_paid_frontier_unit_changed','position':spec['position']}
            if live:
                if type(live.get('unit_number'))is not int or live['unit_number']<1 or live.get('direction')!=0:
                    return {'status':'blocked','reason':'mall_extension_paid_frontier_configuration_changed','position':spec['position']}
                if bound is None:bindings[key]=live['unit_number'];changed=True
        if changed:self._save()
        return None

    def step(self, obs, north_y):
        if self.state and self.state['parameters'] != {'north_y': north_y}:
            if self.state['phase'] != 'verified':
                return {'status': 'blocked', 'reason': 'another_mall_extension_is_unfinished'}
            self.state = {}
        if not self.state:
            ready = self.prepare(obs, north_y)
            if not ready.get('ok'):
                return dict(ready, status='blocked')
            self.state = ready; self._save()
        s = self.state
        if s['world_id'] != obs['world_id'] or s['catalog_fingerprint'] != self.catalog.fingerprint or self._retained_problem(obs):
            return {'status': 'blocked', 'reason': 'mall_extension_bound_bus_changed'}
        if s['phase']in('stock','build'):
            bound=self.bind_added(obs)
            if bound:return bound
        if s['phase'] == 'stock':
            for name, count in s['required_stock'].items():
                if obs.get('inventory', {}).get(name, 0) < count:
                    return self.builder.bootstrap.ensure_item(obs, name, count)
            result = self.builder.ensure_plan(obs, s['clearance'])
            if result.get('status') != 'succeeded': return result
            s['phase'] = 'build'; self._save()
        if s['phase'] == 'build':
            result = self.builder.ensure_plan(obs, _plan(s['added_entities'], []))
            if result.get('status') != 'succeeded': return result
            result = self._publish()
            if result: return result
            s['phase'] = 'verified'; self._save()
        proof = self.proof(obs)
        return {'status': 'succeeded', 'evidence': proof} if proof else {'status': 'blocked', 'reason': 'mall_extension_native_geometry_missing'}

    def proof(self, obs):
        s = self.state
        if not s or s.get('world_id') != obs.get('world_id') or s.get('catalog_fingerprint') != self.catalog.fingerprint or self._retained_problem(obs):
            return None
        actual = {(q['name'], *xy(q)): q for q in obs['entities']}
        if any(not actual.get((q['name'], *xy(q))) or actual[(q['name'], *xy(q))].get('direction') != 0
               or actual[(q['name'],*xy(q))].get('unit_number')!=s.get('added_bindings',{}).get(str(xy(q))) for q in s['added_entities']):
            return None
        return {'tick': obs['tick'], 'added_belts': len(s['added_entities']), 'old_ports_preserved': len(s['old_bus']['ports']),
                'scope': 'paid add-only geometry; no new source capacity or measured throughput'}
