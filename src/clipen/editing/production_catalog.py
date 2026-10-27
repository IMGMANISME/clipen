"""Discoverable contracts for the agent's production tools, shared by MCP and dispatch."""
import json
from clipen.music_vocabulary import MUSIC_TAGS
from jsonschema import Draft202012Validator
from clipen.projects import ProjectError


def schema(name, description, properties=None, required=None):
    properties = properties or {}
    return {'name': name, 'description': description, 'inputSchema': {
        'type':'object', 'properties':properties, 'required':list(properties if required is None else required),
        'additionalProperties':False}}


ID={'type':'string','minLength':1,'maxLength':160}
TEXT={'type':'string','minLength':1,'maxLength':500}
FRAME={'type':'integer','minimum':0,'maximum':108000}
LENGTH={'type':'integer','minimum':1,'maximum':108000}
UNIT={'type':'number','minimum':0,'maximum':1}
GAIN={'type':'number','minimum':-60,'maximum':12}
SPAN={'start_frame':FRAME,'duration_frames':LENGTH}
SUMMARY={'summary':TEXT}
RECT={**SPAN,'x':UNIT,'y':UNIT,'width':{'type':'number','exclusiveMinimum':0,'maximum':1},
      'height':{'type':'number','exclusiveMinimum':0,'maximum':1},'block_size':{'type':'integer','minimum':4,'maximum':96}}
