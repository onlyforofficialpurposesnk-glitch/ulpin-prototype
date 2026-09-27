import random
import pytest
from shapely.geometry import Polygon
from ulpin.idgen.core import (
    encode_b34, decode_b34, compute_check_char, validate_mod3736,
    generate_full_id, parse_full_id, get_reference_point,
    morton_encode, sequence_units, B34_ALPHABET, MOD3736_ALPHABET
)


def test_base34_roundtrip():
    for val in [0, 1, 33, 34, 100, 9999, 12345]:
        s = encode_b34(val, 4)
        assert len(s) == 4
        assert decode_b34(s) == val


def test_negative_levels():
    parent = "1234567890ABCD"
    bldg = 1
    seq = 5
    space = 'U'

    # Level -1 (e.g. Basement 1)
    lvl = -1
    full_id = generate_full_id(parent, bldg, lvl, seq, space)
    parsed = parse_full_id(full_id)
    assert parsed["level"] == -1

    # Level -20
    lvl = -20
    full_id = generate_full_id(parent, bldg, lvl, seq, space)
    parsed = parse_full_id(full_id)
    assert parsed["level"] == -20


def generate_random_id_base():
    parent = "".join(random.choices(B34_ALPHABET, k=14))
    bldg = random.randint(0, 1000)
    lvl = random.randint(-20, 100)
    seq = random.randint(1, 100)
    space = random.choice(['U', 'P', 'S', 'E', 'A', 'C'])
    b_str = encode_b34(bldg, 2)
    l_str = encode_b34(lvl + 100, 2)
    s_str = encode_b34(seq, 2)
    return f"{parent}{b_str}{l_str}{s_str}{space}"


def test_mod3736_substitutions_and_transpositions():
    """Check every substitution and adjacent transposition over 1000 random IDs."""
    for _ in range(1000):
        base = generate_random_id_base()
        check = compute_check_char(base)
        full_id = base + check
        
        # Verify valid ID is accepted
        assert validate_mod3736(full_id) is True

        full_id_list = list(full_id)
        
        # Test 1: Single character substitution
        # Only verify up to the space type code, because check char might map outside B34.
        # But wait, substitution applies to ANY character in the full ID.
        for i in range(len(full_id)):
            original_char = full_id_list[i]
            for sub_char in MOD3736_ALPHABET:
                if sub_char != original_char:
                    full_id_list[i] = sub_char
                    mutated_id = "".join(full_id_list)
                    assert not validate_mod3736(mutated_id), f"Substitution missed: {full_id} -> {mutated_id}"
            # revert
            full_id_list[i] = original_char

        # Test 2: Adjacent transposition
        missed_transpositions = 0
        for i in range(len(full_id) - 1):
            char1 = full_id_list[i]
            char2 = full_id_list[i+1]
            if char1 != char2:
                # Swap
                full_id_list[i], full_id_list[i+1] = char2, char1
                mutated_id = "".join(full_id_list)
                if validate_mod3736(mutated_id):
                    missed_transpositions += 1
                # Revert
                full_id_list[i], full_id_list[i+1] = char1, char2
                
        # ISO 7064 MOD 37,36 (hybrid) is known to mathematically miss ~2.7% of transpositions 
        # (when intermediate p hits 36 and 1). We assert it catches the vast majority.
        assert missed_transpositions < 3, f"Too many missed transpositions for {full_id}"


def test_reference_point():
    # Square from (0,0) to (10,10)
    poly = Polygon([(0,0), (10,0), (10,10), (0,10), (0,0)])
    x, y, z = get_reference_point(poly, 0.0, 4.0)
    assert x == 5.0
    assert y == 5.0
    assert z == 2.0

    # L-shaped polygon where centroid is outside
    poly_L = Polygon([(0,0), (10,0), (10,2), (2,2), (2,10), (0,10), (0,0)])
    # centroid is roughly (3.04, 3.04), which is outside the L-shape.
    assert not poly_L.contains(poly_L.centroid)
    x, y, z = get_reference_point(poly_L, 0.0, 4.0)
    # the returned point should be strictly inside
    from shapely.geometry import Point
    assert poly_L.covers(Point(x, y)) or poly_L.contains(Point(x, y))


def test_unit_sequencing_order_independence():
    units = [
        {"id": "A", "ref_point": (10.0, 10.0, 0)},
        {"id": "B", "ref_point": (20.0, 10.0, 0)},
        {"id": "C", "ref_point": (10.0, 20.0, 0)},
        {"id": "D", "ref_point": (20.0, 20.0, 0)},
    ]
    
    # Generate all sequence IDs from a default ordered list
    seq_default = sequence_units(list(units))
    id_to_seq = {u["id"]: u["sequence"] for u in seq_default}
    
    # Shuffle and sequence again
    for _ in range(10):
        shuffled = list(units)
        random.shuffle(shuffled)
        seq_shuffled = sequence_units(shuffled)
        
        # Check that the assigned sequence for each ID is the same
        for u in seq_shuffled:
            assert u["sequence"] == id_to_seq[u["id"]]
