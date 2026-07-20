"""
* Card search query parser (cross-game)
* Turns a free-text / field-syntax query into SQL WHERE fragments against the
* local cards table. Supports Scryfall-style tokens for MTG (t:, o:, r:, set:)
* and detail keywords for other games (e.g. Pokémon "supporter" / supertype:).
* Must never import from `src/`. Pure — no DB handle, returns SQL + params.
"""
# Standard Library Imports
import re
from typing import Optional

# The single accented value that shows up in a searchable field across the
# supported games is Pokémon's "Pokémon" supertype. Fold just that é→e (a
# single replace() keeps well clear of SQLite's parser-stack limit) so
# "supertype:pokemon" matches. Everything else (trainer, supporter, creature,
# instant, colors, …) is plain ASCII.
def _fold_sql(expr: str) -> str:
    return f"replace({expr},'é','e')"


def fold_value(text: str) -> str:
    """Lowercase a query value and fold the é we also fold on the SQL side."""
    return str(text or '').lower().replace('é', 'e')


# Scryfall Tagger operators. These are crowd-sourced tags that live only in
# Scryfall's search index — they are absent from bulk/API card objects, so the
# local DB can only answer them from a previously downloaded tag cache. Any
# query containing one is treated as a "tag" query (routed live or resolved
# from the offline tag cache) rather than a normal local field search.
TAG_OPERATORS: tuple[str, ...] = (
    'art:', 'atag:', 'arttag:',
    'otag:', 'oracletag:',
    'function:', 'func:',
)


def has_tag_op(query: str) -> bool:
    """True if any whitespace-delimited token uses a Scryfall Tagger operator.

    A leading ``-`` (negation) is ignored so ``-art:elf`` still counts.
    """
    for tok in str(query or '').lower().split():
        if tok.lstrip('-').startswith(TAG_OPERATORS):
            return True
    return False


def normalize_tag(query: str) -> str:
    """Canonical key for a tag query: lowercased, whitespace-collapsed.

    So ``Art:Dragon`` and ``art:dragon`` share one cache entry, and the search
    box matches what the download tool stored.
    """
    return ' '.join(str(query or '').lower().split())

# Per-game field → SQL expression yielding lowercased searchable text.
# Array fields (subtypes/types/colors) are flattened with json_each. Every
# json path is optional: COALESCE keeps rows without the field searchable.
_SEARCH_FIELDS: dict[str, dict[str, str]] = {
    'mtg': {
        'name': "lower(name)",
        'type': "lower(COALESCE(json_extract(json,'$.type_line'),''))",
        'oracle': "lower(COALESCE(json_extract(json,'$.oracle_text'),''))",
        'set': "lower(COALESCE(set_code,''))",
        'rarity': "lower(COALESCE(json_extract(json,'$.rarity'),''))",
        'mana': "lower(COALESCE(json_extract(json,'$.mana_cost'),''))",
        'artist': "lower(COALESCE(json_extract(json,'$.artist'),''))",
        'flavor': "lower(COALESCE(json_extract(json,'$.flavor_text'),''))",
        'color': "lower(COALESCE((SELECT group_concat(value,' ') "
                 "FROM json_each(cards.json,'$.colors')),''))",
        'number': "lower(COALESCE(collector_number,''))",
    },
    'pokemon': {
        'name': "lower(name)",
        'supertype': "lower(COALESCE(json_extract(json,'$.provider_data.supertype'),''))",
        # json_each is a table-valued fn: its source column must be qualified
        # (cards.json) so it correlates to the outer row instead of reading NULL.
        'subtype': "lower(COALESCE((SELECT group_concat(value,' ') "
                   "FROM json_each(cards.json,'$.provider_data.subtypes')),''))",
        'type': "lower(COALESCE((SELECT group_concat(value,' ') "
                "FROM json_each(cards.json,'$.provider_data.types')),''))",
        'rarity': "lower(COALESCE(json_extract(json,'$.provider_data.rarity'),''))",
        'set': "lower(COALESCE(set_code,'') || ' ' || "
               "COALESCE(json_extract(json,'$.set_name'),''))",
        'hp': "lower(COALESCE(json_extract(json,'$.provider_data.hp'),''))",
        'artist': "lower(COALESCE(json_extract(json,'$.provider_data.artist'),''))",
        'number': "lower(COALESCE(collector_number,''))",
    },
    'riftbound': {
        'name': "lower(name)",
        'type': "lower(COALESCE(json_extract(json,'$.provider_data.cardType'),''))",
        'domain': "lower(COALESCE(json_extract(json,'$.provider_data.domain'),''))",
        'oracle': "lower(COALESCE(json_extract(json,'$.provider_data.description'),''))",
        'flavor': "lower(COALESCE(json_extract(json,'$.provider_data.flavorText'),''))",
        'rarity': "lower(COALESCE(json_extract(json,'$.provider_data.rarity'),''))",
        'artist': "lower(COALESCE(json_extract(json,'$.provider_data.artist'),''))",
        'set': "lower(COALESCE(set_code,'') || ' ' || "
               "COALESCE(json_extract(json,'$.set_name'),''))",
        'number': "lower(COALESCE(collector_number,''))",
    },
    'union-arena': {
        'name': "lower(name)",
        'set': "lower(COALESCE(set_code,'') || ' ' || "
               "COALESCE(json_extract(json,'$.set_name'),''))",
        'number': "lower(COALESCE(collector_number,''))",
    },
}

