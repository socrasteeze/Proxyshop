"""
* Pokémon Frame Logic
* Maps pokemontcg.io type / subtype data onto PSD layer names.
"""
# Local Imports
from src.enums.pokemon import POKEMON_TYPE_LAYERS, PokemonType


def primary_type(types: list[str] | None) -> str:
    """Return the primary energy type for frame selection."""
    if not types:
        return PokemonType.Colorless
    return types[0]


def frame_layer_name(types: list[str] | None) -> str:
    """PSD layer name under Frame/ for this card's type."""
    t = primary_type(types)
    return POKEMON_TYPE_LAYERS.get(t, POKEMON_TYPE_LAYERS[PokemonType.Colorless])


def stage_label(subtypes: list[str] | None) -> str:
    """Human-readable stage line (e.g. 'Basic', 'Stage 1')."""
    if not subtypes:
        return 'Basic'
    for s in subtypes:
        if s in ('Basic', 'Stage 1', 'Stage 2', 'VSTAR', 'VMAX', 'Level-Up'):
            return s
    return subtypes[0]


def format_attack_cost(cost: list[str] | None) -> str:
    """Compact attack cost string for text layers (e.g. 'Fire Fire Colorless')."""
    return ' '.join(cost or [])


def format_weakness_resistance(entries: list[dict] | None) -> str:
    """Format weakness/resistance list as 'Water ×2' style text."""
    if not entries:
        return ''
    parts = []
    for e in entries:
        t = e.get('type', '')
        v = e.get('value', '')
        parts.append(f'{t} {v}'.strip())
    return ' · '.join(parts)


def format_retreat(cost: list[str] | None) -> str:
    """Retreat cost as a count or energy list."""
    if not cost:
        return '0'
    return str(len(cost))
