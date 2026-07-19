"""
* Compose dispatch — game → Pillow renderer
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional, Union

# Third Party Imports
from PIL import Image

# Local Imports
from web.shared.compose.mtg import compose_mtg
from web.shared.compose.pokemon import compose_pokemon
from web.shared.compose.riftbound import compose_riftbound

COMPOSE_GAMES = frozenset({'mtg', 'pokemon', 'riftbound'})


def compose_card(
    game: str,
    card: dict,
    art_path: Optional[Union[str, Path]] = None,
    out_path: Optional[Union[str, Path]] = None,
) -> Image.Image:
    """Render a card with the NAS compose engine (no Photoshop)."""
    game = (game or '').lower()
    if game == 'mtg':
        return compose_mtg(card, art_path=art_path, out_path=out_path)
    if game == 'pokemon':
        return compose_pokemon(card, art_path=art_path, out_path=out_path)
    if game == 'riftbound':
        return compose_riftbound(card, art_path=art_path, out_path=out_path)
    raise ValueError(f'Compose engine does not support game {game!r}')