# Cross-game ('All games') field set: unions each game's json paths so a
# keyword or field query works without knowing the game. json_extract returns
# NULL for absent paths, so COALESCE keeps every row searchable.
_SEARCH_FIELDS['all'] = {
    'name': "lower(name)",
    'type': (
        "lower(COALESCE(json_extract(json,'$.type_line'),'') || ' ' "
        "|| COALESCE(json_extract(json,'$.provider_data.supertype'),'') || ' ' "
        "|| COALESCE(json_extract(json,'$.provider_data.cardType'),''))"),
    'oracle': (
        "lower(COALESCE(json_extract(json,'$.oracle_text'),'') || ' ' "
        "|| COALESCE(json_extract(json,'$.provider_data.description'),''))"),
    'rarity': (
        "lower(COALESCE(json_extract(json,'$.rarity'),'') || ' ' "
        "|| COALESCE(json_extract(json,'$.provider_data.rarity'),''))"),
    'artist': (
        "lower(COALESCE(json_extract(json,'$.artist'),'') || ' ' "
        "|| COALESCE(json_extract(json,'$.provider_data.artist'),''))"),
    'set': "lower(COALESCE(set_code,'') || ' ' || "
           "COALESCE(json_extract(json,'$.set_name'),''))",
    'number': "lower(COALESCE(collector_number,''))",
}

# Short aliases → canonical field names (Scryfall-ish where it makes sense).
_FIELD_ALIASES: dict[str, str] = {
    't': 'type', 'type': 'type', 'types': 'type',
    'o': 'oracle', 'oracle': 'oracle', 'text': 'oracle', 'rules': 'oracle',
    'r': 'rarity', 'rarity': 'rarity',
    's': 'set', 'set': 'set', 'e': 'set', 'edition': 'set',
    'c': 'color', 'color': 'color', 'colors': 'color', 'identity': 'color',
    'a': 'artist', 'artist': 'artist',
    'mana': 'mana', 'cost': 'mana', 'cmc': 'mana',
    'flavor': 'flavor', 'ft': 'flavor',
    'st': 'supertype', 'super': 'supertype', 'supertype': 'supertype',
    'sub': 'subtype', 'subtype': 'subtype', 'subtypes': 'subtype',
    'domain': 'domain',
    'hp': 'hp',
    'num': 'number', 'number': 'number', 'cn': 'number', 'collector': 'number',
    'name': 'name', 'n': 'name',
}

