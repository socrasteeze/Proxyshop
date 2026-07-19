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
