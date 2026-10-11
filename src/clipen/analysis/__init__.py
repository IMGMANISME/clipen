"""Local, recoverable media preparation and transcription."""

from clipen.domain.media_time import Rational, SourceSpan

from .artifacts import ArtifactStore
from .audio import (
    AudioPreparationError,
    AudioPreparationInput,
    FFmpegAudioAdapter,
    PCMChunker,
    PreparedAudio,
    PreparedAudioChunk,
    inspect_normalized_audio,
    project_samples_to_source,
)
from .models import (
    ArtifactKind,
    ArtifactRecord,
    PreparationChunkStatus,
    PreparationCounts,
    PreparationItemStatus,
    PreparationRun,
    PreparationState,
    TimelineSegment,
)
from .keyframes import AdaptiveKeyframeSelector, KeyframeCandidate, KeyframeSelection
from .storage import (
    PreparationChunk,
    PreparationRepository,
    PreparationScope,
    PreparationTarget,
    PreparationWorkItem,
)
from .service import PreparationCoordinator, PreparationRecipe
from .runtime import ManagedAnalysisRuntime
from .setup import ASRSetupError, ASRSetupManager, ASRSetupState, ASRSetupStatus
from .transcription import (
    Qwen3ASRAdapter,
    LocalModelSnapshot,
    TranscriptSegment,
    TranscriptWord,
    TranscriptionError,
    TranscriptionResult,
    hash_model_directory,
)

__all__ = [
    "ArtifactKind",
    "ArtifactRecord",
    "PreparationChunk",
    "PreparationChunkStatus",
    "ArtifactStore",
    "ASRSetupError",
    "ASRSetupManager",
    "ASRSetupState",
    "ASRSetupStatus",
    "AudioPreparationError",
    "AudioPreparationInput",
    "AdaptiveKeyframeSelector",
    "FFmpegAudioAdapter",
    "PCMChunker",
    "PreparedAudio",
    "PreparedAudioChunk",
    "inspect_normalized_audio",
    "project_samples_to_source",
    "PreparationCounts",
    "PreparationCoordinator",
    "PreparationItemStatus",
    "PreparationRepository",
    "PreparationRecipe",
    "PreparationRun",
    "PreparationScope",
    "PreparationState",
    "PreparationTarget",
    "PreparationWorkItem",
    "Rational",
    "SourceSpan",
    "TimelineSegment",
    "LocalModelSnapshot",
    "Qwen3ASRAdapter",
    "ManagedAnalysisRuntime",
    "KeyframeCandidate",
    "KeyframeSelection",
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionError",
    "TranscriptionResult",
    "hash_model_directory",
]
