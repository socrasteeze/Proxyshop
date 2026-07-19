"""
* Pokémon Card Data Lookup
* Resolves card identity from art-filename tags via pokemontcg.io (or
* a pre-resolved card object from the web job queue).
"""
# Standard Library Imports
import os
from pathlib import Path
from typing import Optional

# Third Party Imports
import requests

# Local Imports
from src.cards import parse_card_info
from src.console import msg_error
from src.enums.mtg import LayoutType
from src.enums.pokemon import PokemonSupertype
from src.games.pokemon import frame_logic as pkm_frame

POKEMON_API = 'https://api.pokemontcg.io/v2'


def _pokemon_headers() -> dict:
    headers = {'User-Agent': 'Proxyshop'}
    key = os.environ.get('PROXYSHOP_POKEMONTCG_KEY')
    if key:
        headers['X-Api-Key'] = key
    return headers


def fetch_pokemon_card(
    name: str,
    set_code: Optional[str] = None,
    number: Optional[str] = None,
) -> Optional[dict]:
    """Fetch a single Pokémon card object from pokemontcg.io.

    Returns a normalized Proxyshop card dict (game='pokemon', provider_data=...).
    """
    # Prefer exact id lookup when set + number are known (e.g. sv1-1)
    if set_code and number:
        card_id = f'{set_code.lower()}-{number}'
        try:
            res = requests.get(
                f'{POKEMON_API}/cards/{card_id}',
                headers=_pokemon_headers(), timeout=30)
            if res.status_code == 200:
                return _normalize(res.json().get('data') or {})
        except Exception:
            pass

    # Name search with optional filters
    q_parts = [f'name:"*{name}*"']
    if set_code:
        q_parts.append(f'set.id:{set_code.lower()}')
    if number:
        q_parts.append(f'number:{number}')
    try:
        res = requests.get(
            f'{POKEMON_API}/cards',
            params={
                'q': ' '.join(q_parts),
                'pageSize': 5,
                'orderBy': '-set.releaseDate'},
            headers=_pokemon_headers(),
            timeout=30)
        res.raise_for_status()
        data = res.json().get('data') or []
        if data:
            return _normalize(data[0])
    except Exception:
        return None
    return None


def _normalize(c: dict) -> Optional[dict]:
    if not c or not c.get('name'):
        return None
    card_set = c.get('set') or {}
    return {
        'object': 'card',
        'game': 'pokemon',
        'id': f"pkm-{c.get('id', '')}",
        'name': c.get('name', ''),
        'set': card_set.get('id', ''),
        'set_name': card_set.get('name', ''),
        'collector_number': str(c.get('number', '')),
        'lang': 'en',
        'released_at': (card_set.get('releaseDate') or '').replace('/', '-'),
        'images': c.get('images') or {},
        'provider_data': c,
    }


def provider_payload(card: dict) -> dict:
    """Return the raw pokemontcg.io object from a normalized or raw card."""
    if card.get('provider_data'):
        return card['provider_data']
    if card.get('supertype'):
        return card
    return card


def layout_type_for(provider: dict) -> str:
    """Map pokemontcg supertype → LayoutType value."""
    st = (provider.get('supertype') or PokemonSupertype.Pokemon).strip()
    if st == PokemonSupertype.Trainer:
        return LayoutType.PokemonTrainer
    if st == PokemonSupertype.Energy:
        return LayoutType.PokemonEnergy
    return LayoutType.Pokemon


"""
* Layout Classes
"""


