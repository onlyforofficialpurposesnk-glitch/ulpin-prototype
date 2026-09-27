"""
Pure-Python 3D ULPIN ID Generator (no database dependency).
Handles base-34 encoding, ISO/IEC 7064 MOD 37,36 checks, and unit sequencing.
"""

from typing import Dict, List, Tuple, Any
from shapely.geometry import Polygon

B34_ALPHABET = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
B34_CHAR_TO_VAL = {c: i for i, c in enumerate(B34_ALPHABET)}

# ISO/IEC 7064 MOD 37,36 standard uses 36 characters (0-9, A-Z)
MOD3736_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MOD3736_CHAR_TO_VAL = {c: i for i, c in enumerate(MOD3736_ALPHABET)}


def encode_b34(val: int, width: int) -> str:
    """Encode an integer into fixed-width Base-34."""
    if val < 0:
        raise ValueError("Value must be non-negative")
    res = ""
    if val == 0:
        res = B34_ALPHABET[0]
    else:
        while val > 0:
            val, rem = divmod(val, 34)
            res = B34_ALPHABET[rem] + res
    
    if len(res) > width:
        raise ValueError(f"Encoded value {res} exceeds fixed width {width}")
    return res.rjust(width, B34_ALPHABET[0])


def decode_b34(s: str) -> int:
    """Decode a Base-34 string into an integer."""
    val = 0
    for char in s:
        val = val * 34 + B34_CHAR_TO_VAL[char]
    return val


# Note: This check character catches 100% of single-character substitutions and 
# approximately 97.9% of adjacent transpositions, per the known mathematical 
# properties of ISO/IEC 7064 MOD 37,36.
def compute_check_char(s: str) -> str:
    """
    Compute ISO/IEC 7064 MOD 37,36 check character.
    Mapping Document:
    The check value (0 to 35) maps directly to the standard 36-character 
    alphanumeric set (0-9, A-Z), where 0-9 map to values 0-9, and A-Z map 
    to values 10-35. Even though the base ID components use a restricted 
    Base-34 alphabet (no I or O), the check character may be any of the 36 
    standard characters.
    """
    p = 36
    for char in s:
        val = MOD3736_CHAR_TO_VAL[char]
        p = (p + val) % 36
        if p == 0:
            p = 36
        p = (p * 2) % 37
    # For validation p == 1 at the end, and (19 * 2) % 37 == 1
    # Thus (p + check_val) % 36 == 19
    check_val = (19 - p) % 36
    return MOD3736_ALPHABET[check_val]


def validate_mod3736(s: str) -> bool:
    """Validate a string ending in an ISO/IEC 7064 MOD 37,36 check character."""
    p = 36
    for char in s:
        val = MOD3736_CHAR_TO_VAL[char]
        p = (p + val) % 36
        if p == 0:
            p = 36
        p = (p * 2) % 37
    return p == 1


def generate_full_id(parent_ulpin: str, building: int, level: int, seq: int, space_type: str) -> str:
    """Generate the full 22-character 3D ULPIN."""
    if len(parent_ulpin) != 14:
        raise ValueError("Parent ULPIN must be exactly 14 characters")
    if space_type not in ('U', 'P', 'S', 'E', 'A', 'C'):
        raise ValueError("Invalid space type code")
    
    b_str = encode_b34(building, 2)
    l_str = encode_b34(level + 100, 2)  # +100 offsets negative levels
    s_str = encode_b34(seq, 2)
    
    base = f"{parent_ulpin}{b_str}{l_str}{s_str}{space_type}"
    check = compute_check_char(base)
    return f"{base}{check}"


def parse_full_id(full_id: str) -> dict:
    """Parse a full 22-character ID back into its components, validating the check char."""
    if len(full_id) != 22:
        raise ValueError("Full ID must be exactly 22 characters")
    if not validate_mod3736(full_id):
        raise ValueError("Invalid check character")
        
    return {
        "parent_ulpin": full_id[0:14],
        "building": decode_b34(full_id[14:16]),
        "level": decode_b34(full_id[16:18]) - 100,
        "sequence": decode_b34(full_id[18:20]),
        "space_type": full_id[20:21],
        "check_char": full_id[21:22]
    }


def get_reference_point(footprint: Polygon, z_min: float, z_max: float) -> Tuple[float, float, float]:
    """
    Get reference point (centroid) and mid-height Z.
    Falls back to representative point if centroid is outside footprint.
    """
    centroid = footprint.centroid
    if not footprint.contains(centroid):
        pt = footprint.representative_point()
    else:
        pt = centroid
    z = (z_min + z_max) / 2.0
    return (pt.x, pt.y, z)


def morton_encode(x: float, y: float) -> int:
    """Compute 64-bit interleaved Z-order curve Morton code for quantized coordinates."""
    # Scale to mm resolution. Offset by 2e7 m to ensure positive integers.
    ix = int((x + 2e7) * 1000)
    iy = int((y + 2e7) * 1000)
    
    if ix < 0 or iy < 0:
        raise ValueError("Coordinates are out of bounds for Morton encoding")

    res = 0
    # Interleave bits (x on even bits, y on odd bits)
    for i in range(64):
        res |= ((ix & (1 << i)) << i) | ((iy & (1 << i)) << (i + 1))
    return res


def sequence_units(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Given a list of units dicts (must contain 'ref_point': (x, y, z)),
    order them by Morton key (X, Y) and return them with a 'sequence' key.
    """
    def get_morton(unit: Dict[str, Any]) -> int:
        x, y, _ = unit['ref_point']
        return morton_encode(x, y)
        
    sorted_units = sorted(units, key=get_morton)
    for seq, unit in enumerate(sorted_units, start=1):
        unit['sequence'] = seq
    return sorted_units
