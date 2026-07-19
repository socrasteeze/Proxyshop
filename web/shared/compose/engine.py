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
    art_transform: Optional[dict] = None,
    custom_art: bool = False,
    bleed_px: int = 0,
) -> Image.Image:
    """Render a card with the NAS compose engine (no Photoshop).

    custom_art=True skips full-card-scan art extraction (user uploads).
    art_transform: optional {scale, offset_x, offset_y} for pan/zoom in the art window.
    bleed_px: optional print bleed padding around the card.
    """
    game = (game or '').lower()
    # Prefer bleed from card meta when not passed explicitly
    if not bleed_px and isinstance(card, dict):
        try:
            bleed_px = int(card.get('_bleed_px') or 0)
        except (TypeError, ValueError):
            bleed_px = 0
    kwargs = {
        'art_path': art_path,
        'out_path': out_path,
        'art_transform': art_transform,
        'custom_art': custom_art,
        'bleed_px': bleed_px,
    }
    if game == 'mtg':
        return compose_mtg(card, **kwargs)
    if game == 'pokemon':
        return compose_pokemon(card, **kwargs)
    if game == 'riftbound':
        return compose_riftbound(card, **kwargs)
    raise ValueError(f'Compose engine does not support game {game!r}')
