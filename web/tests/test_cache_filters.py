"""
* Selective cache filter builder tests.
"""
# Third Party Imports
import pytest

# Local Imports
from web.shared import cache_filters as cf


def test_scryfall_query_requires_filter():
    with pytest.raises(ValueError, match='filter'):
        cf.build_scryfall_query({})


def test_scryfall_query_pieces():
    q = cf.build_scryfall_query(cf.normalize_filters('mtg', {
        'set': 'MH3',
        'type': 'Creature',
        'rarity': 'mythic',
        'art': 'showcase,borderless',
        'artist': 'Chris Rahn',
        'year': '2024',
        'tags': 'otag:illustrated',
    }))
    assert 'set:mh3' in q
    assert 't:creature' in q
    assert 'r:mythic' in q
    assert 'is:showcase' in q
    assert 'is:borderless' in q
    assert 'a:"Chris Rahn"' in q
    assert 'year:2024' in q
    assert 'otag:illustrated' in q
    assert 'unique:prints' in q


def test_pokemon_query_requires_filter():
    with pytest.raises(ValueError, match='filter'):
        cf.build_pokemon_query({})


def test_pokemon_query_pieces():
    q = cf.build_pokemon_query(cf.normalize_filters('pokemon', {
        'set': 'sv3',
        'types': 'Fire',
        'subtype': 'V',
        'rarity': 'Rare Holo',
        'regulation': 'g',
        'supertype': 'Pokémon',
    }))
    assert 'set.id:sv3' in q
    assert 'types:Fire' in q
    assert 'subtypes:V' in q
    assert 'rarity:"Rare Holo"' in q
    assert 'regulationMark:G' in q
    assert 'supertype:' in q
    assert 'Pok' in q  # Pokémon / Pokemon depending on encoding
