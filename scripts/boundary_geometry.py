"""Keep administrative boundary exports valid after coordinate rounding."""
import json

from shapely import make_valid
from shapely.geometry import mapping, shape
from shapely.ops import unary_union


def polygonal_geometry(geometry):
    """Repair crossings, preserving polygon components and discarding collapsed lines."""
    geometry = make_valid(geometry)
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        return geometry
    polygons = []

    def collect(part):
        if part.geom_type == "Polygon":
            polygons.append(part)
        elif hasattr(part, "geoms"):
            for child in part.geoms:
                collect(child)

    collect(geometry)
    if not polygons:
        raise ValueError("Administrative boundary has no polygon area")
    return unary_union(polygons)


def export_geometry(geometry, tolerance):
    geometry = polygonal_geometry(geometry).simplify(tolerance, preserve_topology=True)

    def rounded(value):
        if isinstance(value, (list, tuple)):
            return [rounded(item) for item in value]
        return round(value, 3)

    geo = mapping(geometry)
    geo = {"type": geo["type"], "coordinates": rounded(geo["coordinates"])}
    # Rounding can collapse narrow rings or create crossings; validate afterwards.
    return mapping(polygonal_geometry(shape(geo)))
