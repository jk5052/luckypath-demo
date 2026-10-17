"""Keep routing semantics while sharing geometry across concurrent requests."""
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import test_routing as reference_tests
import test_budget_routing as budget_tests
from luckypath.routing import PreparedNetwork, plan_route, plan_budget_route


def prepared_route(dataset, origin_lonlat, destination_lonlat, edge_costs,
                   via_lonlat=None, max_snap_m=150):
    trip = PreparedNetwork(dataset).prepare(origin_lonlat, destination_lonlat, via_lonlat, max_snap_m)
    return trip.plan_route(edge_costs)


def prepared_budget(dataset, origin_lonlat, destination_lonlat, edge_costs,
                    max_network_length_m, via_lonlat=None, max_snap_m=150):
    trip = PreparedNetwork(dataset).prepare(origin_lonlat, destination_lonlat, via_lonlat, max_snap_m)
    return trip.plan_budget_route(edge_costs, max_network_length_m)


class PreparedRoutingContract(reference_tests.RoutingTests):
    def setUp(self):
        self.patch = patch.object(reference_tests, 'plan_route', prepared_route)
        self.patch.start()
        self.addCleanup(self.patch.stop)


class PreparedBudgetContract(budget_tests.BudgetRoutingTests):
    def setUp(self):
        for name, replacement in [('plan_route', prepared_route), ('plan_budget_route', prepared_budget)]:
            patcher = patch.object(budget_tests, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)


class SharedGeometryTests(unittest.TestCase):
    def test_ties_budget_search_and_changed_weights_match_reference(self):
        dataset, costs = budget_tests.three_alternatives()
        network = PreparedNetwork(dataset)
        trip = network.prepare(budget_tests.lonlat(0), budget_tests.lonlat(100))
        for values in (costs, {key: 0 for key in costs}, {'short': 1, 'middle': 2, 'long': 3}):
            self.assertEqual(trip.plan_route(values), plan_route(dataset, budget_tests.lonlat(0), budget_tests.lonlat(100), values))
            for cap in (100, 160, 300):
                self.assertEqual(trip.plan_budget_route(values, cap),
                                 plan_budget_route(dataset, budget_tests.lonlat(0), budget_tests.lonlat(100), values, cap))

    def test_concurrent_requests_and_result_mutations_are_isolated(self):
        dataset = budget_tests.single_road()
        network = PreparedNetwork(dataset)
        cases = [(10, 190, None, 5), (20, 80, 180, 90), (180, 20, None, 0)] * 6
        def run(case, prepared):
            a, b, via, cost = case
            args = dict(origin_lonlat=budget_tests.lonlat(a), destination_lonlat=budget_tests.lonlat(b),
                        via_lonlat=None if via is None else budget_tests.lonlat(via))
            if prepared:
                return network.prepare(**args).plan_route({'ab': cost})
            return plan_route(dataset, edge_costs={'ab': cost}, **args)
        expected = [run(case, False) for case in cases]
        with ThreadPoolExecutor(max_workers=4) as pool:
            actual = list(pool.map(lambda case: run(case, True), cases))
        self.assertEqual(actual, expected)
        actual[0]['route']['geometry']['coordinates'][0][0] = 0
        self.assertEqual(run(cases[0], True), expected[0])

    def test_prepared_snapshot_does_not_follow_dataset_mutation(self):
        dataset = budget_tests.single_road()
        network = PreparedNetwork(dataset)
        args = (budget_tests.lonlat(10), budget_tests.lonlat(190))
        expected = network.prepare(*args).plan_route({'ab': 1})
        dataset.nodes.clear()
        dataset.edges.clear()
        self.assertEqual(network.prepare(*args).plan_route({'ab': 1}), expected)


DATA = Path(os.environ.get('LUCKYPATH_TEST_DATA', Path(__file__).resolve().parents[1] / 'data'))


@unittest.skipUnless((DATA / 'dataset.json').is_file(), 'Requires prepared demo data')
class CityEquivalenceTests(unittest.TestCase):
    def test_full_recommendations_equal_original_with_budgets_via_and_new_weights(self):
        from luckypath.engine import Engine
        from luckypath.explore import ExploreService

        # Exercise the old implementation in the same process and projection
        # environment. No rounding/tolerance can hide a changed route or score.
        class ReferenceNetwork:
            def __init__(self, dataset): self.dataset = dataset
            def prepare(self, **args):
                dataset = self.dataset
                class Trip:
                    def plan_route(self, costs):
                        return plan_route(dataset, edge_costs=costs, **args)
                    def plan_budget_route(self, costs, max_network_length_m):
                        return plan_budget_route(dataset, edge_costs=costs,
                                                 max_network_length_m=max_network_length_m, **args)
                return Trip()

        service = ExploreService(Engine(DATA), DATA)
        fast = service.engine.network
        old = ReferenceNetwork(service.engine.dataset)
        base = json.loads((DATA / 'green-request.json').read_text())
        budget = {'max_minutes': 30, 'walking_speed_kmh': 4.5, 'stop_minutes': 0, 'buffer_minutes': 3}
        requests = [base, {**base, 'time_budget': budget},
                    {**base, 'time_budget': {**budget, 'max_minutes': 5}},
                    {**base, 'via': [-73.961, 40.715], 'time_budget': budget},
                    {**base, 'origin': base['destination'], 'destination': base['origin'],
                     'profile_scores': {'open': 10, 'green': 1, 'still': 9, 'social': 7, 'access': 2}},
                    {**base, 'destination': base['origin'], 'time_budget': budget}]
        for index, request in enumerate(requests):
            with self.subTest(case=index):
                service.engine.network = old
                expected = service.recommend(request)
                service.engine.network = fast
                self.assertEqual(service.recommend(request), expected)


if __name__ == '__main__':
    unittest.main()
