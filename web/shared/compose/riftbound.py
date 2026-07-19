"""
* Riftbound compose renderer (Pillow)
"""
# Standard Library Imports
from pathlib import Path
from typing import Optional, Union

# Third Party Imports
from PIL import Image

# Local Imports
from web.shared.compose.frames import load_or_make_riftbound_frame
from web.shared.compose.text import draw_text, paste_cover


def _provider(card: dict) -> dict:
    return card.get('provider_data') or card


def compose_riftbound(
    card: dict,
    art_path: Optional[Union[str, Path]] = None,
    out_path: Optional[Union[str, Path]] = None,
) -> Image.Image:
    """Compose a Riftbound proxy from card metadata + optional art file."""
    p = _provider(card)
    domain = str(p.get('domain') or '')
    card_type = str(p.get('cardType') or p.get('type') or '')

    frame = load_or_make_riftbound_frame(domain, card_type)
    art_box = (40, 120, frame.size[0] - 40, 620)
    if art_path and Path(art_path).is_file():
        try:
            paste_cover(frame, Image.open(art_path), art_box)
        except Exception:
            pass

    name = p.get('name') or card.get('name') or 'Unknown'
    energy = p.get('energyCost')
    power = p.get('powerCost')
    might = p.get('might')

    draw_text(frame, f'{domain}  ·  {card_type}'.strip(' ·'), (48, 40),
              size=20, fill=(230, 230, 230))
    draw_text(frame, name, (48, 70), size=34, bold=True, fill=(255, 255, 255))

    stats = []
    if energy is not None and str(energy) != '':
        stats.append(f'Energy {energy}')
    if power is not None and str(power) != '':
        stats.append(f'Power {power}')
    if might is not None and str(might) != '':
        stats.append(f'Might {might}')
    if stats:
        draw_text(frame, '  ·  '.join(stats), (48, 640), size=26, bold=True,
                  fill=(255, 220, 180))

    # Strip simple HTML from apitcg descriptions
    desc = (p.get('description') or p.get('text') or '').replace('<br>', '\n')
    for tag in ('<em>', '</em>', '<i>', '</i>', '<b>', '</b>', '<strong>', '</strong>'):
        desc = desc.replace(tag, '')
    desc = desc.replace('\r', '')
    y = draw_text(frame, desc, (48, 690), size=20, fill=(220, 220, 220),
                  max_width=frame.size[0] - 96)

    flavor = (p.get('flavorText') or '').replace('<em>', '').replace('</em>', '')
    if flavor:
        draw_text(frame, flavor, (48, max(y + 12, 880)), size=18,
                  fill=(180, 180, 160), max_width=frame.size[0] - 96)

    rarity = p.get('rarity') or ''
    code = p.get('code') or p.get('number') or card.get('collector_number') or ''
    set_info = p.get('set') or {}
    set_name = set_info.get('name') if isinstance(set_info, dict) else card.get('set_name')
    draw_text(
        frame,
        f'{set_name or ""}  {code}  {rarity}'.strip(),
        (48, frame.size[1] - 50), size=16, fill=(160, 160, 160))

    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        frame.convert('RGB').save(out_path, 'PNG')
    return frame
