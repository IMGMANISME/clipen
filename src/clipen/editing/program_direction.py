"""User-selected programme direction, separate from model-authored treatments."""
from clipen.projects import ProjectError

PROGRAM_DIRECTION = {
    'format': 'travel_program',
    'title_cards': False,
    'subtitle_style': 'program',
    'direction': '以人物對話、期待、實際體驗與回應串起故事；不用獨立字卡交代行程。',
}

FULL_PRODUCTION_REQUIREMENTS = {'music': True, 'editing': True, 'transitions': True}


def validate_direction(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) not in (set(PROGRAM_DIRECTION),set(PROGRAM_DIRECTION)|{'production_requirements'})
        or value['format'] != 'travel_program' or value['title_cards'] is not False
        or value['subtitle_style'] != 'program' or not isinstance(value['direction'], str)
        or not 1 <= len(value['direction'].strip()) <= 1000):
        raise ProjectError('editing_direction_invalid', '節目方向需要明確的格式、字幕樣式與不使用字卡設定。', status_code=422)
    requirements=value.get('production_requirements')
    if 'production_requirements' in value and (not isinstance(requirements,dict) or set(requirements)!=set(FULL_PRODUCTION_REQUIREMENTS) or any(type(v) is not bool for v in requirements.values())):
        raise ProjectError('editing_direction_invalid','後製需求需明確指定 music、editing、transitions。',status_code=422)
    return dict(value)


def direction_for(tools):
    return tools.repository.creative_direction(tools.project_id, tools.run_id)


def require_title_cards(tools):
    if direction_for(tools).get('title_cards') is False:
        raise ProjectError('title_cards_disabled', '使用者要求不使用字卡。請以原片口語、節目字幕及畫面安排推進內容。', status_code=422)


def direction_issues(tools):
    if direction_for(tools).get('title_cards') is not False:
        return []
    from clipen.domain import TrackKind
    from .program_story import read_episode
    issues=[{'code':'title_cards_disabled', 'item_id':i.item_id}
            for t in tools._timeline().tracks if t.kind is TrackKind.TITLE and t.track_id not in {'agent-subtitles','program-components'} for i in t.items]
    from .production_media import load_text_styles
    styles=load_text_styles(tools.repository.projects,tools.project_id)
    issues += [{'code':'program_subtitle_style_required','item_id':i.item_id}
               for t in tools._timeline().tracks if t.track_id=='agent-subtitles' for i in t.items
               if styles.get(i.style_id,{}).get('kind')!='program_subtitle']
    from .speech_quality import speech_flags
    issues += [{'code':'subtitle_draft_unreliable','item_id':i.item_id,
                'message':'ASR 有可疑重複詞。請以 source_excerpt 選擇可用字串，或移除這段字幕並保留原音。'}
               for t in tools._timeline().tracks if t.track_id=='agent-subtitles' for i in t.items
               if (p:=styles.get(i.style_id,{})).get('verification')=='asr_draft'
               and speech_flags(i.text if p.get('source_excerpt') else p.get('source_text',i.text))]
    if tools._session_value() is None and read_episode(tools.repository.projects,tools.project_id,tools.run_id,tools._timeline()) is None:
        issues.append({'code':'episode_treatment_required','message':'請用 plan_episode 說明本段在問什麼、怎麼體驗、最後得到什麼，並連到實際鏡頭。'})
    return issues
