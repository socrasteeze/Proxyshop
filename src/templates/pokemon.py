"""
* Pokémon Template Classes
* Extend BaseTemplate with Pokémon-specific frame/text filling.
* Requires PSDs matching the layer contract in src/enums/layers_pokemon.py.
"""
# Standard Library Imports
from contextlib import suppress
from functools import cached_property
from typing import Callable

# Local Imports
import src.helpers as psd
from src.enums.layers_pokemon import PokemonLayers as PL
from src.games.pokemon.frame_logic import format_attack_cost
from src.templates._core import BaseTemplate
from src.text_layers import TextField


class PokemonBaseTemplate(BaseTemplate):
    """Shared Photoshop execution for Pokémon TCG templates."""
    frame_suffix = 'Pokemon'
    template_suffix = ''

    @property
    def hooks(self) -> list[Callable]:
        """Pokémon cards skip MTG creature/mana hooks."""
        return []

    @property
    def text_layer_methods(self) -> list[Callable]:
        return [self.basic_text_layers, self.rules_text_and_pt_layers]

    @property
    def frame_layer_methods(self) -> list[Callable]:
        return [self.enable_frame_layers]

    @cached_property
    def text_group(self):
        with suppress(Exception):
            return psd.getLayerSet(PL.TEXT)
        return None

    @cached_property
    def frame_group(self):
        with suppress(Exception):
            return psd.getLayerSet(PL.FRAME)
        return None

    def _set_text(self, layer_name: str, contents: str, group=None) -> None:
        """Best-effort set a text layer's contents."""
        if contents is None:
            contents = ''
        parent = group if group is not None else self.text_group
        with suppress(Exception):
            layer = psd.getLayer(layer_name, parent) if parent else psd.getLayer(layer_name)
            if layer:
                self._text.append(TextField(layer=layer, contents=str(contents)))

    def enable_frame_layers(self) -> None:
        """Enable the type-colored frame layer under Frame/."""
        if not self.frame_group:
            return
        with suppress(Exception):
            layer = psd.getLayer(self.layout.frame_type, self.frame_group)
            if layer:
                layer.visible = True

    def basic_text_layers(self) -> None:
        """Fill name and shared collector text."""
        self._set_text(PL.NAME, self.layout.name)
        self._set_text(PL.SET, self.layout.set)
        self._set_text(PL.NUMBER, self.layout.collector_number)
        self._set_text(PL.ARTIST, self.layout.artist)

    def rules_text_and_pt_layers(self) -> None:
        """Filled by concrete subclasses."""
        pass


class NormalPokemonTemplate(PokemonBaseTemplate):
    """SV-era standard Pokémon creature frame."""

    def basic_text_layers(self) -> None:
        super().basic_text_layers()
        self._set_text(PL.HP, self.layout.hp)
        self._set_text(PL.STAGE, self.layout.stage)
        self._set_text(PL.EVOLVES_FROM, self.layout.evolves_from)
        self._set_text(PL.REGULATION, self.layout.regulation_mark)
        self._set_text(PL.WEAKNESS, self.layout.weakness_text)
        self._set_text(PL.RESISTANCE, self.layout.resistance_text)
        self._set_text(PL.RETREAT, self.layout.retreat_text)

    def rules_text_and_pt_layers(self) -> None:
        abilities = self.layout.abilities
        if abilities:
            ab = abilities[0]
            self._set_text(PL.ABILITY_NAME, ab.get('name') or '')
            self._set_text(PL.ABILITY_TEXT, ab.get('text') or '')

        attacks = self.layout.attacks
        slots = [
            (PL.ATTACK1_COST, PL.ATTACK1_NAME, PL.ATTACK1_DAMAGE, PL.ATTACK1_TEXT),
            (PL.ATTACK2_COST, PL.ATTACK2_NAME, PL.ATTACK2_DAMAGE, PL.ATTACK2_TEXT),
        ]
        for i, (cost_l, name_l, dmg_l, text_l) in enumerate(slots):
            if i >= len(attacks):
                break
            atk = attacks[i]
            self._set_text(cost_l, format_attack_cost(atk.get('cost')))
            self._set_text(name_l, atk.get('name') or '')
            self._set_text(dmg_l, atk.get('damage') or '')
            self._set_text(text_l, atk.get('text') or '')


class TrainerPokemonTemplate(PokemonBaseTemplate):
    """Trainer card frame."""

    def enable_frame_layers(self) -> None:
        if not self.frame_group:
            return
        with suppress(Exception):
            layer = psd.getLayer(PL.TRAINER, self.frame_group)
            if layer:
                layer.visible = True

    def basic_text_layers(self) -> None:
        super().basic_text_layers()
        self._set_text(PL.TYPE, self.layout.type_line)

    def rules_text_and_pt_layers(self) -> None:
        self._set_text(PL.RULES, self.layout.oracle_text)


class BasicEnergyPokemonTemplate(PokemonBaseTemplate):
    """Basic Energy card frame — type frame only plus name/collector."""

    def rules_text_and_pt_layers(self) -> None:
        pass
