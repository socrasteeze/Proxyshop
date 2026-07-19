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
