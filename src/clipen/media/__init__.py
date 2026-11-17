"""Local media inventory domain and persistence."""

from .discovery import DiscoveredAsset, DiscoveryError, MediaDiscovery
from .models import (
    AssetCandidate,
    AssetKind,
    InventoryCounts,
    InventoryItem,
    InventoryPage,
    InventoryRoot,
    InventoryRun,
    InventoryState,
    ItemStatus,
    MediaStream,
    ProbeStatus,
)
from .preview import MediaPreview, MediaPreviewRepository
from .probe import (
    FFprobeAdapter,
    MediaProbeError,
    MediaProbeResult,
    bounded_file_fingerprint,
)
from .service import InventoryCoordinator
from .storage import InventoryRepository

__all__ = [
    "AssetCandidate",
    "AssetKind",
    "DiscoveredAsset",
    "DiscoveryError",
    "FFprobeAdapter",
    "InventoryCounts",
    "InventoryCoordinator",
    "MediaPreview",
    "MediaPreviewRepository",
    "InventoryItem",
    "InventoryPage",
    "InventoryRepository",
    "InventoryRoot",
    "InventoryRun",
    "InventoryState",
    "ItemStatus",
    "MediaStream",
    "MediaDiscovery",
    "MediaProbeError",
    "MediaProbeResult",
    "ProbeStatus",
    "bounded_file_fingerprint",
]
