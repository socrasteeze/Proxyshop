"""
* Pokémon PSD Layer Contract
*
* Place matching .psd files in plugins/PokemonTCG/templates/ (gitignored like
* other Proxyshop PSDs). Layer names below are looked up by the Python templates.
*
* pokemon-normal.psd  (LayoutType: pokemon)
*   Art Reference          — smart-object / reference layer for artwork
*   Frame/
*     Fire, Water, Grass, Lightning, Psychic, Fighting,
*     Darkness, Metal, Fairy, Dragon, Colorless
*   Text/
*     Name, HP, Stage, Evolves From
*     Ability Name, Ability Text
*     Attack1 Cost, Attack1 Name, Attack1 Damage, Attack1 Text
*     Attack2 Cost, Attack2 Name, Attack2 Damage, Attack2 Text
*     Weakness, Resistance, Retreat
*     Set, Number, Artist, Regulation
*
* pokemon-trainer.psd  (LayoutType: pokemon_trainer)
*   Art Reference
*   Frame/Trainer
*   Text/ Name, Type, Rules, Set, Number, Artist
*
* pokemon-energy.psd  (LayoutType: pokemon_energy)
*   Art Reference
*   Frame/  (same type layers as normal)
*   Text/ Name, Set, Number, Artist
"""
# Third Party Imports
from omnitils.enums import StrConstant


class PokemonLayers(StrConstant):
    """Named PSD layers / groups for Pokémon templates."""
    ART_REFERENCE = 'Art Reference'
    FRAME = 'Frame'
    TEXT = 'Text'
    NAME = 'Name'
    HP = 'HP'
    STAGE = 'Stage'
    EVOLVES_FROM = 'Evolves From'
    ABILITY_NAME = 'Ability Name'
    ABILITY_TEXT = 'Ability Text'
    ATTACK1_COST = 'Attack1 Cost'
    ATTACK1_NAME = 'Attack1 Name'
    ATTACK1_DAMAGE = 'Attack1 Damage'
    ATTACK1_TEXT = 'Attack1 Text'
    ATTACK2_COST = 'Attack2 Cost'
    ATTACK2_NAME = 'Attack2 Name'
    ATTACK2_DAMAGE = 'Attack2 Damage'
    ATTACK2_TEXT = 'Attack2 Text'
    WEAKNESS = 'Weakness'
    RESISTANCE = 'Resistance'
    RETREAT = 'Retreat'
    SET = 'Set'
    NUMBER = 'Number'
    ARTIST = 'Artist'
    REGULATION = 'Regulation'
    TYPE = 'Type'
    RULES = 'Rules'
    TRAINER = 'Trainer'
