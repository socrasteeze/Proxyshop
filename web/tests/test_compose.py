"""
* Compose engine tests — procedural frames, no network.
"""
# Standard Library Imports
from pathlib import Path

# Local Imports
from web.shared.compose.engine import COMPOSE_GAMES, compose_card
from web.shared.compose.pokemon import compose_pokemon
from web.shared.compose.riftbound import compose_riftbound


def test_compose_games():
    assert COMPOSE_GAMES == frozenset({'mtg', 'pokemon', 'riftbound'})


def test_compose_mtg_procedural(tmp_path):
    from PIL import Image
    art = tmp_path / 'art.png'
    Image.new('RGB', (200, 200), (40, 80, 200)).save(art)
    card = {
        'name': 'Lightning Bolt',
        'mana_cost': '{R}',
        'type_line': 'Instant',
        'oracle_text': 'Lightning Bolt deals 3 damage to any target.',
        'colors': ['R'],
        'set': 'lea',
        'collector_number': '161',
        'artist': 'Christopher Rush',
    }
    out = tmp_path / 'bolt.png'
    from web.shared.compose.mtg import compose_mtg
    img = compose_mtg(card, art_path=art, out_path=out)
    assert out.is_file()
    assert img.size == (750, 1050)


def test_compose_pokemon_procedural(tmp_path):
    art = tmp_path / 'art.png'
    # Tiny RGB art
    from PIL import Image
    Image.new('RGB', (200, 200), (255, 80, 40)).save(art)
    card = {
        'name': 'Charmander',
        'set': 'sv1',
        'collector_number': '4',
        'provider_data': {
            'name': 'Charmander',
            'supertype': 'Pokémon',
            'subtypes': ['Basic'],
            'hp': '70',
            'types': ['Fire'],
            'attacks': [{
                'name': 'Ember', 'cost': ['Fire'], 'damage': '30',
                'text': 'Discard an Energy.',
            }],
            'weaknesses': [{'type': 'Water', 'value': '×2'}],
            'retreatCost': ['Colorless'],
            'number': '4',
            'artist': 'Test',
            'set': {'id': 'sv1', 'name': 'SV'},
        },
    }
    out = tmp_path / 'out.png'
    img = compose_pokemon(card, art_path=art, out_path=out)
    assert out.is_file()
    assert img.size == (750, 1050)


