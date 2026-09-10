"""Local, recoverable media preparation and transcription."""

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
    Rational,
    SourceSpan,
    TimelineSegment,
)
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
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionError",
    "TranscriptionResult",
    "hash_model_directory",
]
