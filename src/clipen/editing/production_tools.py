"""Executable production tools. The model chooses actions; this layer owns IO and revisions."""
from dataclasses import replace
from fractions import Fraction
import json
import os
import secrets
import threading

from clipen.domain import ClipSource, EffectRequest, MediaClip, SourceSpan, TextClip, Track, TrackKind
from clipen.projects import ProjectError
from .agent_models import ToolResult
from .production_catalog import validate_arguments


def problem(message):
    return ProjectError('agent_input_invalid',message,status_code=422)


class ProductionTools:
    def production_call(self,name,arguments):
        validate_arguments(name,arguments)
        return getattr(self,'_production_'+name)(arguments)

    def _timeline(self):
        return self.repository.current_revision(self.project_id,self.run_id).timeline

    def _save(self,timeline,summary,**details):
        revision,created=self.repository.save_revision(self.project_id,self.run_id,timeline,summary,
            expected_revision_id=getattr(self,'_expected_revision_id',None),allow_user_edit=getattr(self,'_user_edit',False))
        return ToolResult({'revision':revision.to_public(),'created':created,**details})

    def _video(self):
        return next(t for t in self._timeline().tracks if t.kind is TrackKind.VIDEO)

    def _item(self,item_id):
        for t in self._timeline().tracks:
            for item in t.items:
                if item.item_id==item_id:return item
        raise ProjectError('timeline_item_not_found','找不到指定片段。',status_code=404)

    def _change_item(self,item,summary):
        timeline=self._timeline()
        return self._save(replace(timeline,tracks=tuple(replace(t,items=tuple(item if i.item_id==item.item_id else i for i in t.items)) for t in timeline.tracks)),summary)

    def _effect(self,item_id,effect_id,parameters,summary):
        item=self._item(item_id)
        if not isinstance(item,MediaClip):raise problem('效果需要媒體片段。')
        if parameters is not None and (effect_id.startswith('video.') or effect_id=='audio.source') and item not in self._video().items:
            raise problem('這項處理只能套用於主畫面片段。')
        if parameters is not None and effect_id=='audio.source' and any(t.items for t in self._timeline().tracks if t.kind is TrackKind.AUDIO):
            raise problem('既有獨立音軌會取代原音；請先重剪或移除獨立音軌項目，再設定鏡頭原音。')
        effects=tuple(e for e in item.effects if e.effect_id!=effect_id)
        if parameters is not None:effects+=(EffectRequest(effect_id,parameters),)
        return self._change_item(replace(item,effects=effects),summary)

    def _recut(self,items,summary):
        if not items:raise problem('至少保留一個畫面片段。')
        from .recut import remap_cut
        timeline,invalidated=remap_cut(self,items)
        return self._save(timeline,summary,invalidated=invalidated,
                          next_step='Unchanged post-production was remapped. Review changed boundaries and repair listed dependencies.')

    def _production_list_editing_tools(self,args):
        from dataclasses import asdict
        from .production_limits import limits_for
        from clipen.mcp_editing.server import _TOOLS
        return ToolResult({'tools':[{'name':t['name'],'description':t['description']} for t in _TOOLS],'decision_owner':'LLM','execution_owner':'Clipen',
            'limits':{'production':asdict(limits_for(self)), 'target_duration_seconds':(self._session_value() or {}).get('intent',{}).get('target_duration_seconds',60), 'max_tool_calls':self.repository.get(self.project_id,self.run_id).max_tool_calls,
                      'max_preview_renders':self.repository.get(self.project_id,self.run_id).max_preview_renders,
                      'preview_scope':'SDR production within session limits; technical validation is not aesthetic approval'},
            'unsupported':{'automatic_object_tracking':'Use explicit time intervals and rectangles; inspect every affected interval.',
                'audio_listening':'This provider receives measured audio evidence, not audio perception.',
                'generated_media_or_voice':'No generation provider configured.',
                'resolve_project':'Native timeline JSON only; no verified Resolve exporter.',
                'HDR_mastering':'SDR preview pipeline only.'}})

    def _production_set_target_duration(self,args):
        updated=self.repository.set_target_duration(self.project_id,self.run_id,
            self._expected_revision_id,**args)
        self._session_cache=self.repository.production_session(self.project_id,self.run_id)
        from dataclasses import asdict
        from .production_limits import limits_for
        return ToolResult({**updated,'limits':asdict(limits_for(self))})

    def _custom_candidates(self):
        from clipen.planning.models import PlanningCandidate
        path=self._workdir('analysis')/'candidates.json'
        if path.is_symlink():raise problem('候選索引不安全。')
        if not path.exists():return ()
        if path.stat().st_size>2*1024*1024:raise problem('候選索引超過上限。')
        return tuple(PlanningCandidate.from_storage(v) for v in json.loads(path.read_text()))

    def _production_list_source_media(self,args):
        from .production_media import source_rows
        rows=self._source_rows();offset=args.get('offset',0);limit=args.get('limit',20)
        return ToolResult({'sources':rows[offset:offset+limit],'total':len(rows),'next_offset':offset+limit if offset+limit<len(rows) else None,
                          'next_step':'register_source_range creates candidates for sample_source, transcripts and editing; no prior analysis window is required.'})

    def _production_get_program_reference(self,args):
        from .program_reference import reference_for
        return ToolResult({'reference':reference_for(self.repository,self.project_id,self.run_id),
            'fallback':'Use the user creative_direction when no accepted reference exists. Do not invent user approval.'})

    def _production_search_source_dialogue(self,args):
        from .source_dialogue import search_dialogue
        return search_dialogue(self,args)

    def _production_register_source_range(self,args):
        from .production_media import source_rows,atomic_json
        from clipen.finishing.models import digest
        from clipen.domain import Rational
        from clipen.planning.models import PlanningCandidate,TranscriptReliability
        row=next((r for r in self._source_rows() if r['version_id']==args['version_id'] and r['stream_index']==args['stream_index']),None)
        if row is None:raise problem('來源不在目前作品的可用原片清單。')
        base=Fraction(row['time_base']['num'],row['time_base']['den']);offset=Fraction(str(args['start_offset_seconds']));duration=Fraction(str(args['duration_seconds']))
        start=int(row['start_pts'])+round(offset/base);end=start+round(duration/base)
        if end==int(row['end_pts'])+1:end=int(row['end_pts'])
        if start<int(row['start_pts']) or end>int(row['end_pts']) or end<=start:raise problem('指定範圍超出原片串流。')
        span=SourceSpan(row['version_id'],row['stream_index'],str(start),str(end),Rational(base.numerator,base.denominator))
        identifier='candidate-'+digest(span.to_storage())[:32]
        candidate=PlanningCandidate(identifier,span,row['display_name'],row['kind'],'LLM observation (not independently verified): '+args['description'],TranscriptReliability.UNAVAILABLE)
        candidates={c.candidate_id:c for c in self._custom_candidates()};candidates[identifier]=candidate
        if len(candidates)>512:raise problem('本次新增候選已達上限。')
        atomic_json(self._workdir('analysis')/'candidates.json',[c.to_storage() for c in candidates.values()])
        return ToolResult({'candidate':candidate.to_provider(),'authority':'model_authored_observation','source':span.to_storage()})

    def _production_record_story_plan(self,args):
        from .production_media import atomic_json
        for section in args['sections']:
            for identifier in section['candidate_ids']:self._candidate(identifier)
        plan={'authority':'model_authored','user_intent':self._context_value().brief,**args}
        atomic_json(self._workdir('analysis')/'story-plan.json',plan)
        return ToolResult({'story_plan':plan})

    def _production_read_story_plan(self,args):
        path=self._workdir('analysis')/'story-plan.json'
        if path.is_symlink():raise problem('敘事計畫檔案不安全。')
        if path.is_file() and path.stat().st_size>512000:raise problem('敘事計畫超過上限。')
        from .program_story import read_episode
        return ToolResult({'story_plan':json.loads(path.read_text()) if path.is_file() else None,
            'episode':read_episode(self.repository.projects,self.project_id,self.run_id,self._timeline())})

    def _production_set_canvas(self,args):
        if args['width']*9!=args['height']*16:raise problem('目前樣片畫布需要 16:9。')
        return self._save(replace(self._timeline(),width=args['width'],height=args['height']),args['summary'])

    def _production_plan_episode(self,args):
        from .program_story import plan_episode
        return plan_episode(self,args)

    def _production_sample_source(self,args):
        from clipen.media import MediaPreviewRepository
        from .production_media import sample_frames
        candidate=self._candidate(args['candidate_id']);span=candidate.source
        base=Fraction(span.time_base.numerator,span.time_base.denominator)
        duration=span.duration_pts*base
        if any(Fraction(str(o))>=duration for o in args['offsets_seconds']):raise problem('取樣時間超出候選來源範圍。')
        media=getattr(self,'media',None) or MediaPreviewRepository(self.repository.projects)
        paths,manifest=sample_frames(media,self.project_id,span,args['offsets_seconds'],self._workdir('samples'),self._cancel_event())
        return ToolResult({'candidate_id':candidate.candidate_id,'samples':manifest,'coverage':'selected instants, not continuous motion'},paths)

    def _production_inspect_cut(self,args):
        from .editorial_review import inspect_cut
        return inspect_cut(self,args)

    def _production_inspect_candidates(self,args):
        candidates=[];images=[]
        for identifier in args['candidate_ids']:
            candidate=self._candidate(identifier)
            if candidate.media_kind!='video':raise problem('畫面試剪需要影片候選。')
            span=candidate.source;duration=float(span.duration_pts*Fraction(span.time_base.numerator,span.time_base.denominator))
            offsets=[duration*.05,duration*.5,duration*.95]
            result=self._production_sample_source({'candidate_id':identifier,'offsets_seconds':offsets})
            speech=self._production_list_transcripts({'candidate_id':identifier}).payload
            candidates.append({'candidate':candidate.to_provider(),'samples':result.payload['samples'],'transcripts':speech,'image_count':len(result.images)})
            images.extend(result.images)
        return ToolResult({'candidates':candidates,'image_order':'candidate order, then sample order','coverage':'three source stills per range, not continuous motion or audio listening'},tuple(images))

    def _production_inspect_pacing(self,args):
        from .programme_review import pacing
        return ToolResult(pacing(self._timeline()))

    def _production_review_cut(self,args):
        from .editorial_review import review_cut
        return review_cut(self,args)

    def _production_replace_clip_source(self,args):
        from .production_media import source_rows
        from clipen.domain import Rational
        item=self._item(args['item_id']);source=args['source']
        if item not in self._video().items:raise problem('只能替換主畫面來源。')
        row=next((r for r in self._source_rows() if r['version_id']==source['version_id'] and r['stream_index']==source['stream_index']),None)
        if row is None or row['kind']!='video' or row['time_base']!=source['time_base']:raise problem('指定來源不在目前影像原片清單，或時間基準不符。')
        start,end=int(source['start_pts']),int(source['end_pts'])
        if not int(row['start_pts'])<=start<end<=int(row['end_pts']):raise problem('來源範圍超出原片。')
        unit=Fraction(source['time_base']['num'],source['time_base']['den'])
        duration=(end-start)*unit
        if duration>30:raise problem('樣片單鏡最多 30 秒；請選擇完整且合適的口語或重新安排敘事。')
        span=SourceSpan(source['version_id'],source['stream_index'],str(start),str(end),Rational(unit.numerator,unit.denominator))
        fps=Fraction(self._timeline().frame_rate.numerator,self._timeline().frame_rate.denominator)
        updated=replace(item,source=ClipSource(span),duration_frames=max(1,round(duration*fps)))
        return self._recut([updated if i.item_id==item.item_id else i for i in self._video().items],args['summary'])

    def _production_add_clip_title(self,args):
        from .production_preview import _event
        item=self._item(args['item_id'])
        if item not in self._video().items or args['start_offset_frames']+args['duration_frames']>item.duration_frames:raise problem('字卡必須完整落在指定鏡頭內。')
        result=self._add_title({'start_frame':item.start_frame+args['start_offset_frames'],'duration_frames':args['duration_frames'],
            'text':args['text'],'style_id':args['style_id'],'summary':args['summary']})
        _event(self,'text_evidence',{'revision_id':result.payload['revision']['revision_id'],'shot_id':item.item_id,'text':args['text'],'evidence':args['evidence'],'authority':'model_authored'})
        return result

    def _production_list_transcripts(self,args):
        from .production_media import transcript_cues
        timeline=self._timeline()
        if args.get('candidate_id'):
            candidate=self._candidate(args['candidate_id']);span=candidate.source
            if candidate.media_kind!='video':raise problem('候選字幕目前需要影片來源。')
            frames=max(1,round(span.duration_pts*Fraction(span.time_base.numerator,span.time_base.denominator)*Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)))
            timeline=replace(timeline,duration_frames=frames,tracks=(Track('source-inspection',TrackKind.VIDEO,(MediaClip(candidate.candidate_id,0,frames,ClipSource(span)),)),))
        cues,limitations=transcript_cues(self.repository.projects,self.project_id,timeline,**({'include_drafts':True} if args.get('include_drafts') else {}))
        from .evidence_jobs import local_cues
        local,local_limits=local_cues(self,timeline,include_drafts=args.get('include_drafts',False))
        if local:
            cues=[c for c in cues if not any(c['item_id']==l['item_id'] and c['start_frame']<l['start_frame']+l['duration_frames'] and l['start_frame']<c['start_frame']+c['duration_frames'] for l in local)]+local
            cues.sort(key=lambda c:c['start_frame'])
        limitations+=local_limits
        return ToolResult({'cues':cues,'limitations':limitations,'policy':'Complete source utterances only. asr_draft is provisional, never human-approved. No fabricated speech.'})

    def _production_replace_subtitles(self,args):
        available={c['cue_id']:c for c in self._production_list_transcripts({'include_drafts':args.get('allow_asr_draft',False)}).payload['cues']}
        from .program_direction import direction_for
        program=direction_for(self).get('subtitle_style')=='program'
        from .program_subtitles import make_subtitle_items
        captions=[];seen=set()
        for row in args['captions']:
            cue=available.get(row['cue_id'])
            if cue is None or row['cue_id'] in seen:raise problem('字幕引用了不存在、不完整或重複的口語。')
            seen.add(row['cue_id'])
            if len(row['text'])>80:raise problem('單段字幕請分成可閱讀的句子，最多 80 字。')
            if any(word not in row['text'] for word in row.get('emphasis',[])):raise problem('重點字必須實際存在於字幕句內。')
            if program or cue.get('verification')=='asr_draft' or any(k in row for k in ('source_excerpt','delivery','emphasis')):captions.extend(make_subtitle_items(self,cue,row))
            else:captions.append(TextClip('subtitle-'+row['cue_id'],cue['start_frame'],cue['duration_frames'],row['text'],'title.caption'))
        from .production_limits import limits_for
        if len(captions)>limits_for(self).titles:raise problem('分句後超過本次製作的字幕上限。')
        timeline=self._timeline()
        retained=tuple(t for t in timeline.tracks if t.track_id!='agent-subtitles')
        if captions:retained+=(Track('agent-subtitles',TrackKind.TITLE,tuple(captions)),)
        return self._save(replace(timeline,tracks=retained),args['summary'])

    def _production_edit_text(self,args):
        item=self._item(args['item_id'])
        if not isinstance(item,TextClip):raise problem('指定項目不是文字。')
        self._require_generic_text(item)
        if getattr(self,'_user_edit',False):args={**args,'style_id':item.style_id}
        if args['start_frame']+args['duration_frames']>self._timeline().duration_frames:raise problem('文字超過時間軸結尾。')
        if getattr(self,'_user_edit',False):
            from .production_media import atomic_json,load_text_styles,text_style_path
            from clipen.finishing.models import digest
            styles=load_text_styles(self.repository.projects,self.project_id)
            profile=styles.get(item.style_id,{})
            text_changed=args['text']!=item.text
            time_changed=(args['start_frame'],args['duration_frames'])!=(item.start_frame,item.duration_frames)
            if profile.get('kind')=='program_subtitle' and (text_changed or time_changed):
                invalidated=profile.get('text_alignment_invalidated',False) or text_changed
                profile={**profile,**({'verification':'user_edited','wording_authority':'user'} if text_changed else {}),
                    'timing_basis':'alignment_invalidated_by_user_text' if invalidated else 'user_adjusted',
                    'text_alignment_invalidated':invalidated,'caption_text_edited':profile.get('caption_text_edited',False) or text_changed,
                    'previous_timing_basis':profile.get('previous_timing_basis',profile.get('timing_basis')),
                    'user_edit_base_revision_id':self.repository.current_revision(self.project_id,self.run_id).revision_id}
                style_id='agent.text.'+digest(profile)[:24]
                styles[style_id]=profile;atomic_json(text_style_path(self.repository.projects,self.project_id),styles)
                args={**args,'style_id':style_id}
        return self._change_item(replace(item,**{k:args[k] for k in ('start_frame','duration_frames','text','style_id')}),args['summary'])

    def _production_remove_item(self,args):
        item=self._item(args['item_id'])
        if item in self._video().items:return self._recut([i for i in self._video().items if i.item_id!=item.item_id],args['summary'])
        timeline=self._timeline()
        return self._save(replace(timeline,tracks=tuple(replace(t,items=tuple(i for i in t.items if i.item_id!=item.item_id)) for t in timeline.tracks)),args['summary'])

    def _production_configure_text_style(self,args):
        from .production_media import atomic_json,text_style_path,load_text_styles
        from clipen.finishing.models import digest
        item=self._item(args['item_id'])
        if not isinstance(item,TextClip):raise problem('自訂文字樣式需要字幕或字卡。')
        self._require_generic_text(item)
        profile={k:v for k,v in args.items() if k not in {'item_id','summary'}}
        if profile['x']+profile['width_ratio']>1:raise problem('文字區塊超出畫布右邊界。')
        style_id='agent.text.'+digest(profile)[:24]
        styles=load_text_styles(self.repository.projects,self.project_id)
        if len(styles)>=4096 and style_id not in styles:raise problem('自訂文字樣式已達上限。')
        styles[style_id]=profile
        atomic_json(text_style_path(self.repository.projects,self.project_id),styles)
        return self._change_item(replace(item,style_id=style_id),args['summary'])

    def _require_generic_text(self,item):
        from .production_media import load_text_styles
        if getattr(self,'_user_edit',False):return
        if load_text_styles(self.repository.projects,self.project_id).get(item.style_id,{}).get('kind') in {'program_subtitle','program_component'}:
            raise problem('節目字幕需保留來源與斷句資訊。請用 replace_subtitles 調整 text、source_excerpt、delivery、emphasis；通用字卡工具不能覆寫來源字幕。')

    def _trim(self,item,offset,duration):
        if not isinstance(item,MediaClip) or offset+duration>item.duration_frames:raise problem('裁切超出片段範圍。')
        timeline=self._timeline();span=item.source.span
        unit=Fraction(span.time_base.numerator,span.time_base.denominator)
        fps=Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)
        start=int(span.start_pts)+round(Fraction(offset,1)/fps/unit)
        end=start+round(Fraction(duration,1)/fps/unit)
        if end>int(span.end_pts):raise problem('裁切超出原片時間範圍。')
        return replace(item,duration_frames=duration,source=ClipSource(replace(span,start_pts=str(start),end_pts=str(end))))

    def _production_trim_clip(self,args):
        item=self._item(args['item_id'])
        if item not in self._video().items:raise problem('只能精剪主畫面片段。')
        trimmed=self._trim(item,args['start_offset_frames'],args['duration_frames'])
        return self._recut([trimmed if i.item_id==item.item_id else i for i in self._video().items],args['summary'])

    def _production_reorder_clips(self,args):
        clips={i.item_id:i for i in self._video().items}
        if len(args['item_ids'])!=len(clips) or set(args['item_ids'])!=set(clips):raise problem('必須完整且不重複地列出畫面片段。')
        return self._recut([clips[i] for i in args['item_ids']],args['summary'])

    def _production_split_clip(self,args):
        item=self._item(args['item_id']);offset=args['offset_frames']
        if item not in self._video().items or not offset<item.duration_frames:raise problem('分割點必須在畫面片段內。')
        left=self._trim(item,0,offset);right=replace(self._trim(item,offset,item.duration_frames-offset),item_id='split-'+secrets.token_hex(12),
            effects=tuple(e for e in item.effects if e.effect_id in {'video.grade','audio.source','video.punch_in','video.reframe'}))
        items=[]
        for i in self._video().items:items.extend([left,right] if i.item_id==item.item_id else [i])
        return self._recut(items,args['summary'])

    def _production_insert_clip(self,args):
        candidate,span,_=self._trimmed_candidate({k:args[k] for k in ('candidate_id','start_offset_seconds','duration_seconds')})
        if candidate.media_kind!='video':raise problem('主畫面需要影片來源。')
        timeline=self._timeline();fps=Fraction(timeline.frame_rate.numerator,timeline.frame_rate.denominator)
        frames=round(span.duration_pts*Fraction(span.time_base.numerator,span.time_base.denominator)*fps)
        item=MediaClip('insert-'+secrets.token_hex(12),0,frames,ClipSource(span))
        items=list(self._video().items)
        before=args['before_item_id']
        if before is None:items.append(item)
        else:
            index=next((i for i,v in enumerate(items) if v.item_id==before),None)
            if index is None:raise problem('找不到插入位置。')
            items.insert(index,item)
        return self._recut(items,args['summary'])

    def _production_set_source_audio(self,args):
        return self._effect(args['item_id'],'audio.source',{k:args[k] for k in ('mode','gain_db')},args['summary'])

    def _production_set_color_grade(self,args):
        return self._effect(args['item_id'],'video.grade',{k:args[k] for k in ('contrast','saturation','brightness')},args['summary'])

    def _production_set_mosaic(self,args):
        item=self._item(args['item_id'])
        for r in args['regions']:
            if r['x']+r['width']>1 or r['y']+r['height']>1 or r['start_frame']+r['duration_frames']>item.duration_frames:raise problem('馬賽克超出畫面或片段時間。')
        return self._effect(item.item_id,'video.mosaic',{'regions':args['regions']} if args['regions'] else None,args['summary'])

    def _production_set_transition(self,args):
        item=self._item(args['item_id']);clips=list(self._video().items)
        if item not in clips or clips.index(item)==0:raise problem('轉場需要有效的前後鏡頭。')
        if args['duration_frames']>min(item.duration_frames,clips[clips.index(item)-1].duration_frames):raise problem('轉場超出相鄰鏡頭長度。')
        return self._effect(item.item_id,'video.transition',{'kind':args['kind'],'duration_frames':args['duration_frames']} if args['kind']!='none' else None,args['summary'])

    def _production_set_fade(self,args):
        item=self._item(args['item_id'])
        if args['fade_in_frames']+args['fade_out_frames']>item.duration_frames:raise problem('淡入與淡出合計不可超過片段長度。')
        return self._effect(item.item_id,'video.fade',{k:args[k] for k in ('fade_in_frames','fade_out_frames')} if args['fade_in_frames'] or args['fade_out_frames'] else None,args['summary'])

    def _production_remove_clip_effect(self,args):
        return self._effect(args['item_id'],args['effect_id'],None,args['summary'])

    def _asset_store(self):
        from .production_media import AgentAudioStore
        return AgentAudioStore(self.repository.projects,self.project_id)

    def _production_search_music(self,args):
        choice=(self._session_value() or {}).get('intent',{}).get('music_choice')
        if choice and choice.get('mode')=='user':raise problem('使用者已指定配樂，請使用 list_audio_assets 取得該曲，不要另行搜尋替換。')
        from clipen.finishing.catalog import PublicMusicCatalog
        store=self._asset_store()
        assets,reference=PublicMusicCatalog().search_and_prepare({'music_tags':args['tags']},store.directory/('search-'+secrets.token_hex(12)),self._cancel_event(),lambda _:None)
        store.add(assets,reference)
        found={a['asset_id'] for a in assets}
        return ToolResult({'assets':[a for a in store.public_assets() if a['asset_id'] in found],
            'query_tags':args['tags'],'other_available_asset_ids':[a['asset_id'] for a in store.public_assets() if a['asset_id'] not in found],
            'selection_policy':'These are results of THIS search, not the oldest acquired tracks. Compare genre, tags and measured tempo with the requested mood; metadata is not listening.'})

    def _production_list_audio_assets(self,args):
        return ToolResult({'assets':self._asset_store().public_assets(),'perception':'measured evidence only; no audio listening'})

    def _production_inspect_audio(self,args):
        value=next((a for a in self._asset_store().public_assets() if a['asset_id']==args['asset_id']),None)
        if value is None:raise problem('找不到已取得的音訊。')
        return ToolResult({'asset':value,'heard_by_model':False})

    def _production_extract_audio(self,args):
        from clipen.media import MediaPreviewRepository
        from clipen.projects import DirectoryReference,DirectoryRole
        from clipen.projects.storage import utc_now
        from clipen.finishing.audio import inspect_library,run_process
        candidate=self._candidate(args['candidate_id']);span=candidate.source
        unit=Fraction(span.time_base.numerator,span.time_base.denominator)
        if Fraction(str(args['start_offset_seconds']))+Fraction(str(args['duration_seconds']))>span.duration_pts*unit:raise problem('擷取音訊超過候選範圍。')
        source=(getattr(self,'media',None) or MediaPreviewRepository(self.repository.projects)).open(self.project_id,span.version_id)
        store=self._asset_store();directory=store.directory/('extract-'+secrets.token_hex(12));directory.mkdir(mode=0o700)
        try:
            if not source.audio_stream_indices:raise problem('指定來源沒有音軌。')
            stream=span.stream_index if candidate.media_kind=='audio' else source.audio_stream_indices[0]
            if stream not in source.audio_stream_indices:raise problem('指定來源沒有這個音訊串流。')
            start=float(int(span.start_pts)*unit)+args['start_offset_seconds']
            run_process(['ffmpeg','-v','error','-nostdin','-seek_timestamp','1','-ss',f'{start:.9f}','-i',f'/dev/fd/{source.descriptor}',
                         '-map',f'0:{stream}','-t',str(args['duration_seconds']),'-vn','-c:a','pcm_s16le',str(directory/'source.wav')],self._cancel_event(),pass_fds=(source.descriptor,),timeout=120)
        finally:source.close()
        info=directory.stat();reference=DirectoryReference(DirectoryRole.SOURCE,str(directory),directory.name,directory.name,info.st_dev,info.st_ino,utc_now()).to_storage()
        assets,issues=inspect_library('sfx',reference,self._cancel_event(),lambda _:None)
        if not assets:raise problem('來源音訊無法分析或沒有可用聲音。')
        for a in assets:
            a['name']=candidate.display_name
            a['provenance']={'source':'project_media','candidate_id':candidate.candidate_id,'version_id':span.version_id,
                'source_offset_seconds':args['start_offset_seconds'],'duration_seconds':args['duration_seconds'],
                'license':'user_source_rights_unknown','attribution':'使用者提供的來源素材；權利條件未由系統確認。','changes':'截取並轉為 PCM 音訊。'}
        store.add(assets,reference)
        return ToolResult({'assets':[{k:v for k,v in a.items() if k not in {'relative_path','fingerprint','mtime_ns','size_bytes'}} for a in assets],'issues':issues})

    def _production_place_audio(self,args):
        from clipen.finishing.audio import open_asset
        context=self._asset_store().context(args['asset_id'])
        fd,asset=open_asset(context,args['asset_id']);os.close(fd)
        item=self._item(args['item_id'])
        if item not in self._video().items:raise problem('音訊必須錨定於畫面片段。')
        fps=self._timeline().frame_rate.numerator/self._timeline().frame_rate.denominator
        if item.start_frame+args['start_frame']+args['duration_frames']>self._timeline().duration_frames:raise problem('音訊超過影片結尾。')
        if args['source_offset_seconds']+args['duration_frames']/fps>asset['duration_seconds']:raise problem('音訊來源長度不足。')
        timeline=self._timeline();tracks=[]
        for t in timeline.tracks:
            items=[]
            for i in t.items:
                if not isinstance(i,MediaClip):items.append(i);continue
                layers=[l for e in i.effects if e.effect_id=='audio.layers' for l in e.parameters['layers'] if l['layer_id']!=args['layer_id']]
                if i.item_id==item.item_id:layers.append({k:v for k,v in args.items() if k not in {'item_id','summary'}})
                effects=tuple(e for e in i.effects if e.effect_id!='audio.layers')
                if layers:effects+=(EffectRequest('audio.layers',{'layers':layers}),)
                items.append(replace(i,effects=effects))
            tracks.append(replace(t,items=tuple(items)))
        return self._save(replace(timeline,tracks=tuple(tracks)),args['summary'])

    def _production_remove_audio(self,args):
        timeline=self._timeline();tracks=[]
        for t in timeline.tracks:
            items=[]
            for i in t.items:
                if not isinstance(i,MediaClip):items.append(i);continue
                layers=[l for e in i.effects if e.effect_id=='audio.layers' for l in e.parameters['layers'] if l['layer_id']!=args['layer_id']]
                effects=tuple(e for e in i.effects if e.effect_id!='audio.layers')
                if layers:effects+=(EffectRequest('audio.layers',{'layers':layers}),)
                items.append(replace(i,effects=effects))
            tracks.append(replace(t,items=tuple(items)))
        return self._save(replace(timeline,tracks=tuple(tracks)),args['summary'])

    def _production_validate_sequence(self,args):
        from .production_media import validate_production_timeline,load_text_styles
        from .production_preview import finish_review_status
        from clipen.finishing.audio import open_asset
        from .production_limits import limits_for,production_limit_issues
        issues=validate_production_timeline(self._timeline(),max_texts=limits_for(self).titles)
        limit_issues=production_limit_issues(self,self._timeline()) if self._session_value() else []
        issues+=limit_issues
        component_issues=self._component_issues()
        content_codes={'program_component_collision','component_restates_subtitle'}
        issues+=[i for i in component_issues if i['code'] not in content_codes]
        from .program_direction import direction_issues
        delivery_issues=direction_issues(self)
        if self._session_value():
            invalid_fields={i['field'] for i in limit_issues}
            delivery_issues += [i for i in production_limit_issues(self,self._timeline(),complete=True) if i['field'] not in invalid_fields]
        delivery_issues+=[i for i in component_issues if i['code'] in content_codes]
        from .production_requirements import requirement_issues
        delivery_issues+=requirement_issues(self)
        if self._session_value() and not any(t.items for t in self._timeline().tracks if (t.track_id=='program-components' or t.track_id.startswith('program-components-'))):
            delivery_issues.append({'code':'program_graphics_missing','message':'節目 Skill 需要有用途的後製畫面，請檢視素材後選擇合適元件。'})
        if self._session_value():
            from .speech_coverage import speech_coverage
            coverage=speech_coverage(self)
            if not coverage['complete']:delivery_issues.append({'code':'source_speech_evidence_missing','ranges':coverage['unknown_ranges'],
                'message':'保留的原音仍有未取證範圍，請按需轉寫或明確調整剪輯；空搜尋不能視為沒有對話。'})
            from .program_subtitles import subtitle_status
            subtitle_review=subtitle_status(self.repository.projects,self.project_id,self._timeline())
            pending=subtitle_review['unaligned_item_ids']
            if pending:delivery_issues.append({'code':'subtitle_alignment_required','item_ids':pending,
                'message':'字幕時間尚未核對，或修改文字後原對齊已失效。請核對來源並修復相關字幕；可以先看草稿。'})
            if not subtitle_review['count'] and self._production_list_transcripts({'include_drafts':True}).payload['cues']:
                delivery_issues.append({'code':'program_subtitles_missing','message':'目前保留口語但沒有節目字幕，請依來源建立並對齊字幕。'})
            from .programme_review import caption_coverage
            captions=caption_coverage(self,self._production_list_transcripts({'include_drafts':True}).payload)
            if captions['missing_cues']:
                delivery_issues.append({'code':'subtitle_coverage_incomplete','ranges':captions['missing_cues'],
                    'message':'保留口語仍有缺漏字幕；少數已對齊字幕不能代表整片完成。請修復來源辨識／對齊並重建字幕，或明確調整原音剪輯。'})
            if captions['unresolved_item_ids']:
                delivery_issues.append({'code':'subtitle_source_range_unresolved','item_ids':captions['unresolved_item_ids'],
                    'message':'部分原音切在尚未對齊的句段內，尚無完整可用字幕；請按需重新轉寫該區間，不能以刪除字幕消除待辦。'})
            from .production_media import load_text_styles
            profiles=load_text_styles(self.repository.projects,self.project_id)
            shots={i.item_id:i for i in self._video().items}
            caption_items=[i for t in self._timeline().tracks if t.track_id=='agent-subtitles' for i in t.items]
            for clip in shots.values():
                anchored=[i.item_id for i in caption_items if profiles.get(i.style_id,{}).get('anchor_item_id')==clip.item_id]
                muted=any(e.effect_id=='audio.source' and e.parameters['mode']=='mute' for e in clip.effects)
                if muted and anchored:delivery_issues.append({'code':'subtitle_muted_source','item_ids':anchored,'message':'字幕所引用的原音已靜音，請確認聲音安排並重建對應字幕。'})
                if any(e.effect_id=='video.speed_ramp' for e in clip.effects) and (not muted or anchored):
                    delivery_issues.append({'code':'retimed_speech_mapping_unavailable','item_id':clip.item_id,
                        'message':'這段變速尚無可靠的語音時間映射。請移除語音鏡頭的變速，或將它改為不帶口語字幕的靜音畫面。'})
            with self.repository.projects.connect(self.project_id) as db:
                corrections=db.execute("SELECT request_json FROM production_evidence_jobs WHERE run_id=? AND kind='align' AND state='succeeded'",(self.run_id,)).fetchall()
            changed={(value['transcript_job_id'],str(value['correction']['segment_ordinal'])) for row in corrections
                     if 'correction' in (value:=json.loads(row['request_json']))}
            stale_text=[i.item_id for t in self._timeline().tracks if t.track_id=='agent-subtitles' for i in t.items
                        if (profiles.get(i.style_id,{}).get('source_revision_id'),str(profiles.get(i.style_id,{}).get('segment_id'))) in changed]
            if stale_text:delivery_issues.append({'code':'subtitle_source_changed','item_ids':stale_text,'message':'來源口語已有更正，請使用最新證據重新建立相關字幕。'})
            revision_id=self.repository.current_revision(self.project_id,self.run_id).revision_id
            with self.repository.projects.connect(self.project_id) as db:
                rows=db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='mix_review' ORDER BY sequence DESC",(self.run_id,)).fetchall()
            from .production_preview import render_recipe_id
            recipe_id=render_recipe_id()
            mix=next((value for r in rows if (value:=json.loads(r['payload_json']))['revision_id']==revision_id and value.get('recipe_id')==recipe_id),None)
            if mix is None:delivery_issues.append({'code':'mix_review_required','message':'請在預覽完成後呼叫 inspect_mix，檢查實際配樂與混音量測。'})
            else:delivery_issues+=mix['issues']
        issues+=delivery_issues
        styles=load_text_styles(self.repository.projects,self.project_id)
        fps=self._timeline().frame_rate.numerator/self._timeline().frame_rate.denominator
        with self.repository.projects.connect(self.project_id) as db:
            for clip in self._video().items:
                row=db.execute("SELECT details_json FROM media_streams WHERE version_id=? AND stream_index=? AND kind='video'",(clip.source.span.version_id,clip.source.span.stream_index)).fetchone()
                if row and json.loads(row['details_json']).get('color_transfer') in {'smpte2084','arib-std-b67'}:
                    issues.append({'code':'hdr_preview_unsupported','item_id':clip.item_id})
        for t in self._timeline().tracks:
            for item in t.items:
                if isinstance(item,TextClip) and item.style_id.startswith('agent.text.') and item.style_id not in styles:
                    issues.append({'code':'text_style_missing','item_id':item.item_id})
                if not isinstance(item,MediaClip):continue
                for e in item.effects:
                    if e.effect_id!='audio.layers':continue
                    for layer in e.parameters['layers']:
                        try:
                            fd,asset=open_asset(self._asset_store().context(layer['asset_id']),layer['asset_id']);os.close(fd)
                            if item.start_frame+layer['start_frame']+layer['duration_frames']>self._timeline().duration_frames or layer['source_offset_seconds']+layer['duration_frames']/fps>asset['duration_seconds']:
                                issues.append({'code':'audio_out_of_bounds','layer_id':layer['layer_id']})
                        except ProjectError as error:issues.append({'code':error.code,'layer_id':layer['layer_id']})
        from .production_requirements import production_contents
        validation={'valid':not issues,'issues':issues,'render_issues':[i for i in issues if i not in delivery_issues],
                    'delivery_issues':delivery_issues,'design_notes':self._component_design_notes(),'production_contents':production_contents(self.repository.projects,self.project_id,self._timeline()),'revision_id':self.repository.current_revision(self.project_id,self.run_id).revision_id,'render_review':finish_review_status(self),'aesthetic_review':'not performed'}
        from .production_preview import _event
        _event(self,'sequence_validation',{k:validation[k] for k in ('revision_id','valid','issues','render_issues','delivery_issues')})
        return ToolResult(validation)

    def _production_get_edit_history(self,args):
        # A long run holds thousands of events; return versions and one page of
        # recent events so the result always fits a single tool response.
        from .versions import versions
        limit=args.get('limit',40);before=args.get('before_sequence')
        with self.repository.projects.connect(self.project_id) as db:
            film=versions(db,self.run_id,self.repository.get(self.project_id,self.run_id).provider)
            rows=db.execute('SELECT sequence,event_type,payload_json,created_at FROM edit_agent_events WHERE run_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
                            (self.run_id,before if before is not None else 2**62,limit+1)).fetchall()
            revisions=db.execute('SELECT COUNT(*) FROM edit_agent_revisions WHERE run_id=?',(self.run_id,)).fetchone()[0]
        page=rows[:limit]
        events=[{'sequence':r['sequence'],'event_type':r['event_type'],'created_at':r['created_at'],
                 'payload':r['payload_json'] if len(r['payload_json'])<=400 else r['payload_json'][:400]+'…'} for r in page]
        return ToolResult({'current_revision_id':self.repository.current_revision(self.project_id,self.run_id).revision_id,
            'versions':film,'revision_count':revisions,'events':events,
            'next_before_sequence':page[-1]['sequence'] if len(rows)>limit else None,
            'note':'Versions are complete edits a person can restore; events are newest first and payloads over 400 characters are shortened.'})

    def _production_read_mutation_receipt(self,args):
        with self.repository.projects.connect(self.project_id) as db:
            row=db.execute('SELECT mutation_id,tool_name,state,result_json FROM production_tool_receipts WHERE run_id=? AND mutation_id=?',(self.run_id,args['mutation_id'])).fetchone()
        if row is None:raise ProjectError('mutation_not_found','找不到這次操作的回報紀錄。',status_code=404)
        return ToolResult({'mutation_id':row['mutation_id'],'tool':row['tool_name'],'state':row['state'],
                          'result':json.loads(row['result_json']) if row['result_json'] else None,
                          'current_revision_id':self.repository.current_revision(self.project_id,self.run_id).revision_id})

    def _production_restore_revision(self,args):
        with self.repository.projects.connect(self.project_id) as db:
            row=db.execute('SELECT payload_json FROM edit_agent_revisions WHERE revision_id=? AND run_id=?',(args['revision_id'],self.run_id)).fetchone()
        if row is None:raise problem('版本不屬於這次剪輯。')
        from clipen.domain import EditTimeline
        return self._save(EditTimeline.from_payload(json.loads(row['payload_json'])),args['summary'])

    def _production_export_edit(self,args):
        from .production_media import atomic_json
        revision=self.repository.current_revision(self.project_id,self.run_id)
        assets=self._asset_store().public_assets()
        used={l['asset_id'] for t in revision.timeline.tracks for i in t.items if isinstance(i,MediaClip) for e in i.effects if e.effect_id=='audio.layers' for l in e.parameters['layers']}
        credits=[a['provenance'] for a in assets if a['asset_id'] in used]
        path=self._workdir('exports')/(revision.revision_id+'.json')
        from .production_media import load_text_styles
        from .program_story import read_episode
        from .program_subtitles import subtitle_status
        from .program_reference import reference_for
        from .production_requirements import production_contents
        styles=load_text_styles(self.repository.projects,self.project_id)
        used_styles={i.style_id for t in revision.timeline.tracks if t.kind is TrackKind.TITLE for i in t.items}
        atomic_json(path,{'timeline':revision.timeline.to_payload(),'credits':credits,'text_styles':{k:v for k,v in styles.items() if k in used_styles},
            'production_contents':production_contents(self.repository.projects,self.project_id,revision.timeline),
            **({'production_skill':self._skill_value(),'production_intent':self._session_value()['intent']} if self._session_value() else {}),
            'program_reference':reference_for(self.repository,self.project_id,self.run_id),
            'creative_direction':self.repository.creative_direction(self.project_id,self.run_id),
            'episode':read_episode(self.repository.projects,self.project_id,self.run_id,revision.timeline),
            'subtitle_review':subtitle_status(self.repository.projects,self.project_id,revision.timeline),'format':'clipen.native.v1'})
        return ToolResult({'revision_id':revision.revision_id,'export_id':revision.revision_id,'format':'clipen.native.v1','credits':credits,
                           'download_url':f'/api/projects/{self.project_id}/edit-agent/{self.run_id}/exports/{revision.revision_id}'})

    def _workdir(self,name):
        base=self.repository.projects.root/self.project_id/'agent-production'
        for p in (base,base/self.run_id,base/self.run_id/name):
            if p.is_symlink():raise problem('製作目錄不安全。')
            p.mkdir(mode=0o700,exist_ok=True)
        return base/self.run_id/name

    def _cancel_event(self):
        if not hasattr(self,'_production_cancel'):self._production_cancel=threading.Event()
        return self._production_cancel

    def _production_render_preview(self,args):
        from .production_preview import render_preview
        return render_preview(self)

    def _production_inspect_preview(self,args):
        from .production_preview import inspect_preview
        return inspect_preview(self,args)

    def _production_inspect_mix(self,args):
        from clipen.rendering import PreviewRenderRepository
        from clipen.projects.storage import utc_now
        with self.repository.projects.connect(self.project_id) as db:
            entries=[json.loads(r['payload_json']) for r in db.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='production_preview'",(self.run_id,))]
        entry=next((v for v in entries if v['render_id']==args['render_id']),None)
        if entry is None:raise problem('混音預覽不屬於這次製作。')
        store=PreviewRenderRepository(self.repository.projects)
        if store.get(self.project_id,args['render_id'])['state']!='succeeded':raise problem('請先等待預覽完成。')
        path=store.output_path(self.project_id,args['render_id'],prepare=False).with_suffix('.mix.json')
        if path.is_symlink() or not path.is_file() or path.stat().st_size>512000:
            raise ProjectError('mix_evidence_unavailable','這版預覽沒有保存分軌混音證據。',status_code=409)
        from .production_preview import render_recipe_id
        payload={**json.loads(path.read_text()),'revision_id':entry['revision_id'],'render_id':args['render_id'],'recipe_id':entry.get('recipe_id'),
                 'stale':entry['revision_id']!=self.repository.current_revision(self.project_id,self.run_id).revision_id or entry.get('recipe_id')!=render_recipe_id()}
        with self.repository.projects.connect(self.project_id) as db:
            self.repository._append_event(db,self.run_id,'mix_review',payload,utc_now())
        return ToolResult(payload)

    def _production_cancel_preview(self,args):
        from .production_preview import cancel_preview
        return cancel_preview(self,args)

    def close_production(self):
        self._cancel_event().set()
        for cancel,thread in getattr(self,'_evidence_jobs',{}).values():cancel.set()
        for cancel,thread in getattr(self,'_preview_jobs',{}).values():cancel.set()
        for cancel,thread in getattr(self,'_evidence_jobs',{}).values():thread.join(3)
        for cancel,thread in getattr(self,'_preview_jobs',{}).values():thread.join(3)