# field:value, field:"quoted value", or a bare (optionally "quoted") term.
_TOKEN_RE = re.compile(
    r'(?:(?P<field>[A-Za-z]+):)?(?:"(?P<qval>[^"]*)"|(?P<val>\S+))')


class ParsedQuery:
    """Free-text terms + recognized field filters from one search string."""

    def __init__(self) -> None:
        self.terms: list[str] = []                 # match the whole blob
        self.fields: list[tuple[str, str]] = []    # (canonical_field, value)

    @property
    def is_empty(self) -> bool:
        return not self.terms and not self.fields


def _resolve_game(game: Optional[str]) -> str:
    """Map a game selector to a field-set key. None/''/'all' → cross-game."""
    g = (game or 'all').strip().lower()
    if g in ('', 'all'):
        return 'all'
    return g if g in _SEARCH_FIELDS else 'mtg'


def parse_query(text: str, game: Optional[str]) -> ParsedQuery:
    """Parse a query string into free terms + field filters for a game.

    A `field:value` token is treated as a field filter only when the field is
    known for the game; otherwise the whole token is kept as free text (so a
    stray colon never swallows the query). game=None/''/'all' uses the
    cross-game field set.
    """
    parsed = ParsedQuery()
    known = _SEARCH_FIELDS[_resolve_game(game)]
    for m in _TOKEN_RE.finditer(text or ''):
        field = (m.group('field') or '').lower()
        value = m.group('qval')
        if value is None:
            value = m.group('val') or ''
        value = value.strip()
        if not value and value != '':
            continue
        canon = _FIELD_ALIASES.get(field) if field else None
        if field and canon and canon in known:
            if value:
                parsed.fields.append((canon, value))
        else:
            # Not a recognized field: keep the raw token (with its colon) as text
            raw = m.group(0).strip('"').strip()
            if raw:
                parsed.terms.append(raw.lower())
    return parsed


def _blob_expr(game: str) -> str:
    """SQL expression concatenating every searchable field for the game."""
    fields = _SEARCH_FIELDS[_resolve_game(game)]
    return " || ' ' || ".join(fields.values())


def build_where(parsed: ParsedQuery, game: Optional[str]) -> tuple[str, list]:
    """Return (sql_fragment, params) — an ANDed WHERE body (no leading AND).

    Free terms match the whole per-game blob; field filters match that field's
    expression. game=None/''/'all' searches across every game. Empty parsed
    query yields ('1=1', []).
    """
    game = _resolve_game(game)
    fields = _SEARCH_FIELDS[game]
    clauses: list[str] = []
    params: list = []
    # Free text matches the whole (unfolded) blob; the accent-insensitive FTS
    # name path handles diacritics for names. Wrapping the full blob in accent
    # replace() calls overflows SQLite's parser, so fold only single fields.
    blob = _blob_expr(game)
    for term in parsed.terms:
        clauses.append(f'({blob}) LIKE ?')
        params.append(f'%{term.lower()}%')
    for field, value in parsed.fields:
        expr = fields.get(field)
        if not expr:
            continue
        # Accent-fold only scalar fields. Array fields use a json_each subquery;
        # wrapping that in accent replace() calls overflows SQLite's parser, and
        # their values (subtypes, types, colors) are never accented anyway.
        if 'SELECT' in expr:
            clauses.append(f'({expr}) LIKE ?')
            params.append(f'%{value.lower()}%')
        else:
            clauses.append(f'({_fold_sql(expr)}) LIKE ?')
            params.append(f'%{fold_value(value)}%')
    if not clauses:
        return '1=1', []
    return ' AND '.join(clauses), params


def name_rank_sql(text: str) -> tuple[str, list]:
    """Ranking expression that floats exact / prefix name matches to the top."""
    needle = (text or '').strip()
    return (
        """
        CASE
            WHEN name = ? COLLATE NOCASE THEN 0
            WHEN name LIKE ? COLLATE NOCASE THEN 1
            ELSE 2
        END
        """,
        [needle, f'{needle}%'],
    )
