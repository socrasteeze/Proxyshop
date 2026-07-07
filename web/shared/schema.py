"""
* Shared API Schemas
* Used by both the NAS server and the Windows worker.
* Must never import from `src/` (Windows-only package).
"""
# Standard Library Imports
from enum import Enum
from typing import Optional

# Third Party Imports
from pydantic import BaseModel, Field

"""
* Job Lifecycle
"""


class JobStatus(str, Enum):
    """Lifecycle states for a render job."""
    QUEUED = 'queued'
    CLAIMED = 'claimed'
    RENDERING = 'rendering'
    DONE = 'done'
    FAILED = 'failed'


class JobSubmit(BaseModel):
    """Payload for submitting a render job (art file uploaded separately as multipart)."""
    card_name: str = Field(min_length=1, max_length=200)
    set_code: Optional[str] = Field(default=None, max_length=10)
    collector_number: Optional[str] = Field(default=None, max_length=10)
    template_name: Optional[str] = Field(default=None, max_length=100)
    lang: str = Field(default='en', max_length=5)


class Job(BaseModel):
    """A render job as tracked by the server."""
    id: str
    status: JobStatus = JobStatus.QUEUED
    card_name: str
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    template_name: Optional[str] = None
    lang: str = 'en'
    card_json: Optional[str] = None      # Full Scryfall card object, pre-resolved by server
    art_filename: Optional[str] = None
    result_filename: Optional[str] = None
    error: Optional[str] = None
    log: Optional[str] = None
    attempts: int = 0
    created_at: Optional[str] = None
    claimed_at: Optional[str] = None
    finished_at: Optional[str] = None


class JobResult(BaseModel):
    """Worker's report on a finished job (result PNG uploaded separately)."""
    ok: bool
    error: Optional[str] = None
    log: Optional[str] = None


"""
* Worker Capabilities
"""


class TemplateInfo(BaseModel):
    """One renderable template as reported by the worker."""
    name: str
    class_name: str
    installed: bool = True


class Capabilities(BaseModel):
    """Capabilities handshake sent by the worker on startup.

    Maps card class (layout type, e.g. 'normal', 'saga') to the list of
    templates the worker can render for that class.
    """
    worker_name: str = 'worker'
    proxyshop_version: str = 'unknown'
    templates: dict[str, list[TemplateInfo]] = Field(default_factory=dict)


"""
* Deck Import
"""


class DeckCardLine(BaseModel):
    """One resolved (or unresolved) line of an imported decklist."""
    qty: int = 1
    name: str
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    board: str = 'main'
    resolved: bool = False
    card_id: Optional[str] = None        # Scryfall UUID once resolved
    source: Optional[str] = None         # 'cache' | 'api' | None


class DeckImportRequest(BaseModel):
    """Request to import a decklist by pasted text or by URL."""
    name: Optional[str] = Field(default=None, max_length=200)
    text: Optional[str] = Field(default=None, max_length=100_000)
    url: Optional[str] = Field(default=None, max_length=500)


class DeckImportReport(BaseModel):
    """Result of a deck import: what resolved, what didn't."""
    deck_id: Optional[str] = None
    deck_name: str = ''
    resolved: list[DeckCardLine] = Field(default_factory=list)
    unresolved: list[DeckCardLine] = Field(default_factory=list)
    from_cache: int = 0
    from_api: int = 0
