"""
# Pokémon TCG Proxyshop Plugin

SV-era MVP templates for Pokémon, Trainer, and Basic Energy cards.

## Setup

1. Create Photoshop documents matching the layer contract in
   `src/enums/layers_pokemon.py`.
2. Save them as:
   - `plugins/PokemonTCG/templates/pokemon-normal.psd`
   - `plugins/PokemonTCG/templates/pokemon-trainer.psd`
   - `plugins/PokemonTCG/templates/pokemon-energy.psd`
3. Restart Proxyshop (GUI or worker). Capabilities will advertise `pokemon`
   once at least one PSD is installed (`is_installed`).

## Render inputs

Art filenames use the same Proxyshop tags as MTG:

```
Pikachu [sv1] {25}.png
```

Card data comes from [pokemontcg.io](https://pokemontcg.io) (optional
`PROXYSHOP_POKEMONTCG_KEY` for higher rate limits).

## Legal

For personal proxy use only. Do not redistribute Nintendo/Pokémon Company
frame artwork as an official template pack.
"""
