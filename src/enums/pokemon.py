"""
* Enums: Pokémon TCG Related Data
"""
# Third Party Imports
from omnitils.enums import StrConstant


class PokemonType(StrConstant):
    """Pokémon energy / frame types (pokemontcg.io `types` values)."""
    Colorless = 'Colorless'
    Darkness = 'Darkness'
    Dragon = 'Dragon'
    Fairy = 'Fairy'
    Fighting = 'Fighting'
    Fire = 'Fire'
    Grass = 'Grass'
    Lightning = 'Lightning'
    Metal = 'Metal'
    Psychic = 'Psychic'
    Water = 'Water'


class PokemonSupertype(StrConstant):
    """pokemontcg.io `supertype` values."""
    Pokemon = 'Pokémon'
    Trainer = 'Trainer'
    Energy = 'Energy'


# Maps type name → PSD layer name under Frame/
POKEMON_TYPE_LAYERS: dict[str, str] = {
    PokemonType.Colorless: 'Colorless',
    PokemonType.Darkness: 'Darkness',
    PokemonType.Dragon: 'Dragon',
    PokemonType.Fairy: 'Fairy',
    PokemonType.Fighting: 'Fighting',
    PokemonType.Fire: 'Fire',
    PokemonType.Grass: 'Grass',
    PokemonType.Lightning: 'Lightning',
    PokemonType.Metal: 'Metal',
    PokemonType.Psychic: 'Psychic',
    PokemonType.Water: 'Water',
}
