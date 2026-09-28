import json
import pytest
import pyproj
from copy import deepcopy
from shapely.geometry import shape, box
from shapely.ops import transform as shapely_transform
from shapely.affinity import translate
from ulpin.validate.core import load_floor_units, validate_building

@pytest.fixture
def base_data():
    units = load_floor_units()
    
    with open('data/mock/parcels.geojson') as f:
        parcels = json.load(f)
    parcel_feat = [f for f in parcels['features'] if f['id'] == 'P1'][0]
    parcel_wgs = shape(parcel_feat['geometry'])
    
    to_wgs = pyproj.Transformer.from_crs('EPSG:32643', 'EPSG:4326', always_xy=True)
    with open('data/processed/units_georef.json') as f:
        doc = json.load(f)
    tf = doc['metadata']['transform']
    bldg_utm = box(tf['translation_x'], tf['translation_y'] - 17.8, tf['translation_x'] + 8.8, tf['translation_y'])
    bldg_wgs = shapely_transform(to_wgs.transform, bldg_utm)
    
    return units, parcel_wgs, bldg_wgs, bldg_utm

def test_duplex_units_pass(base_data):
    """(a) the Duplex units pass"""
    units, parcel_wgs, bldg_wgs, bldg_utm = base_data
    res = validate_building(units, parcel_wgs, bldg_wgs, bldg_utm)
    assert res.passed is True
    assert len(res.reasons) == 0

def test_copied_unit_shifted_rejected(base_data):
    """(b) a copied unit shifted 1 m sideways is rejected as overlapping"""
    units, parcel_wgs, bldg_wgs, bldg_utm = base_data
    shifted_unit = deepcopy(units[0])
    shifted_unit.ulpin_3d = "shifted_id"
    # shift by 1 meter in UTM
    shifted_unit.footprint_utm = translate(shifted_unit.footprint_utm, xoff=1.0)
    
    res = validate_building(units + [shifted_unit], parcel_wgs, bldg_wgs, bldg_utm)
    assert res.passed is False
    assert any("OVERLAP" in r for r in res.reasons)

def test_units_sharing_wall_pass(base_data):
    """(c) two units sharing a wall pass and are recorded as adjacent"""
    units, parcel_wgs, bldg_wgs, bldg_utm = base_data
    res = validate_building(units, parcel_wgs, bldg_wgs, bldg_utm)
    assert res.passed is True
    wall_adj = [adj for adj in res.adjacencies if adj.kind == "wall"]
    assert len(wall_adj) >= 1

def test_unit_outside_parcel_fails(base_data):
    """(d) a unit outside the parcel fails containment"""
    units, parcel_wgs, bldg_wgs, bldg_utm = base_data
    shifted_unit = deepcopy(units[0])
    shifted_unit.ulpin_3d = "outside_id"
    # shift by 100 meters
    shifted_unit.footprint_utm = translate(shifted_unit.footprint_utm, xoff=100.0)
    
    res = validate_building([shifted_unit], parcel_wgs, bldg_wgs, bldg_utm)
    assert res.passed is False
    assert any("CONTAINMENT: unit outside_id" in r for r in res.reasons)

def test_stacked_units_floor_adjacent(base_data):
    """(e) two stacked units with matching slab Z pass and are recorded as floor-adjacent"""
    units, parcel_wgs, bldg_wgs, bldg_utm = base_data
    res = validate_building(units, parcel_wgs, bldg_wgs, bldg_utm)
    assert res.passed is True
    floor_adj = [adj for adj in res.adjacencies if adj.kind == "floor"]
    assert len(floor_adj) >= 1