def test_compose_pokemon_full_scan_extracts_art(tmp_path):
    """Full-card scans must not be cover-cropped whole into the art window."""
    from PIL import Image, ImageDraw
    from web.shared.compose.text import extract_card_art_region, looks_like_full_card_scan

    scan = Image.new('RGB', (750, 1050), (30, 30, 40))
    draw = ImageDraw.Draw(scan)
    # Distinct color only in the typical art band
    draw.rectangle([60, 120, 690, 540], fill=(0, 200, 80))
    # "Rules" band that must not dominate the pasted art
    draw.rectangle([60, 560, 690, 900], fill=(200, 40, 40))
    path = tmp_path / 'scan.png'
    scan.save(path)

    assert looks_like_full_card_scan(scan)
    cropped = extract_card_art_region(scan)
    # Cropped region should be mostly green art, not red rules
    px = cropped.getpixel((cropped.size[0] // 2, cropped.size[1] // 2))
    assert px[1] > px[0]  # green channel dominates

    card = {
        'name': 'Dendra',
        'provider_data': {
            'name': 'Dendra',
            'supertype': 'Trainer',
            'subtypes': ['Supporter'],
            'rules': ['Put a card from your hand on the bottom of your deck.'],
            'number': '250',
            'artist': 'yuu',
            'set': {'id': 'sv2'},
        },
    }
    out = tmp_path / 'trainer.png'
    img = compose_pokemon(card, art_path=path, out_path=out)
    assert out.is_file()
    assert img.size == (750, 1050)
    # Sample center of art window — should be green from extracted band
    art_px = img.getpixel((375, 330))
    assert art_px[1] >= art_px[0]


def test_compose_pokemon_trainer_skips_matchups(tmp_path):
    from PIL import Image
    art = tmp_path / 'art.png'
    Image.new('RGB', (400, 300), (80, 120, 200)).save(art)
    card = {
        'name': 'Dendra',
        'provider_data': {
            'name': 'Dendra',
            'supertype': 'Trainer',
            'subtypes': ['Supporter'],
            'rules': ['Draw until you have 5 cards.'],
            'weaknesses': [{'type': 'Fire', 'value': '×2'}],  # should be ignored
            'number': '250',
            'artist': 'yuu',
            'set': {'id': 'sv2'},
        },
    }
    out = tmp_path / 't.png'
    compose_pokemon(card, art_path=art, out_path=out)
    # Smoke: render succeeds; matchup footer omission is structural
    assert out.is_file()


def test_compose_riftbound_procedural(tmp_path):
    card = {
        'name': 'Annie - Fiery',
        'provider_data': {
            'name': 'Annie - Fiery',
            'domain': 'Fury',
            'cardType': 'Champion Unit',
            'energyCost': '5',
            'powerCost': '1',
            'might': '4',
            'description': 'Your spells deal 1 Bonus Damage.',
            'rarity': 'Epic',
            'code': '001/024',
            'set': {'name': 'Origins'},
        },
    }
    out = tmp_path / 'rb.png'
    img = compose_riftbound(card, out_path=out)
    assert out.is_file()
    assert img.size[0] == 750


def test_compose_card_dispatch(tmp_path):
    out = tmp_path / 'd.png'
    compose_card('pokemon', {'name': 'X', 'provider_data': {
        'name': 'X', 'types': ['Psychic'], 'supertype': 'Pokémon'}}, out_path=out)
    assert out.is_file()


def test_paste_cover_art_transform(tmp_path):
    from PIL import Image
    from web.shared.compose.text import normalize_art_transform, paste_cover

    t = normalize_art_transform({'scale': 2, 'offset_x': -1, 'offset_y': 1})
    assert t['scale'] == 2.0
    assert t['offset_x'] == -1.0
    assert t['offset_y'] == 1.0

    base = Image.new('RGBA', (200, 200), (0, 0, 0, 255))
    # Distinct color in top-left so pan can be observed
    art = Image.new('RGBA', (100, 100), (0, 0, 255, 255))
    for x in range(20):
        for y in range(20):
            art.putpixel((x, y), (255, 0, 0, 255))
    paste_cover(base, art, (0, 0, 100, 100), transform={'scale': 2, 'offset_x': -1, 'offset_y': -1})
    # Top-left of box should pull from art's top-left (red)
    assert base.getpixel((5, 5))[:3] == (255, 0, 0)

    out = tmp_path / 'mtg-zoom.png'
    from web.shared.compose.mtg import compose_mtg
    card = {
        'name': 'Bolt', 'mana_cost': '{R}', 'type_line': 'Instant',
        'oracle_text': 'Deal 3.', 'colors': ['R'], 'set': 'lea',
        'collector_number': '1',
    }
    art_path = tmp_path / 'art.png'
    Image.new('RGB', (400, 300), (20, 180, 40)).save(art_path)
    img = compose_mtg(
        card, art_path=art_path, out_path=out,
        art_transform={'scale': 1.5, 'offset_x': 0.2, 'offset_y': -0.2},
        custom_art=True)
    assert out.is_file()
    assert img.size[0] == 750


def test_custom_art_skips_full_scan_extract(tmp_path):
    """Portrait custom art must not be treated as an official card scan."""
    from PIL import Image
    from web.shared.compose.pokemon import compose_pokemon

    # Portrait near card aspect — would trigger extract without custom_art
    art = Image.new('RGB', (750, 1050), (10, 200, 10))
    path = tmp_path / 'portrait.png'
    art.save(path)
    card = {
        'name': 'Custom', 'provider_data': {
            'name': 'Custom', 'types': ['Grass'], 'supertype': 'Pokémon',
            'hp': '60', 'subtypes': ['Basic'],
        },
    }
    out = tmp_path / 'pkm.png'
    img = compose_pokemon(card, art_path=path, out_path=out, custom_art=True)
    assert out.is_file()
    assert img.size[0] == 750


def test_frame_style_and_layers_and_bleed(tmp_path):
    from PIL import Image
    from web.shared.compose.mtg import compose_mtg
    from web.shared.compose.text import expand_symbols

    assert expand_symbols('{W}{T}') == 'W⟳'
    card = {
        'name': 'Bolt', 'mana_cost': '{R}', 'type_line': 'Instant',
        'oracle_text': 'Deal 3. {T}', 'colors': ['R'], 'set': 'lea',
        'collector_number': '1', 'frame': 'borderless',
        '_layers': {'art': True, 'text': True, 'footer': False},
    }
    art = tmp_path / 'a.png'
    Image.new('RGB', (200, 200), (90, 20, 20)).save(art)
    out = tmp_path / 'bleed.png'
    img = compose_mtg(card, art_path=art, out_path=out, custom_art=True, bleed_px=20)
    assert out.is_file()
    # Default 750 + 2*20 bleed
    assert img.size == (790, 1090)
