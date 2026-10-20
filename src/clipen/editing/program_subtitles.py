"""Source-linked programme captions; draft ASR never becomes human approval."""
import unicodedata
import re
import math
from clipen.domain import TextClip
from clipen.finishing.models import digest
from clipen.projects import ProjectError
from .production_media import load_text_styles, atomic_json, text_style_path


def make_subtitle_items(tools,cue,row):
    text=row['text'].strip()
    if not text:raise ProjectError('agent_input_invalid','字幕不可空白。',status_code=422)
    def spoken_content(value):
        return ''.join(c.casefold() for c in unicodedata.normalize('NFKC',value) if unicodedata.category(c)[0] not in 'PZC')
    original=cue;excerpt=row.get('source_excerpt');source_text=cue['text']
    if excerpt:
        left,right=excerpt['start'],excerpt['end']
        if not 0<=left<right<=len(source_text):raise ProjectError('subtitle_source_mismatch','字幕引用字元範圍超出來源句。',status_code=422)
        begin=cue['duration_frames']*left//len(source_text);end=cue['duration_frames']*right//len(source_text)
        cue={**cue,'text':source_text[left:right],'start_frame':cue['start_frame']+begin,'duration_frames':end-begin}
    if (excerpt or cue.get('verification')=='asr_draft') and spoken_content(text)!=spoken_content(cue['text']):
        raise ProjectError('subtitle_source_mismatch','未核對字幕草稿可以調整標點與分句，不能改寫原話內容或加入新事實；請先核對來源逐字稿。',status_code=422)
    from .speech_quality import speech_flags
    if cue.get('verification')=='asr_draft' and speech_flags(text):
        raise ProjectError('subtitle_draft_unreliable','這段 ASR 有可疑的連續重複詞，不能直接作為字幕。請用 source_excerpt 引用原句中可用的部分，或略過這段字幕並保留原音；不可編造替代台詞。',status_code=422)
    # Page boundaries are an explicit estimate inside the original ASR segment,
    # not invented word-level alignment. The original audio remains untouched.
    spans=[];cursor=0
    for match in re.finditer(r'[^，,。！？!?；;]+[，,。！？!?；;]*',text):
        end=match.end();phrase=text[cursor:end];count=max(1,math.ceil(len(phrase)/18))
        spans.extend((cursor+len(phrase)*n//count,cursor+len(phrase)*(n+1)//count) for n in range(count));cursor=end
    if cursor<len(text):spans.append((cursor,len(text)))
    pages=[]
    for left,right in spans:
        if pages and pages[-1][1]-pages[-1][0]<6 and right-pages[-1][0]<=18:
            pages[-1]=(pages[-1][0],right)
        else:pages.append((left,right))
    if cue['duration_frames']<len(pages):raise ProjectError('subtitle_too_short','口語時間不足以顯示分句字幕。',status_code=422)
    styles=load_text_styles(tools.repository.projects,tools.project_id);items=[]
    for n,(left,right) in enumerate(pages):
        start=cue['start_frame']+cue['duration_frames']*left//len(text)
        end=cue['start_frame']+cue['duration_frames']*right//len(text)
        delivery=row.get('delivery','dialogue')
        profile={'kind':'program_subtitle','font_size_ratio':.075 if delivery=='punchline' else .05 if delivery=='quiet' else .06,
            'delivery':delivery,'emphasis':[s for s in row.get('emphasis',[]) if s in text[left:right]],
            'verification':cue.get('verification','reviewed_source'),'source_revision_id':cue.get('source_revision_id'),
            'segment_id':cue.get('segment_id'),'source':cue.get('source'),'source_text':source_text,
            'source_excerpt':excerpt,'cue_id':cue['cue_id'],'timing_basis':'character_proportional_within_asr_segment' if excerpt or len(pages)>1 else 'asr_segment',
            'caption_text_edited':text!=original['text'],'motion':'none' if delivery=='quiet' else 'pop'}
        identifier='agent.text.'+digest(profile)[:24];styles[identifier]=profile
        items.append(TextClip('subtitle-'+cue['cue_id']+'-'+str(n),start,end-start,text[left:right],identifier))
    if len(styles)>256:raise ProjectError('agent_input_invalid','自訂字幕樣式索引超過上限。',status_code=422)
    atomic_json(text_style_path(tools.repository.projects,tools.project_id),styles)
    return items


def subtitle_status(projects,project_id,timeline):
    from clipen.domain import TrackKind
    styles=load_text_styles(projects,project_id)
    items=[i for t in timeline.tracks if t.kind is TrackKind.TITLE and t.track_id=='agent-subtitles' for i in t.items]
    drafts=[i.item_id for i in items if styles.get(i.style_id,{}).get('verification')=='asr_draft']
    return {'count':len(items),'draft_count':len(drafts),'draft_item_ids':drafts,
        'audio_listened':False,'review_scope':'Source-linked draft captions are not human-verified transcription.'}
