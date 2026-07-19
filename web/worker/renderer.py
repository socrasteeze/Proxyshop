"""
* Real Renderer (Windows only)
* Drives the Proxyshop render pipeline in-process, mirroring the GUI's
* `start_render` flow (src/gui/app.py). Importing this module pulls in the
* full Proxyshop singleton stack (Photoshop COM, plugins, templates) — it can
* only run on Windows inside the Proxyshop checkout/venv.
"""
# Standard Library Imports
import json
import os
from pathlib import Path
from typing import Optional

# Force headless mode before importing src
os.environ.setdefault('PROXYSHOP_HEADLESS', '1')
os.environ.setdefault('PROXYSHOP_NONINTERACTIVE', '1')

# Proxyshop Imports (Windows-only chain: Photoshop COM, kivy-free headless console)
from src import APP, CFG, CON, ENV, PATH, TEMPLATE_DEFAULTS, TEMPLATE_MAP
from src.enums.mtg import LayoutType
from src.enums.settings import OutputFileType
from src.games.pokemon.layouts import assign_pokemon_layout
from src.layouts import assign_layout

# Local Imports
from web.shared.schema import Capabilities, Job, TemplateInfo

"""
* Capabilities
"""

POKEMON_LAYOUT_TYPES = (
    LayoutType.Pokemon,
    LayoutType.PokemonTrainer,
    LayoutType.PokemonEnergy,
)


def _pokemon_templates_installed() -> bool:
    """True when at least one Pokémon PSD is present on this worker."""
    for category_map in TEMPLATE_MAP.values():
        for layout_type in POKEMON_LAYOUT_TYPES:
            by_name = category_map.get('map', {}).get(layout_type, {})
            for details in by_name.values():
                if details['object'].is_installed:
                    return True
    return False


def get_capabilities(worker_name: str) -> Capabilities:
    """Flatten TEMPLATE_MAP into the capabilities handshake payload."""
    templates: dict[str, list[TemplateInfo]] = {}
    seen: dict[str, set[str]] = {}
    for category_map in TEMPLATE_MAP.values():
        for card_class, by_name in category_map.get('map', {}).items():
            for name, details in by_name.items():
                if name in seen.setdefault(card_class, set()):
                    continue
                seen[card_class].add(name)
                templates.setdefault(card_class, []).append(TemplateInfo(
                    name=name,
                    class_name=details['class_name'],
                    installed=details['object'].is_installed))
    games = ['mtg']
    if _pokemon_templates_installed():
        games.append('pokemon')
    return Capabilities(
        worker_name=worker_name,
        proxyshop_version=str(ENV.VERSION),
        templates=templates,
        games=games)


def _find_template(card_class: str, template_name: Optional[str]):
    """Resolve TemplateDetails by display name for a card class, else default."""
    if template_name:
        for category_map in TEMPLATE_MAP.values():
            details = category_map.get('map', {}).get(card_class, {}).get(template_name)
            if details:
                return details
    return TEMPLATE_DEFAULTS.get(card_class)


"""
* Rendering
"""


def render(job: Job, art_path: Path, out_dir: Path) -> tuple[bool, Optional[Path], str, Optional[str]]:
    """Render one card through the Proxyshop pipeline.

    Args:
        job: The render job (card identity + template choice).
        art_path: Downloaded art file, named with Proxyshop filename tags
            ("Name [SET] {num}.png") so layout assignment can parse it.
        out_dir: Ignored for the real renderer — Proxyshop writes to PATH.OUT;
            the deterministic name (job id) is what matters.

    Returns:
        (ok, result_path, log, error)
    """
    log: list[str] = []
    game = (job.game or 'mtg').lower()

    if game == 'pokemon':
        if not _pokemon_templates_installed():
            return False, None, '\n'.join(log), (
                'Pokémon templates are not installed on this worker — place PSDs in '
                'plugins/PokemonTCG/templates/ (see plugins/PokemonTCG/README.md).')
        card_data = None
        if job.card_json:
            try:
                card_data = json.loads(job.card_json)
            except json.JSONDecodeError:
                card_data = None
        layout = assign_pokemon_layout(art_path, card_data=card_data)
    elif game == 'mtg':
        layout = assign_layout(art_path)
    else:
        return False, None, '\n'.join(log), (
            f'Unsupported game {game!r} — this worker only renders mtg/pokemon.')

    if isinstance(layout, str):
        # assign_* returns an error string on failure
        return False, None, '\n'.join(log), layout
    log.append(f'Game: {game}')
    log.append(f'Layout: {type(layout).__name__} ({layout.card_class})')

    # Resolve the template
    template = _find_template(layout.card_class, job.template_name)
    if not template:
        return False, None, '\n'.join(log), (
            f'No template available for card class {layout.card_class!r}')
    if not template['object'].is_installed:
        return False, None, '\n'.join(log), (
            f"Template '{template['name']}' is not installed on this worker — "
            f"open the Proxyshop GUI updater to download it, or place the PSD "
            f"under the plugin templates folder.")
    log.append(f"Template: {template['name']} ({template['class_name']})")

    # Load template config, then force job-appropriate overrides
    CFG.load(config=template['config'])
    CFG.output_file_name = job.id          # → PATH.OUT / <job_id>.png
    CFG.output_file_type = OutputFileType.PNG
    CFG.overwrite_duplicate = True
    CFG.skip_failed = True                 # never prompt on failure
    CFG.exit_early = False                 # never wait for manual edit input
    CON.reload()

    # Refresh the Photoshop connection and render
    APP.refresh_app()
    layout.template_file = template['object'].path_psd
    template_class = template['object'].get_template_class(template['class_name'])
    render_obj = template_class(layout)
    try:
        ok = bool(render_obj.execute())
    except Exception as e:
        with_suppress_reset(render_obj)
        return False, None, '\n'.join(log), f'Render raised: {e}'

    result = PATH.OUT / f'{job.id}.png'
    if ok and result.exists():
        log.append(f'Saved: {result}')
        return True, result, '\n'.join(log), None
    return False, None, '\n'.join(log), 'Render failed — check Proxyshop logs/error.txt'


def with_suppress_reset(render_obj) -> None:
    """Best-effort document reset after an exception (mirrors GUI error path)."""
    try:
        render_obj.reset()
    except Exception:
        pass
