# Optional blank-frame PNGs for the compose engine

Drop community / self-made blanks here to replace procedural frames.
Compose also accepts a **frame style** on the card (`frame` / `_frame`):

| Game | Styles |
|---|---|
| MTG | `default`, `borderless`, `fullart` |
| Pokémon | `default`, `fullart` |
| Riftbound | `default`, `wide` |

Blank file layout (optional — missing files use procedural tinted frames):

```
mtg/default.png
mtg/borderless.png
mtg/fullart.png
pokemon/pokemon/<type>.png          e.g. fire.png, grass.png
pokemon/pokemon/<type>_<subtype>.png
pokemon/trainer/default.png
pokemon/energy/<type>.png
pokemon/default.png
riftbound/<domain>.png              e.g. fury.png
riftbound/<domain>_<cardtype>.png
riftbound/default.png
```

Images are resized to 750×1050. Art placement supports pan/zoom
(`art_transform`) and custom uploads skip full-card-scan peeling.

Do not redistribute copyrighted Nintendo/Riot frame packs with this repository —
keep blanks local / gitignored if needed.
