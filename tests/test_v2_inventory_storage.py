import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from factorio_agent.inventory_storage import InventoryStorage, chest_craft_batch


def catalog():
    return SimpleNamespace(items={name: {'stack_size': 50} for name in ('iron-chest', 'wooden-chest', 'steel-chest')},
        recipes={name: {'name': name, 'ingredients': [{'name': ingredient, 'amount': amount}],
                        'products': [{'name': name, 'amount': 1}]}
                 for name, ingredient, amount in (('iron-chest', 'iron-plate', 8),
                    ('wooden-chest', 'wood', 2), ('steel-chest', 'steel-plate', 8))})


def stack(name, count, index=1, quality='normal', kind='item'):
    return {'slot': index, 'name': name, 'count': count, 'quality': quality, 'type': kind}


def snapshot(slots, chests=None, empty=0, crafting=False):
    return {'ok': True, 'world_id': 'w', 'actor_unit_number': 42, 'slots': slots,
            'chests': chests or [], 'empty_slots': empty, 'crafting': crafting,
            'recipes': {name: {'enabled': True, 'handcraftable': True} for name in ('iron-chest', 'wooden-chest', 'steel-chest')}}


class InventoryStorageTests(unittest.TestCase):
    def routine(self, snap):
        routine = InventoryStorage.__new__(InventoryStorage)
        routine.catalog = catalog()
        routine.state = {'world_id': 'w', 'chests': []}
        routine._snapshot = Mock(return_value=snap)
        routine._site = Mock(return_value={'ok': True, 'position': {'x': 70.5, 'y': 20.5}})
        routine._save = Mock()
        return routine

    def test_full_inventory_1594_iron_conservatively_frees_a_full_ingredient_stack(self):
        slots = [stack('iron-plate', 100, i+1) for i in range(15)]+[stack('iron-plate', 94, 16)]
        craft = chest_craft_batch(catalog(), slots, 0)
        self.assertEqual((craft['type'], craft['recipe'], craft['count']), ('craft', 'iron-chest', 13))
        self.assertLessEqual(craft['count'], 50)

    def test_batch_uses_actual_partial_stacks_not_aggregate_modulo(self):
        self.assertEqual(chest_craft_batch(catalog(), [stack('iron-plate', 100), stack('iron-plate', 100, 2)], 0)['count'], 13)
        self.assertEqual(chest_craft_batch(catalog(), [stack('iron-plate', 20), stack('iron-plate', 80, 2)], 0)['count'], 10)
        self.assertIsNone(chest_craft_batch(catalog(), [stack('iron-plate', 4)], 0))
        self.assertIsNone(chest_craft_batch(catalog(), [stack('iron-plate', 100, quality='rare')], 0))

    def test_full_inventory_craft_is_direct_and_pending_queue_is_reobserved(self):
        slots = [stack('iron-plate', 100, i+1) for i in range(15)]+[stack('iron-plate', 94, 16)]
        routine = self.routine(snapshot(slots))
        craft = routine.step({'world_id': 'w'})
        self.assertEqual(craft['count'], 13)
        self.assertEqual(routine.state['pending_craft'], {'recipe': 'iron-chest', 'count': 13})
        routine._snapshot.return_value = snapshot(slots, crafting=True)
        self.assertEqual(routine.step({'world_id': 'w'})['status'], 'waiting')

    def test_crafted_chests_are_reused_for_paid_build_then_bound_by_native_unit(self):
        slots = [stack('iron-chest', 12), stack('iron-ore', 371, 2)]
        routine = self.routine(snapshot(slots))
        obs = {'world_id': 'w', 'entities': []}
        build = routine.step(obs)
        self.assertEqual((build['type'], build['name']), ('build', 'iron-chest'))
        live = {'name': 'iron-chest', 'position': build['position'], 'unit_number': 88}
        storage = dict(live, space={'iron-ore': 1600})
        routine._snapshot.side_effect = [snapshot(slots), snapshot(slots, [storage])]
        deposit = routine.step(dict(obs, entities=[live]))
        self.assertEqual(deposit['type'], 'deposit')
        self.assertEqual((deposit['item'], deposit['count']), ('iron-ore', 371))
        self.assertEqual((deposit['expected_entity_unit'], deposit['expected_entity_world_id']), (88, 'w'))
        self.assertEqual(routine.state['chests'], [live])
        self.assertNotIn('pending_build', routine.state)

    def test_deposit_preserves_construction_reserve_and_respects_chest_capacity(self):
        chest = {'name': 'iron-chest', 'position': {'x': .5, 'y': .5}, 'unit_number': 7,
                 'space': {'iron-plate': 80}}
        routine = self.routine(snapshot([stack('iron-plate', 450)], [chest]))
        action = routine.step({'world_id': 'w'})
        self.assertEqual((action['item'], action['count']), ('iron-plate', 50))
        chest['space']['iron-plate'] = 20
        self.assertEqual(routine.step({'world_id': 'w'})['count'], 20)

    def test_science_ammo_and_non_normal_items_cannot_be_deposited(self):
        slots = [stack('logistic-science-pack', 200, kind='tool'), stack('firearm-magazine', 50, 2, kind='ammo'),
                 stack('wood', 100, 3, quality='rare')]
        chest = {'name': 'iron-chest', 'position': {'x': .5, 'y': .5}, 'unit_number': 7,
                 'space': {'logistic-science-pack': 1000, 'firearm-magazine': 1000, 'wood': 1000}}
        routine = self.routine(snapshot(slots, [chest]))
        self.assertEqual(routine.step({'world_id': 'w'})['status'], 'blocked')

    def test_existing_unregistered_automation_chests_are_not_adopted(self):
        routine = self.routine(snapshot([stack('iron-chest', 1), stack('iron-ore', 100, 2)]))
        action = routine.step({'world_id': 'w', 'entities': [
            {'name': 'iron-chest', 'position': {'x': .5, 'y': .5}, 'unit_number': 100}]})
        self.assertEqual(action['type'], 'build')
        self.assertEqual(routine.state['chests'], [])

    def test_sufficient_headroom_is_noop_and_changed_world_is_rejected_before_query(self):
        routine = self.routine(snapshot([], empty=4))
        self.assertIsNone(routine.step({'world_id': 'w'}))
        routine._snapshot.reset_mock()
        self.assertEqual(routine.step({'world_id': 'other'})['reason'], 'inventory_storage_world_changed')
        routine._snapshot.assert_not_called()

    def test_native_observation_headroom_skips_extra_query_and_locked_recipes_are_not_crafted(self):
        routine = self.routine(snapshot([stack('iron-plate', 200)]))
        self.assertIsNone(routine.step({'world_id': 'w', 'inventory_empty_slots': 4}))
        routine._snapshot.assert_not_called()
        routine._snapshot.return_value['recipes'] = {}
        self.assertEqual(routine.step({'world_id': 'w'})['status'], 'blocked')

    def test_second_snapshot_after_binding_must_still_match_world(self):
        routine = self.routine(snapshot([]))
        chest = {'name': 'iron-chest', 'position': {'x': .5, 'y': .5}, 'unit_number': 8}
        routine.state['pending_build'] = {k: chest[k] for k in ('name', 'position')}
        routine._snapshot.side_effect = [snapshot([]), {'ok': True, 'world_id': 'other'}]
        self.assertEqual(routine.step({'world_id': 'w', 'entities': [chest]})['status'], 'blocked')

    def test_site_candidates_exclude_board_blocks_and_future_bus_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            areas = [[[-50, -50], [50, 50]], [[-100, -100], [0, 100]]]
            (root/'pcb-board.json').write_text(json.dumps({'world_id': 'w', 'blocks': {'production': {'reserved_area': areas[0]}},
                'shared_main_bus': {'reservations': [{'area': areas[1]}]}}))
            routine = InventoryStorage.__new__(InventoryStorage)
            routine.path = root/'inventory-storage.json'
            routine.game = Mock()
            routine.game.query.return_value = {'ok': False, 'reason': 'native_test_only'}
            routine._site({'world_id': 'w', 'position': {'x': 0, 'y': 0}})
            body = routine.game.query.call_args.args[0]
            encoded, _ = json.JSONDecoder().raw_decode(body.split('helpers.json_to_table(', 1)[1])
            candidates = json.loads(encoded)
            self.assertTrue(candidates)
            for p in candidates:
                for area in areas:
                    self.assertFalse(p['x']+.5>area[0][0] and p['x']-.5<area[1][0]
                                     and p['y']+.5>area[0][1] and p['y']-.5<area[1][1])
            self.assertNotIn('--', body)


if __name__ == '__main__':
    unittest.main()