EXTRA_TOOLS=(
 schema('list_editing_tools','Discover executable tools, limits and explicitly unsupported capabilities.'),
 schema('get_program_reference','Read the human-selected programme style reference frozen when this run started. Its example speech is not a script to copy; style acceptance does not verify ASR or audio.'),
 schema('search_source_dialogue','Search all current source transcripts before choosing clips. Empty query browses complete utterances. Includes adjacent speech for context; inspect actual footage before inferring events. By default excludes approved reference source files to test a new event.',
        {'query':{'type':'string','maxLength':120},'version_id':ID,'include_drafts':{'type':'boolean'},'exclude_reference_sources':{'type':'boolean'},
         'offset':{'type':'integer','minimum':0,'maximum':100000},'limit':{'type':'integer','minimum':1,'maximum':30}},[]),
 schema('list_source_media','Page through all currently inventoried source streams, including material absent from earlier candidate selection. Never copies reference footage.',
        {'offset':{'type':'integer','minimum':0,'maximum':100000},'limit':{'type':'integer','minimum':1,'maximum':50}},[]),
 schema('register_source_range','Create an executable candidate anywhere inside an inventoried source stream. Record observations as model-authored evidence, not verified transcript.',
        {'version_id':ID,'stream_index':{'type':'integer','minimum':0,'maximum':4096},'start_offset_seconds':{'type':'number','minimum':0},
         'duration_seconds':{'type':'number','exclusiveMinimum':0,'maximum':3600},'description':TEXT}),
 schema('record_story_plan','Persist the agent-authored story plan and uncertainties against real candidates; this does not pretend the user confirmed the new treatment.',
        {'intent':TEXT,'sections':{'type':'array','minItems':1,'maxItems':30,'items':{'type':'object','properties':{'purpose':TEXT,'candidate_ids':{'type':'array','items':ID,'maxItems':30},'treatment':TEXT},'required':['purpose','candidate_ids','treatment'],'additionalProperties':False}},
         'uncertainties':{'type':'array','items':TEXT,'maxItems':30}}),
 schema('read_story_plan','Read the last model-authored story plan for this run; the confirmed user intent remains separately available.'),
 schema('plan_episode','Plan a dialogue-led programme: audience question, real experience, response/payoff and a purpose for every selected shot. Optional start_frame/duration_frames mark beats within continuous shots without artificial cuts; beats must cover the timeline once in order. Link retained cue IDs when quoting speech. This is an authored treatment, not evidence that the story is entertaining.',
        {'premise':TEXT,'audience_question':TEXT,'payoff':TEXT,'beats':{'type':'array','minItems':1,'maxItems':30,'items':{'type':'object','properties':{
            'beat_id':ID,'role':{'enum':['hook','setup','experience','response','payoff','bridge']},
            **SPAN,
            'item_ids':{'type':'array','items':ID,'minItems':1,'maxItems':30,'uniqueItems':True},
            'dialogue_ids':{'type':'array','items':ID,'maxItems':30,'uniqueItems':True},'source_evidence':TEXT,'audience_takeaway':TEXT},
            'required':['beat_id','role','item_ids','dialogue_ids','source_evidence','audience_takeaway'],'additionalProperties':False}}}),
 schema('set_canvas','Choose a bounded SDR preview canvas before styling. Changes the revision and invalidates previous render review; source media and timing remain intact.',
        {'width':{'enum':[640,1280,1920]},'height':{'enum':[360,720,1080]},**SUMMARY}),
 schema('sample_source','Choose exact sample offsets inside a candidate. Returns actual frames with source timestamps; does not infer unseen motion.',
        {'candidate_id':ID,'offsets_seconds':{'type':'array','items':{'type':'number','minimum':0},'minItems':1,'maxItems':8}}),
 schema('inspect_candidates','Audition up to three candidate source ranges before choosing shots. Returns start/middle/end images, transcripts and evidence limitations in one call. Prefer this to repeatedly requesting single cached stills; refine with sample_source when action is missed.',
        {'candidate_ids':{'type':'array','items':ID,'minItems':1,'maxItems':3,'uniqueItems':True}}),
 schema('inspect_cut','Inspect start/middle/end of up to three current shots in one call, with exact source ranges, speech repair spans and overlapping text. Omit render_id to inspect sources before decorating; provide render_id for rendered review. All shots and text must be covered before finish.',
        {'item_ids':{'type':'array','items':ID,'minItems':1,'maxItems':3,'uniqueItems':True},'render_id':ID},['item_ids']),
 schema('inspect_pacing','Inspect actual source continuity across the current timeline. Adjacent pieces of the same take are grouped even when split; use this to find prolonged stretches to review. This does not decide whether a scene is interesting.'),
 schema('review_cut','Save an editorial assessment of the exact current revision after inspecting every changed rendered shot (unchanged shots accepted earlier in this run are carried and may be omitted). Keep specific unresolved problems as revise/matches=false; these block finish. This is a model assessment, not human approval.',
        {'shots':{'type':'array','maxItems':180,'items':{'type':'object','properties':{'item_id':ID,'observation':TEXT,'decision':{'enum':['keep','revise']}},'required':['item_id','observation','decision'],'additionalProperties':False}},
         'text_checks':{'type':'array','maxItems':960,'items':{'type':'object','properties':{'item_id':ID,'evidence':TEXT,'matches':{'type':'boolean'}},'required':['item_id','evidence','matches'],'additionalProperties':False}},
         'story_assessment':TEXT,'audio_limitations':TEXT}),
 schema('replace_clip_source','Replace one shot with an exact inventoried source span, including expansion to a complete utterance returned by list_transcripts. Reflows the timeline and invalidates overlays. Does not restrict selection to earlier candidate windows.',
        {'item_id':ID,'source':{'type':'object','properties':{'version_id':ID,'stream_index':{'type':'integer','minimum':0},'start_pts':{'type':'string','pattern':'^-?[0-9]+$'},'end_pts':{'type':'string','pattern':'^-?[0-9]+$'},'time_base':{'type':'object','properties':{'num':{'type':'integer','minimum':1},'den':{'type':'integer','minimum':1}},'required':['num','den'],'additionalProperties':False}},'required':['version_id','stream_index','start_pts','end_pts','time_base'],'additionalProperties':False},**SUMMARY}),
 schema('add_clip_title','Add a fact-grounded card confined to one shot; provide the actual visual/transcript evidence for named places. Do not carry a location label onto unrelated storefronts. Recutting invalidates cards.',
        {'item_id':ID,'start_offset_frames':FRAME,'duration_frames':LENGTH,'text':TEXT,'evidence':TEXT,'style_id':{'enum':['title.default','title.chapter','title.caption']},**SUMMARY}),
 schema('list_transcripts','Read complete source utterances remapped to the current cut, or a candidate before cutting. include_drafts explicitly exposes low-confidence ASR as unverified draft cues, not human-approved speech.',{'candidate_id':ID,'include_drafts':{'type':'boolean'}},[]),
 schema('replace_subtitles','Replace the editable subtitle track using returned cue IDs and faithful text; [] removes subtitles. source_excerpt selects an exact character range [start,end) in the source cue, with estimated timing, to omit uncertain ASR without inventing replacement speech. Omit unreliable cues entirely when needed; original audio is preserved.',
        {'captions':{'type':'array','items':{'type':'object','properties':{'cue_id':ID,'text':TEXT,'source_excerpt':{'type':'object','properties':{'start':{'type':'integer','minimum':0,'maximum':500},'end':{'type':'integer','minimum':1,'maximum':500}},'required':['start','end'],'additionalProperties':False},'delivery':{'enum':['dialogue','punchline','quiet']},'emphasis':{'type':'array','items':{'type':'string','minLength':1,'maxLength':16},'maxItems':4,'uniqueItems':True}},'required':['cue_id','text'],'additionalProperties':False},'maxItems':960},'allow_asr_draft':{'type':'boolean'},**SUMMARY},['captions','summary']),
 schema('edit_text','Revise one existing title/caption, its style and timing.',{'item_id':ID,**SPAN,'text':TEXT,'style_id':{'enum':['title.default','title.chapter','title.caption']},**SUMMARY}),
 schema('configure_text_style','Set editable text size, position, wrapping width, alignment and colors on one title/subtitle. Coordinates refer to the full output canvas.',
        {'item_id':ID,'font_size_ratio':{'type':'number','minimum':0.015,'maximum':0.15},'x':UNIT,'y':UNIT,
         'width_ratio':{'type':'number','minimum':0.1,'maximum':1},'align':{'enum':['left','center','right']},
         'text_color':{'type':'array','items':{'type':'integer','minimum':0,'maximum':255},'minItems':4,'maxItems':4},
         'background_color':{'type':'array','items':{'type':'integer','minimum':0,'maximum':255},'minItems':4,'maxItems':4},**SUMMARY}),
 schema('remove_item','Delete a selected timeline element; video deletion closes the gap and invalidates time-bound overlays.',{'item_id':ID,**SUMMARY}),
 schema('trim_clip','Trim within the current source span, close the gap, and invalidate overlays/audio placements whose timing changed.',
        {'item_id':ID,'start_offset_frames':FRAME,'duration_frames':LENGTH,**SUMMARY}),
 schema('reorder_clips','Reorder all video item IDs exactly once. Clears overlays and audio placements; inspect and reapply them against the new cut.',
        {'item_ids':{'type':'array','items':ID,'minItems':1,'maxItems':180},**SUMMARY}),
 schema('split_clip','Split a video clip at a relative frame. Existing effects/placements on that clip are cleared for explicit reapplication.',
        {'item_id':ID,'offset_frames':LENGTH,**SUMMARY}),
 schema('insert_clip','Insert a precisely trimmed candidate before a video item, or at end when before_item_id is null. Invalidates overlays.',
        {'candidate_id':ID,'start_offset_seconds':{'type':'number','minimum':0},'duration_seconds':{'type':'number','minimum':0.75,'maximum':30},'before_item_id':{'anyOf':[ID,{'type':'null'}]},**SUMMARY}),
 schema('set_source_audio','Choose camera sound treatment and gain for one video clip.',
        {'item_id':ID,'mode':{'enum':['keep','mute','dialogue']},'gain_db':GAIN,**SUMMARY}),
 schema('set_color_grade','Set explicit per-clip exposure/contrast/saturation; neutral values remove the visual grade.',
        {'item_id':ID,'contrast':{'type':'number','minimum':0.5,'maximum':2},'saturation':{'type':'number','minimum':0,'maximum':2},'brightness':{'type':'number','minimum':-0.3,'maximum':0.3},**SUMMARY}),
 schema('set_mosaic','Set static timed rectangular mosaic regions in final canvas coordinates. Multiple intervals permit manual tracking; no automatic tracking claim. [] clears.',
        {'item_id':ID,'regions':{'type':'array','items':{'type':'object','properties':RECT,'required':list(RECT),'additionalProperties':False},'maxItems':32},**SUMMARY}),
 schema('set_transition','Set a dip to black/white at the incoming clip boundary, or none. Duration is in timeline frames.',
        {'item_id':ID,'kind':{'enum':['dip_black','dip_white','none']},'duration_frames':{'type':'integer','minimum':2,'maximum':60},**SUMMARY}),
 schema('set_fade','Fade the beginning/end of THIS shot to black. Use fade_out_frames on the final shot for an actual ending. This is independent of incoming scene transitions. Zero removes that fade.',
        {'item_id':ID,'fade_in_frames':FRAME,'fade_out_frames':FRAME,**SUMMARY}),
 schema('remove_clip_effect','Remove any named semantic effect from a video/audio clip.',{'item_id':ID,'effect_id':ID,**SUMMARY}),
 schema('search_music','Search and acquire up to three public instrumental candidates. Only generic music tags leave the project; preserve source/license evidence.',
        {'tags':{'type':'array','items':{'enum':MUSIC_TAGS},'minItems':1,'maxItems':4}}),
 schema('list_audio_assets','List acquired audio candidates and provenance without filesystem paths.'),
 schema('extract_audio','Prepare a local source range as an audio asset for ambience, dialogue or sound effects. No ownership/license claim is invented.',
        {'candidate_id':ID,'start_offset_seconds':{'type':'number','minimum':0},'duration_seconds':{'type':'number','exclusiveMinimum':0,'maximum':90}}),
 schema('inspect_audio','Read measured loudness, peaks, onsets, BPM and source/license metadata. This is analysis, not listening.',{'asset_id':ID}),
 schema('place_audio','Add/update a named audio layer anchored to a video clip. Supports independent offsets, gain, fades and dialogue ducking.',
        {'item_id':ID,'layer_id':ID,'asset_id':ID,**SPAN,'source_offset_seconds':{'type':'number','minimum':0,'maximum':3600},'gain_db':GAIN,'duck':{'type':'boolean'},'fade_seconds':{'type':'number','minimum':0,'maximum':5},**SUMMARY}),
 schema('remove_audio','Remove one named audio layer from the current timeline.',{'layer_id':ID,**SUMMARY}),
 schema('render_preview','Render the current immutable revision locally for review. Asynchronous: inspect_preview reads the actual result. Reuses a successful exact-revision render.'),
 schema('inspect_preview','Wait at most 30 seconds for a run-owned preview and inspect exact output timestamps. Does not claim audio listening or full-motion review.',
        {'render_id':ID,'offsets_seconds':{'type':'array','items':{'type':'number','minimum':0},'maxItems':8},'wait_seconds':{'type':'number','minimum':0,'maximum':30}},['render_id','offsets_seconds']),
 schema('cancel_preview','Cancel a preview started in this run; preserves the timeline.',{'render_id':ID}),
 schema('validate_sequence','Check executable timing, effects, audio references and render freshness. Returns actionable issues; never aesthetic approval.'),
 schema('get_edit_history','Read saved revisions and tool events for this run.'),
 schema('restore_revision','Restore a run-owned immutable revision as a new current selection.',{'revision_id':ID,**SUMMARY}),
 schema('export_edit','Save native editable timeline JSON and source/license credits for the current revision. This is not a Resolve project.'),
)
TARGET = {"type": "object", "properties": {"x": UNIT, "y": UNIT,
          "radius": {"type": "number", "minimum": 0.015, "maximum": 0.2}},
          "required": ["x", "y", "radius"], "additionalProperties": False}