class PokemonBaseLayout:
    """Shared properties for all Pokémon TCG layouts."""
    card_class: str = LayoutType.Pokemon

    # MTG flags BaseTemplate may probe — keep False for Pokémon
    is_transform: bool = False
    is_mdfc: bool = False
    is_creature: bool = False
    is_legendary: bool = False
    is_land: bool = False
    is_artifact: bool = False
    is_vehicle: bool = False
    is_hybrid: bool = False
    is_colorless: bool = False
    is_front: bool = True
    is_companion: bool = False
    is_nyx: bool = False
    is_snow: bool = False
    is_miracle: bool = False
    is_token: bool = False
    is_emblem: bool = False
    is_basic_land: bool = False
    is_promo: bool = False

    def __init__(self, card: dict, file: dict):
        self._file = file
        self._card = card
        self._provider = provider_payload(card)
        from src import PATH
        self.template_file: Path = PATH.TEMPLATES / self._default_psd

    def __str__(self):
        return (f"{self.name}"
                f"{f' [{self.set}]' if self.set else ''}"
                f"{f' {{{self.collector_number}}}' if self.collector_number else ''}")

    @property
    def _default_psd(self) -> str:
        return 'pokemon-normal.psd'

    @property
    def file(self) -> dict:
        return self._file

    @property
    def card(self) -> dict:
        return self._card

    @property
    def provider(self) -> dict:
        return self._provider

    @property
    def art_file(self) -> Path:
        return self.file['file']

    @property
    def name(self) -> str:
        return self.provider.get('name') or self.card.get('name') or self.file.get('name', '')

    @property
    def name_raw(self) -> str:
        return self.name

    @property
    def set(self) -> str:
        card_set = self.provider.get('set') or {}
        if isinstance(card_set, dict):
            return str(card_set.get('id') or self.card.get('set') or 'PKM').upper()
        return str(self.card.get('set') or 'PKM').upper()

    @property
    def set_name(self) -> str:
        card_set = self.provider.get('set') or {}
        if isinstance(card_set, dict):
            return str(card_set.get('name') or self.card.get('set_name') or self.set)
        return str(self.card.get('set_name') or self.set)

    @property
    def collector_number(self) -> str:
        return str(self.provider.get('number') or self.card.get('collector_number') or '')

    @property
    def collector_number_raw(self) -> str:
        return self.collector_number

    @property
    def collector_data(self) -> str:
        return self.collector_number

    @property
    def artist(self) -> str:
        return self.provider.get('artist') or self.file.get('artist') or ''

    @property
    def creator(self) -> str:
        return self.file.get('creator') or ''

    @property
    def lang(self) -> str:
        return self.card.get('lang') or 'en'

    @property
    def rarity(self) -> str:
        return self.provider.get('rarity') or ''

    @property
    def flavor_text(self) -> str:
        return self.provider.get('flavorText') or ''

    @property
    def oracle_text(self) -> str:
        return ''

    @property
    def mana_cost(self) -> str:
        return ''

    @property
    def type_line(self) -> str:
        return self.provider.get('supertype') or ''

    @property
    def power(self) -> str:
        return ''

    @property
    def toughness(self) -> str:
        return ''

    @property
    def color_indicator(self) -> str:
        return ''

    @property
    def other_face_power(self) -> str:
        return ''

    @property
    def other_face_toughness(self) -> str:
        return ''

    @property
    def twins(self) -> str:
        return self.frame_type

    @property
    def pinlines(self) -> str:
        return self.frame_type

    @property
    def identity(self) -> str:
        return self.frame_type

    @property
    def background(self) -> str:
        return self.frame_type

    @property
    def watermark(self) -> str:
        return ''

    @property
    def watermark_svg(self) -> Optional[Path]:
        return None

    @property
    def watermark_basic(self) -> Optional[Path]:
        return None

    @property
    def symbol_svg(self) -> Optional[Path]:
        return None

    @property
    def scryfall_scan(self) -> str:
        images = self.provider.get('images') or self.card.get('images') or {}
        return images.get('large') or images.get('small') or ''

    @property
    def transform_icon(self) -> str:
        return ''

    @property
    def types(self) -> list[str]:
        return list(self.provider.get('types') or [])

    @property
    def subtypes(self) -> list[str]:
        return list(self.provider.get('subtypes') or [])

    @property
    def frame_type(self) -> str:
        return pkm_frame.frame_layer_name(self.types)

    @property
    def stage(self) -> str:
        return pkm_frame.stage_label(self.subtypes)

    @property
    def evolves_from(self) -> str:
        return self.provider.get('evolvesFrom') or ''

    @property
    def hp(self) -> str:
        return str(self.provider.get('hp') or '')

    @property
    def abilities(self) -> list[dict]:
        return list(self.provider.get('abilities') or [])

    @property
    def attacks(self) -> list[dict]:
        return list(self.provider.get('attacks') or [])

    @property
    def weaknesses(self) -> list[dict]:
        return list(self.provider.get('weaknesses') or [])

    @property
    def resistances(self) -> list[dict]:
        return list(self.provider.get('resistances') or [])

    @property
    def retreat_cost(self) -> list[str]:
        return list(self.provider.get('retreatCost') or [])

    @property
    def weakness_text(self) -> str:
        return pkm_frame.format_weakness_resistance(self.weaknesses)

    @property
    def resistance_text(self) -> str:
        return pkm_frame.format_weakness_resistance(self.resistances)

    @property
    def retreat_text(self) -> str:
        return pkm_frame.format_retreat(self.retreat_cost)

    @property
    def regulation_mark(self) -> str:
        return self.provider.get('regulationMark') or ''

    @property
    def rules(self) -> list[str]:
        return list(self.provider.get('rules') or [])


