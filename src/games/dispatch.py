"""
* Game-aware layout dispatch
* Routes art files to MTG or Pokémon layout assignment based on ENV.GAME
* (or an explicit game override).
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional, Union

# Local Imports
from src import ENV
from src.games.pokemon.layouts import assign_pokemon_layout
from src.layouts import assign_layout


def assign_layout_for_game(
    filename: Path,
    game: Optional[str] = None,
    card_data: Optional[dict] = None,
) -> Union[object, str]:
    """Assign a layout object for the configured (or overridden) TCG.

    Args:
        filename: Art file path with Proxyshop filename tags.
        game: Optional game slug ('mtg' | 'pokemon'). Defaults to ENV.GAME.
        card_data: Optional pre-resolved card object (Pokémon jobs).
    """
    game = (game or getattr(ENV, 'GAME', None) or 'mtg').lower()
    if game == 'pokemon':
        return assign_pokemon_layout(filename, card_data=card_data)
    return assign_layout(filename)
