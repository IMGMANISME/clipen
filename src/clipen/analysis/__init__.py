"""Local, recoverable media preparation and transcription."""

from .artifacts import ArtifactStore
from .audio import (
    AudioPreparationError,
    AudioPreparationInput,
    FFmpegAudioAdapter,
    PCMChunker,
    PreparedAudio,
    PreparedAudioChunk,
)
from .models import (
    ArtifactKind,
    ArtifactRecord,
    PreparationCounts,
    PreparationItemStatus,
    PreparationRun,
    PreparationState,
    Rational,
    SourceSpan,
)
from .storage import PreparationRepository, PreparationScope, PreparationTarget
from .transcription import (
    LocalModelSnapshot,
    MLXWhisperAdapter,
    TranscriptSegment,
    TranscriptWord,
    TranscriptionError,
    TranscriptionResult,
    hash_model_directory,
)

__all__ = [
    "ArtifactKind",
    "ArtifactRecord",
    "ArtifactStore",
    "AudioPreparationError",
    "AudioPreparationInput",
    "FFmpegAudioAdapter",
    "PCMChunker",
    "PreparedAudio",
    "PreparedAudioChunk",
    "PreparationCounts",
    "PreparationItemStatus",
    "PreparationRepository",
    "PreparationRun",
    "PreparationScope",
    "PreparationState",
    "PreparationTarget",
    "Rational",
    "SourceSpan",
    "LocalModelSnapshot",
    "MLXWhisperAdapter",
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionError",
    "TranscriptionResult",
    "hash_model_directory",
]