class PokemonLayout(PokemonBaseLayout):
    """Standard Pokémon creature card (SV-era MVP)."""
    card_class: str = LayoutType.Pokemon

    @property
    def _default_psd(self) -> str:
        return 'pokemon-normal.psd'


class PokemonTrainerLayout(PokemonBaseLayout):
    """Trainer card layout."""
    card_class: str = LayoutType.PokemonTrainer

    @property
    def _default_psd(self) -> str:
        return 'pokemon-trainer.psd'

    @property
    def type_line(self) -> str:
        parts = [self.provider.get('supertype') or 'Trainer']
        if self.subtypes:
            parts.append(' — '.join(self.subtypes))
        return ' '.join(parts)

    @property
    def oracle_text(self) -> str:
        rules = self.rules
        if rules:
            return '\n'.join(rules)
        return ''


class PokemonEnergyLayout(PokemonBaseLayout):
    """Basic Energy card layout."""
    card_class: str = LayoutType.PokemonEnergy

    @property
    def _default_psd(self) -> str:
        return 'pokemon-energy.psd'


POKEMON_LAYOUT_MAP: dict[str, type[PokemonBaseLayout]] = {
    LayoutType.Pokemon: PokemonLayout,
    LayoutType.PokemonTrainer: PokemonTrainerLayout,
    LayoutType.PokemonEnergy: PokemonEnergyLayout,
}


def assign_pokemon_layout(
    filename: Path,
    card_data: Optional[dict] = None,
) -> 'PokemonBaseLayout | str':
    """Build a Pokémon layout from an art file (+ optional pre-resolved card).

    Args:
        filename: Path to art file (supports Proxyshop filename tags).
        card_data: Optional normalized/raw card object (from job.card_json).

    Returns:
        Layout object, or an error string on failure.
    """
    card_file = parse_card_info(filename)
    name_failed = Path(str(card_file.get('file', filename))).name

    if card_data is None:
        card_data = fetch_pokemon_card(
            name=card_file.get('name', ''),
            set_code=card_file.get('set'),
            number=card_file.get('number'))
    if not card_data:
        return msg_error(name_failed, reason='Pokémon card lookup failed')

    provider = provider_payload(card_data)
    layout_key = layout_type_for(provider)
    layout_cls = POKEMON_LAYOUT_MAP.get(layout_key, PokemonLayout)
    try:
        return layout_cls(card_data, card_file)
    except Exception as e:
        from src import CONSOLE
        CONSOLE.log_exception(e)
        return msg_error(name_failed, reason='Pokémon layout generation failed')
