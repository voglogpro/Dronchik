"""Administrative polygons must remain drawable after export."""
import json
import sys
import unittest
from pathlib import Path

from shapely.geometry import Polygon, shape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from boundary_geometry import export_geometry, polygonal_geometry


class BoundaryGeometryTests(unittest.TestCase):
    def test_repair_preserves_both_sides_of_crossing(self):
        repaired = polygonal_geometry(Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)]))
        self.assertTrue(repaired.is_valid)
        self.assertAlmostEqual(repaired.area, 2)

    def test_rounding_does_not_leave_invalid_ring(self):
        narrow = Polygon([(0, 0), (1, 0), (1, 1), (.5004, 1),
                          (.5004, .2), (.5003, .2), (.5003, 1), (0, 1), (0, 0)])
        exported = shape(export_geometry(narrow, 0))
        self.assertTrue(exported.is_valid)
        self.assertFalse(exported.is_empty)
        self.assertAlmostEqual(exported.area, narrow.area, places=3)

    def test_existing_boundaries_are_valid_and_keep_ids(self):
        for name, count in [("regions", 89), ("districts", 406)]:
            data = json.loads((ROOT / "geodata" / f"{name}.geojson").read_text())
            self.assertEqual(len(data["features"]), count)
            ids = set()
            for feature in data["features"]:
                with self.subTest(boundary=feature["properties"]["id"]):
                    geometry = shape(feature["geometry"])
                    self.assertTrue(geometry.is_valid)
                    self.assertIn(geometry.geom_type, {"Polygon", "MultiPolygon"})
                    self.assertFalse(geometry.is_empty)
                    self.assertEqual(feature["id"], feature["properties"]["id"])
                    self.assertNotIn(feature["id"], ids)
                    ids.add(feature["id"])
            if name == "regions":
                self.assertTrue({"moscow", "moscow-oblast", "vladimir-oblast"} <= ids)


if __name__ == "__main__":
    unittest.main()
