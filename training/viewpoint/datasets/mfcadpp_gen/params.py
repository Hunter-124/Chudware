"""Generator parameters (Utils/parameters.py of CAPP-SRC/mfcad2-dataset)."""

# Stock cube
STOCK_MIN = (10.0, 10.0, 10.0)
STOCK_MAX = (50.0, 50.0, 50.0)

# General feature parameters
MIN_LEN = 2.0
CLEARANCE = 1.0

# Round / chamfer
ROUND_RADIUS_MIN = 0.1
ROUND_RADIUS_MAX = 5.0
CHAMFER_DEPTH_MIN = 0.1
CHAMFER_DEPTH_MAX = 4.0

# main.py: feature sequences are multisets of 3..9 classes drawn from the 24 feature classes
NUM_FEATURES = 24
COMBO_RANGE = (3, 10)

# Class index table (`feat_names`); a face's label is its index here.
FEAT_NAMES = ('chamfer', 'through_hole', 'triangular_passage', 'rectangular_passage',
              '6sides_passage', 'triangular_through_slot', 'rectangular_through_slot',
              'circular_through_slot', 'rectangular_through_step', '2sides_through_step',
              'slanted_through_step', 'Oring', 'blind_hole', 'triangular_pocket',
              'rectangular_pocket', '6sides_pocket', 'circular_end_pocket',
              'rectangular_blind_slot', 'v_circular_end_blind_slot', 'h_circular_end_blind_slot',
              'triangular_blind_step', 'circular_blind_step', 'rectangular_blind_step', 'round',
              'stock')
STOCK = FEAT_NAMES.index('stock')

THROUGH_BLIND_FEATURES = frozenset({
    "triangular_passage", "rectangular_passage", "6sides_passage", "triangular_pocket",
    "rectangular_pocket", "6sides_pocket", "through_hole", "blind_hole", "circular_end_pocket",
    "Oring"})