COMPONENT_FIELDS = {
    "element_id": ID, "item_id": ID,
    "font_style_id": {"enum":["default","japanese","street","travel","handwriting","calligraphy"]},
    "visual_style_id": ID,
    "component_id": {"enum": ["location", "person", "fact", "pointer", "circle", "reaction", "program_title", "headline", "impact", "chapter", "burst"]},
    "start_offset_frames": FRAME, "duration_frames": LENGTH,
    "text": {"type": "string", "minLength": 1, "maxLength": 48},
    "detail": {"type":"string","minLength":1,"maxLength":48,"description":"Grounded secondary line, e.g. the observed price or person's role. Required for fact unless the text itself has a number or price."},
    "evidence": TEXT,
    "x": {**UNIT, "description": "Required when adding. Chosen after looking at this shot: keep faces, hands, food and action clear; do not reuse one corner for every graphic."},
    "y": {**UNIT, "description": "Required when adding. See x."},
    "target": TARGET, **SUMMARY,
}
COMPONENT_REQUIRED = [key for key in COMPONENT_FIELDS if key not in {"x", "y", "target", "detail", "visual_style_id", "font_style_id"}]
SKILL_TOOLS = (
    schema("list_production_skills", "List bundled production skills; selecting one never analyzes or uploads footage."),
    schema("read_production_skill", "Read one resource from the skill version frozen for this session. Start with SKILL.md, then load relevant references.",
           {"resource": {"type": "string", "maxLength": 120}}, []),
    schema("list_program_components", "Read executable components, visual presets, exact tool parameters and limitations for the selected skill."),
    schema("add_program_component", "Add a purposeful in-picture programme graphic, anchored to a shot and bound to this skill. Not an independent title card. Positions are normalized canvas coordinates. Provide real source evidence; inspect faces/targets yourself. Add only where a moment needs it: never restate the concurrent subtitle or describe what is already visible; reaction/impact/burst land on the reaction beat for at most 2 seconds.", COMPONENT_FIELDS, COMPONENT_REQUIRED),
    schema("update_program_component", "Replace the settings of an existing programme graphic; preserves its identity and supports exact revision undo.", COMPONENT_FIELDS, COMPONENT_REQUIRED),
    schema("restyle_program_components", "Restyle all existing programme graphics or selected element IDs using one visual style frozen in the session. Optional font_style_id changes the type pairing independently. Keeps text, timing, anchors, cuts, captions and music. Does not change the default for future additions; pass visual_style_id when adding new graphics.",
           {"font_style_id":{"enum":["default","japanese","street","travel","handwriting","calligraphy"]},"visual_style_id":ID,"element_ids":{"type":"array","minItems":1,"maxItems":128,"uniqueItems":True,"items":ID},**SUMMARY}, ["visual_style_id","summary"]),
    schema("remove_program_component", "Remove one programme graphic without changing the cut, captions or music.", {"element_id": ID, **SUMMARY}),
)

