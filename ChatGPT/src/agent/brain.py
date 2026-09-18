"""Single-frame compatibility API. Continuous games use state.Session."""
from .engine import decide
from .protocol import empty_response
from .policies.construction import _tower_sites
from .policies.defense import _tower_pairs
from .policies.combat import _attack_targets
