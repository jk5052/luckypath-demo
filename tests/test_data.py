"""Source parsing regressions that affect pedestrian connectivity and evidence."""

import csv
from pathlib import Path
import tempfile
import unittest

from luckypath.data import (
    Dataset, _read_osm, _read_trees, load_dataset,
    load_prepared_dataset, save_dataset,
)


OSM = """<osm version="0.6">
<bounds minlon="-73.975" minlat="40.708" maxlon="-73.955" maxlat="40.722"/>
<node id="1" lon="-73.970" lat="40.710"/>
<node id="2" lon="-73.969" lat="40.710"/>
<node id="3" lon="-73.968" lat="40.710"/>
<node id="4" lon="-73.969" lat="40.709"/>
<node id="5" lon="-73.969" lat="40.711"/>
<node id="6" lon="-73.969" lat="40.710"/>
<way id="walk"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="highway" v="footway"/></way>
<way id="bridge"><nd ref="4"/><nd ref="6"/><nd ref="5"/><tag k="highway" v="footway"/><tag k="bridge" v="yes"/><tag k="layer" v="1"/></way>
<way id="private"><nd ref="1"/><nd ref="4"/><tag k="highway" v="service"/><tag k="access" v="private"/></way>
<way id="foot_forbidden"><nd ref="2"/><nd ref="5"/><tag k="highway" v="residential"/><tag k="foot" v="no"/></way>
<way id="motor"><nd ref="1"/><nd ref="5"/><tag k="highway" v="motorway"/></way>
<way id="exception"><nd ref="3"/><nd ref="5"/><tag k="highway" v="service"/><tag k="access" v="private"/><tag k="foot" v="yes"/></way>
<way id="steps"><nd ref="3"/><nd ref="4"/><tag k="highway" v="steps"/></way>
<node id="10" lon="-73.967" lat="40.713"/>
<node id="11" lon="-73.965" lat="40.713"/>
<node id="12" lon="-73.965" lat="40.715"/>
<node id="13" lon="-73.967" lat="40.715"/>
<node id="14" lon="-73.9665" lat="40.7135"/>
<node id="15" lon="-73.9655" lat="40.7135"/>
<node id="16" lon="-73.9655" lat="40.7145"/>
<node id="17" lon="-73.9665" lat="40.7145"/>
<way id="outer_a"><nd ref="10"/><nd ref="11"/><nd ref="12"/></way>
<way id="outer_b"><nd ref="12"/><nd ref="13"/><nd ref="10"/></way>
<way id="inner"><nd ref="14"/><nd ref="15"/><nd ref="16"/><nd ref="17"/><nd ref="14"/></way>
<relation id="building"><member type="way" ref="outer_a" role="outer"/><member type="way" ref="outer_b" role="outer"/><member type="way" ref="inner" role="inner"/><tag k="type" v="multipolygon"/><tag k="building" v="yes"/></relation>
<relation id="missing_building"><member type="way" ref="missing" role="outer"/><tag k="type" v="multipolygon"/><tag k="building" v="yes"/></relation>
</osm>"""


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.osm = self.root / "will.xml"
        self.osm.write_text(OSM)
        self.dataset = Dataset()
        _read_osm(self.osm, self.dataset)

    def tearDown(self):
        self.tmp.cleanup()

    def test_access_filters_and_major_roads_have_different_roles(self):
        ids = {e.osm_tags["osm_way_id"] for e in self.dataset.edges}
        self.assertEqual(ids, {"walk", "bridge", "exception", "steps"})
        self.assertEqual(len(self.dataset.major_roads), 1)
        self.assertTrue(all(e.length > 1 for e in self.dataset.edges))
        x, y = self.dataset.nodes["1"]
        self.assertGreater(x, 500000)
        self.assertGreater(y, 4000000)

    def test_coincident_bridge_node_does_not_join_ground_node(self):
        self.assertEqual(self.dataset.nodes["2"], self.dataset.nodes["6"])
        ground = [e for e in self.dataset.edges if e.osm_tags["osm_way_id"] == "walk"]
        bridge = [e for e in self.dataset.edges if e.osm_tags["osm_way_id"] == "bridge"]
        self.assertEqual({n for e in ground for n in (e.u, e.v)}, {"1", "2", "3"})
        self.assertEqual({n for e in bridge for n in (e.u, e.v)}, {"4", "5", "6"})

    def test_multipolygon_hole_and_missing_geometry_are_preserved(self):
        self.assertEqual(len(self.dataset.buildings), 1)
        self.assertEqual(len(self.dataset.buildings[0].interiors), 1)
        self.assertTrue(self.dataset.buildings[0].is_valid)
        stats = self.dataset.metadata["osm"]["buildings"]
        self.assertEqual(stats["included_building_relations"], 1)
        self.assertEqual(stats["incomplete_building_relations"], 1)

    def test_tree_reader_matches_gh_all_valid_coordinate_rows(self):
        path = self.root / "trees.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["tree_id", "longitude", "latitude", "status"])
            writer.writerows([
                ["1", -73.97, 40.71, "Alive"],
                ["1", -73.97, 40.71, "Alive"],
                ["2", -73.97, 40.71, "Stump"],
                ["3", -74.5, 40.71, "Alive"],
                ["4", "nan", 40.71, "Alive"],
            ])
        _read_trees(path, self.dataset)
        self.assertEqual(len(self.dataset.trees), 4)
        stats = self.dataset.metadata["trees"]
        self.assertEqual(stats["source_rows"], 5)
        self.assertEqual(stats["included_status_counts"], {"alive": 3, "stump": 1})
        self.assertEqual(stats["included_outside_study_bounds"], 1)
        self.assertEqual(stats["invalid_coordinates"], 1)

    def test_prepared_json_roundtrip_keeps_geometry_and_topology(self):
        path = self.root / "prepared" / "dataset.json"
        save_dataset(self.dataset, path)
        loaded = load_prepared_dataset(path)
        self.assertEqual(loaded.nodes, self.dataset.nodes)
        self.assertEqual(loaded.edges, self.dataset.edges)
        self.assertTrue(loaded.buildings[0].equals(self.dataset.buildings[0]))
        self.assertTrue(loaded.major_roads[0].equals(self.dataset.major_roads[0]))
        self.assertNotIn(str(self.root), path.read_text())

    def test_active_will_source_and_duplicate_context(self):
        data = self.root / "data"
        for label in ("east", "west"):
            folder = data / label
            folder.mkdir(parents=True)
            (folder / "map.osm").write_text(OSM)
        folder = data / "places_output_v1_4"
        folder.mkdir()
        (folder / "williamsburg_places_scored_v1_4_full.csv").write_text(
            "place_id,name,longitude,latitude,social_component,access_component\n"
            "one,Example,-73.97,40.71,0.8,0.5\n"
        )
        loaded = load_dataset(data)
        self.assertEqual(loaded.metadata["osm"]["file"], "will.xml")
        self.assertEqual(len(loaded.buildings), 1)
        self.assertEqual(loaded.metadata["building_context_duplicates_removed"], 1)
        self.assertEqual(len(loaded.places), 1)


if __name__ == "__main__":
    unittest.main()
