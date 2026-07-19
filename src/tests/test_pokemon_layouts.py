"""
* Pokémon layout / frame-logic unit tests (no Photoshop).
"""
# Standard Library Imports
from pathlib import Path

# Local Imports
from src.enums.mtg import LayoutType
from src.enums.pokemon import PokemonType
from src.games.pokemon import frame_logic
from src.games.pokemon.layouts import (
    PokemonEnergyLayout,
    PokemonLayout,
    PokemonTrainerLayout,
    assign_pokemon_layout,
    layout_type_for,
)


def _pokemon_provider(**overrides):
    base = {
        'id': 'sv1-25',
        'name': 'Pikachu',
        'supertype': 'Pokémon',
        'subtypes': ['Basic'],
        'hp': '60',
        'types': ['Lightning'],
        'attacks': [{
            'name': 'Thunder Shock',
            'cost': ['Lightning', 'Colorless'],
            'damage': '30',
            'text': 'Flip a coin.',
        }],
        'weaknesses': [{'type': 'Fighting', 'value': '×2'}],
        'resistances': [],
        'retreatCost': ['Colorless'],
        'number': '25',
        'artist': 'Test Artist',
        'rarity': 'Common',
        'regulationMark': 'G',
        'set': {'id': 'sv1', 'name': 'Scarlet & Violet', 'releaseDate': '2023/03/31'},
        'images': {'large': 'https://img.example/pika.png'},
    }
    base.update(overrides)
    return base


class TestFrameLogic:

    def test_primary_type_and_frame(self):
        assert frame_logic.primary_type(['Fire', 'Water']) == 'Fire'
        assert frame_logic.frame_layer_name(['Water']) == 'Water'
        assert frame_logic.frame_layer_name([]) == PokemonType.Colorless

    def test_stage_and_retreat(self):
        assert frame_logic.stage_label(['Stage 1', 'ex']) == 'Stage 1'
        assert frame_logic.format_retreat(['Colorless', 'Colorless']) == '2'
        assert frame_logic.format_retreat([]) == '0'

    def test_weakness_text(self):
        assert 'Water' in frame_logic.format_weakness_resistance(
            [{'type': 'Water', 'value': '×2'}])


class TestLayoutType:

    def test_supertype_routing(self):
        assert layout_type_for({'supertype': 'Pokémon'}) == LayoutType.Pokemon
        assert layout_type_for({'supertype': 'Trainer'}) == LayoutType.PokemonTrainer
        assert layout_type_for({'supertype': 'Energy'}) == LayoutType.PokemonEnergy


class TestLayouts:

    def test_pokemon_layout_fields(self, tmp_path):
        art = tmp_path / 'Pikachu [sv1] {25}.png'
        art.write_bytes(b'fake')
        file = {'file': art, 'name': 'Pikachu', 'set': 'sv1', 'number': '25',
                'artist': '', 'creator': ''}
        card = {
            'game': 'pokemon', 'id': 'pkm-sv1-25', 'name': 'Pikachu',
            'set': 'sv1', 'collector_number': '25',
            'provider_data': _pokemon_provider()}
        layout = PokemonLayout(card, file)
        assert layout.card_class == LayoutType.Pokemon
        assert layout.name == 'Pikachu'
        assert layout.hp == '60'
        assert layout.frame_type == 'Lightning'
        assert layout.stage == 'Basic'
        assert layout.set == 'SV1'
        assert len(layout.attacks) == 1

    def test_trainer_layout(self, tmp_path):
        art = tmp_path / 'Professor.png'
        art.write_bytes(b'fake')
        file = {'file': art, 'name': 'Professor', 'set': '', 'number': '',
                'artist': '', 'creator': ''}
        card = {
            'provider_data': {
                'name': "Professor's Research",
                'supertype': 'Trainer',
                'subtypes': ['Supporter'],
                'rules': ['Draw 7 cards.'],
                'number': '1',
                'set': {'id': 'sv1', 'name': 'SV'},
            }}
        layout = PokemonTrainerLayout(card, file)
        assert layout.card_class == LayoutType.PokemonTrainer
        assert 'Supporter' in layout.type_line
        assert 'Draw 7' in layout.oracle_text

    def test_energy_layout(self, tmp_path):
        art = tmp_path / 'Fire Energy.png'
        art.write_bytes(b'fake')
        file = {'file': art, 'name': 'Fire Energy', 'set': '', 'number': '',
                'artist': '', 'creator': ''}
        card = {
            'provider_data': {
                'name': 'Basic Fire Energy',
                'supertype': 'Energy',
                'subtypes': ['Basic'],
                'types': ['Fire'],
                'number': '2',
                'set': {'id': 'sv1', 'name': 'SV'},
            }}
        layout = PokemonEnergyLayout(card, file)
        assert layout.card_class == LayoutType.PokemonEnergy
        assert layout.frame_type == 'Fire'

    def test_assign_with_preresolved(self, tmp_path):
        art = tmp_path / 'Pikachu [sv1] {25}.png'
        art.write_bytes(b'fake')
        card = {
            'game': 'pokemon', 'name': 'Pikachu',
            'provider_data': _pokemon_provider()}
        layout = assign_pokemon_layout(art, card_data=card)
        assert not isinstance(layout, str)
        assert layout.name == 'Pikachu'
