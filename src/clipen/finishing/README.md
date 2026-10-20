# AI finishing, workflow v2

The user confirms a direction proof and optionally describes the desired feel or
revision. One operation produces a film: it understands the confirmed story, finds
public music, compares candidates, recuts the actual source spans, renders locally,
and publishes the playable result with its decisions and music credit. No music
folder, manual song selection, parameter editing, or separate render command is
required. A later natural-language revision includes the previous decisions.

## AI and evidence

There are at most two bounded Codex attempts per operation:

1. A creative brief uses the confirmed requirements/sections, source observations,
   trusted transcript provenance, and up to eight explicitly consented midpoint
   thumbnails. It chooses a story arc, essential shots, and generic musical tags.
2. The edit decision uses that brief and actual acquired music candidates, including
   source-provided tags/genres and locally measured volume/onsets. It selects music,
   explains the fit and alternatives, and chooses each shot's source trim, order,
   purpose and audio treatment, plus supported finishing elements.

Codex does **not** receive raw music or listen to it through this CLI interface.
Mood/instrument descriptions come from catalog metadata, not an inference from BPM.
Single-frame thumbnails do not establish continuous motion. Those limitations are
included in the prompts; production taste still needs real user footage and review.

Search terms are limited to an enum of generic musical vocabulary. Private project
text is not sent to the catalog. Thumbnails, transcript, analysis and feedback go to
Codex only after consent that states the two-call budget and image scope.

## Music sourcing

`catalog.py` searches the public Openverse audio API and refreshes each candidate's
metadata before downloading. It accepts instrumental tracks with complete CC0 or
CC BY metadata, title, creator, source page and canonical license link. Unknown,
NC, ND and SA entries are excluded by this initial implementation. Metadata is an
index's license assertion, not independent verification of ownership or original
source terms; the UI identifies it as such and links the original source and terms.

Each operation downloads at most three candidates (24 MiB per file) through a fixed
HTTPS host allowlist with public-address checks, bounded redirects, timeouts and
response sizes. Cancellation stops acquisition. Search outages and unusable files
are explicit failures; there is no silent switch to manual music or unlicensed audio.

Local inspection decodes at most 90 seconds per candidate at 8 kHz mono. It records
volume, peak, observed energy onsets and estimated tempo. The selected source window
must lie inside analyzed audio. Flat music may still be selected; it cannot acquire
invented beat markers. Only explicitly marked aligned cuts are checked against
observed beats. Narrative cuts need not all fall on a beat.

## Edit and render contracts

`models.py` validates schema v2. Every edit cites supplied shot evidence and stays
inside its accepted direction shot's source range. It can trim, omit and reorder
shots while protecting the brief's essential shots. The target is 45–60 seconds,
with a bounded 30–75 second allowance. The application compiles source spans using
rational PTS and rebuilds positions and subtitle timing from the resulting order.

Retained speech must remain complete; partial utterances can be excluded or muted
as B-roll. Captions correspond exactly to retained complete cues. Films without
speech do not need invented captions. Cards, emphasis, transitions, SFX and motion
are optional decisions serving the film, not minimum-count completion gates.

The renderer retains the fixed, local FFmpeg/Pillow compositor: source camera audio,
CJK overlays, AI-directed regional music levels with short gain ramps, music ducking,
supported motion and dips, SDR grade, normalization and
peak limiting. It verifies dimensions, codecs, duration, frame rate/count and full
decode before publication. Human review remains pending, not silently passed.

## Persistence and tests

Project schema v17 adds the single `produce` operation while preserving v16 trial
rows and their files. The former parameter draft workflow cannot be invoked as a
new production operation; old trials are not relabeled as v2 results. Requests are
idempotent, source snapshots immutable, failed retries preserve the last successful
film, and restart never replays a paid attempt. Usage is recorded per AI stage;
partial/unknown usage is explicit, never represented as a known zero.

The output includes a playable MP4, a decisions/timeline/source JSON record, the
music catalog snapshot, and a text credit with author, source, license and changes.
New success replaces the UI's result pointer; earlier files remain in managed storage.

Concurrent cards retain their original text and timing and stack vertically in the
upper half of the frame, leaving room for dialogue captions. Temporal overlap does
not discard otherwise valid AI output. Production inputs and raw AI decisions are
saved before local validation. Failed local work can resume using those snapshots
through `POST /api/projects/{project_id}/finishing/{run_id}/commands/resume`, without
provider calls or new music searches. A changed source context blocks this retry;
the original usage totals stay attached to the same run.

Run `PYTHONPATH=tests/python .venv/bin/python -m unittest test_finishing test_finishing_catalog`.
The tests cover actual recuts/subtitle remapping, feedback, two-stage orchestration,
source/license filtering, cancellation, recovery, and real FFmpeg composition.
Default tests use provider/network doubles and spend no AI quota. Live catalog smoke
tests are separate and do not establish the aesthetic quality of real AI edits.

Current scope: a continuous direction video track and its camera audio, up to 1080p
at 24/25/30/60 fps, SDR, supported semantic effects. Full films, HDR mastering,
independent graphics/audio tracks, and final Resolve delivery remain outside this
branch. Source references and semantic decisions remain editable for later delivery.

Sources: [Openverse audio API](https://api.openverse.org/v1/),
[Openverse media metadata](https://docs.openverse.org/meta/media_properties/api.html).