EXTRA_TOOLS += SKILL_TOOLS
EXTRA_TOOLS += (
 schema('transcribe_source_range','Transcribe one registered source range locally, on demand. No old analysis/plan needed. Async job with explicit coverage; does not invent word timing.',
        {'candidate_id':ID,'language':{'enum':['zh','en','ja']},'align_words':{'type':'boolean'}},['candidate_id']),
 schema('align_source_speech','Align the exact ASR text from a successful transcription job to its source audio using an installed offline aligner. Does not verify wording or upload audio.',
        {'transcript_job_id':ID}),
 schema('read_evidence_job','Read a run-owned local evidence job. Wait up to 15 seconds; polling never repeats inference.',
        {'job_id':ID,'wait_seconds':{'type':'number','minimum':0,'maximum':15}},['job_id']),
 schema('cancel_evidence_job','Cancel local source transcription/alignment; preserves completed evidence and edit revisions.',{'job_id':ID}),
 schema('inspect_mix','Read measurements and dialogue-driven duck envelopes from an actual run-owned render. Reports camera/music/final stems separately; numeric evidence is not audio listening.',{'render_id':ID}),
 schema('prepare_local_speech','Prepare the fixed local transcription or word-alignment runtime when unavailable. Requires the session model-download grant. Async and cancellable; never uploads source audio.',{'target':{'enum':['transcription','alignment']}}),
 schema('retry_evidence_job','Explicitly retry a failed/cancelled/interrupted LOCAL evidence job as a new attempt. The old outcome is preserved; reuse request_id to avoid starting the same retry twice.',{'job_id':ID,'request_id':ID}),
 schema('revise_source_speech','Propose a grounded correction to one source utterance and re-align it locally. Preserves original ASR, reason and model-authored authority; never claims human verification.',
        {'transcript_job_id':ID,'segment_ordinal':{'type':'integer','minimum':0,'maximum':99},'text':TEXT,'evidence':TEXT}),
 schema('read_mutation_receipt','Read one run-owned mutation outcome after a lost response. A started receipt means outcome unknown: inspect the current revision before choosing another action.',{'mutation_id':ID}),
)
EXTRA_NAMES=tuple(t['name'] for t in EXTRA_TOOLS)
BY_NAME={t['name']:t for t in EXTRA_TOOLS}


def validate_arguments(name, arguments):
    errors=list(Draft202012Validator(BY_NAME[name]['inputSchema']).iter_errors(arguments))
    if errors:
        # Report the failing field and constraint, never echo source text/IDs or
        # a potentially huge value. Models otherwise repeat the same invalid call.
        error=errors[0];field='.'.join(str(p) for p in error.absolute_path) or 'arguments'
        constraint=error.validator
        expected=json.dumps(error.validator_value,ensure_ascii=False)[:240]
        raise ProjectError('agent_input_invalid',f'{name}: {field} 不符合 {constraint}={expected}；請依工具 schema 修正。',status_code=422)
