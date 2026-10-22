"""Skill discovery and source-anchored programme graphics exposed via MCP."""
from dataclasses import dataclass, replace
import base64
import hashlib
import json
import os

from clipen.domain import TextClip, Track, TrackKind
from clipen.projects import ProjectError
from clipen.skills import get_skill, list_skills
from .agent_models import ToolResult
from .production_catalog import COMPONENT_FIELDS
from clipen.skills.visual_styles import style_presets, public_styles, font_presets, public_font_styles



@dataclass(frozen=True)
class SourceContext:
    """An actual user brief with unknown evidence, not a synthetic confirmed plan."""
    brief: str
    candidates: tuple = ()
    limitations: tuple = ("素材尚未分析的範圍是未知；請用來源工具按需取證。",)

    def candidate_map(self):
        return {candidate.candidate_id: candidate for candidate in self.candidates}


class SkillTools:
    def _session_value(self):
        if not hasattr(self, "_session_cache"):
            self._session_cache = self.repository.production_session(self.project_id, self.run_id)
        return self._session_cache

    def _skill_value(self):
        session = self._session_value()
        return session["skill"] if session else get_skill()

    def _source_rows(self):
        from .production_media import source_rows
        rows = source_rows(self.repository.projects, self.project_id)
        session = self._session_value()
        if session is None:
            return rows
        granted = {(r["version_id"], r["stream_index"]) for r in session["sources"]}
        return [r for r in rows if (r["version_id"], r["stream_index"]) in granted]

    def _production_list_production_skills(self, args):
        return ToolResult({"skills": list_skills(), "selected": self._skill_summary()})

    def _skill_summary(self):
        value = self._skill_value()
        return {**{key: value[key] for key in ("skill_id", "version", "name", "content_hash", "limitations")},
                **({'template_skill':value['selected_template_skill']} if value.get('selected_template_skill') else {})}

    def _production_read_production_skill(self, args):
        skill = self._skill_value()
        resource = args.get("resource", "SKILL.md")
        if resource in skill.get('assets',{}):
            asset=skill['assets'][resource];data=base64.b64decode(asset['data'],validate=True)
            if len(data)>512*1024 or hashlib.sha256(data).hexdigest()!=asset['sha256']:
                raise ProjectError('skill_asset_invalid','Skill 範例資產未通過校驗。',status_code=409)
            path=self._workdir('skill-assets')/(asset['sha256']+'.png')
            if path.is_symlink():raise ProjectError('storage_unsafe','Skill 資產路徑不安全。',status_code=409)
            if not path.exists():
                descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                with os.fdopen(descriptor,'wb') as output:output.write(data)
            elif hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']:
                raise ProjectError('skill_asset_invalid','Skill 範例資產內容已改變。',status_code=409)
            return ToolResult({'skill':self._skill_summary(),'resource':resource,'authority':'style_example_not_source_evidence',
                               'instruction':'Use as a visual style reference only; never copy sample speech/facts into the film.'},(path,))
        if resource not in skill["resources"]:
            raise ProjectError("skill_resource_not_found", "Skill 沒有這份資源。", status_code=404)
        content=skill['resources'][resource]
        selected=skill.get('selected_template_skill')
        applied=[]
        if selected and resource=='SKILL.md':
            # The default read must actually deliver the selected instructions,
            # even if the director has not discovered the nested resource yet.
            content+='\n\n---\n\n'+f"Selected template Skill: {selected['skill_id']} v{selected['version']}\nResource: {selected['resource']}\n\n"+skill['resources'][selected['resource']]
            applied=[selected]
        return ToolResult({"skill": self._skill_summary(), "resource": resource,
                           "content": content, 'applied_template_skills':applied,
                           "available_resources": list(skill["resources"])+list(skill.get('assets',{}))})

    def _production_list_program_components(self, args):
        skill = self._skill_value()
        return ToolResult({"skill": self._skill_summary(), "components": skill["components"],
                           "presets": font_presets(skill,style_presets(skill)[1])[1], "visual_styles":public_styles(skill),
                           "font_styles":public_font_styles(skill), "selected_font_style_id":skill.get("selected_font_style_id","default"),
                           "selected_visual_style_id":skill.get("selected_visual_style_id","variety"), "parameters": COMPONENT_FIELDS,
                           "tracking": "static_target_only", "independent_title_cards": False})

    def _production_add_program_component(self, args):
        return self._write_component(args, updating=False)

    def _production_update_program_component(self, args):
        return self._write_component(args, updating=True)

    def _write_component(self, args, *, updating):
        from clipen.finishing.models import digest
        from .production_media import atomic_json, load_text_styles, text_style_path
        timeline = self._timeline()
        shot = next((i for i in self._video().items if i.item_id == args["item_id"]), None)
        if shot is None:
            raise ProjectError("component_anchor_missing", "畫面元件需要有效的鏡頭錨點。", status_code=422)
        current = next((i for t in timeline.tracks for i in t.items if i.item_id == args["element_id"]), None)
        if (updating and (not isinstance(current, TextClip) or not current.style_id.startswith("agent.component."))) or (not updating and current is not None):
            raise ProjectError("component_identity_conflict", "元件識別已存在，或找不到要更新的元件。", status_code=409)
        skill = self._skill_value()
        component = next((c for c in skill["components"] if c["component_id"] == args["component_id"]), None)
        if component is None:
            raise ProjectError("component_unavailable", "此製作工作凍結的 Skill 沒有這種元件，請另開新版製作工作。", status_code=422)
        if len(args["text"]) > component["max_characters"] or not args["text"].strip():
            raise ProjectError("component_text_overflow", "請縮短這個元件的文字。", status_code=422)
        if '\n' in args['text'] or '\n' in args.get('detail',''):
            raise ProjectError('component_text_overflow','主標與補充資訊各使用一行，換行由元件處理。',status_code=422)
        if args["start_offset_frames"] + args["duration_frames"] > shot.duration_frames:
            raise ProjectError("component_time_out_of_bounds", "元件必須位於錨定鏡頭內。", status_code=422)
        if component["requires_target"] and "target" not in args:
            raise ProjectError("component_target_required", "指示元件需要指定目標位置。", status_code=422)
        from .component_review import (REACTION_COMPONENTS, REACTION_MAX_SECONDS, RESTATE_RATIO,
                                       concurrent_subtitles, fact_has_information, restated_ratio)
        # Editorial rules steer the model; a user's own workbench edit stays in their control.
        model_edit = not getattr(self, "_user_edit", False)
        if model_edit and not updating and not {"x", "y"} <= set(args):
            raise ProjectError("component_position_required",
                               "請先看這個畫面，再依人物、物件與空白處指定 x、y；不使用預設位置。", status_code=422)
        if model_edit and component["component_id"] == "fact" and not fact_has_information(args["text"], args.get("detail", "")):
            raise ProjectError("component_fact_without_information",
                               "fact 需要畫面看不出的具體資訊：在 detail 寫有來源的品名、價格或數字，否則不要放。", status_code=422)
        fps = timeline.frame_rate.numerator/timeline.frame_rate.denominator
        if model_edit and component["component_id"] in REACTION_COMPONENTS and args["duration_frames"] > REACTION_MAX_SECONDS*fps:
            raise ProjectError("component_reaction_too_long",
                               f"反應類元件請對準反應那一刻，顯示 {REACTION_MAX_SECONDS} 秒以內。", status_code=422)
        start_frame = shot.start_frame+args["start_offset_frames"]
        if model_edit and restated_ratio(args["text"], concurrent_subtitles(timeline, start_frame, args["duration_frames"])) >= RESTATE_RATIO:
            raise ProjectError("component_restates_subtitle",
                               "這段文字和同時出現的字幕重複。請補充畫面與字幕都沒說的資訊或觀點，否則不要放。", status_code=422)
        previous_style=load_text_styles(self.repository.projects,self.project_id).get(current.style_id,{}) if updating else {}
        visual_style,presets=style_presets(skill,args.get('visual_style_id',previous_style.get('visual_style_id')))
        font_style,presets=font_presets(skill,presets,args.get('font_style_id',previous_style.get('font_style_id')))
        defaults = presets.get('component_positions', {}).get(component['component_id'], component['default_position'])
        position = {axis: args.get(axis, previous_style.get(axis, defaults[axis])) for axis in ('x', 'y')}
        if not .055 <= position["x"] <= .65 or not .055 <= position["y"] <= .68:
            raise ProjectError("component_unsafe_position", "元件需留在安全區域，並避開底部字幕。", status_code=422)
        if "target" in args:
            target = args["target"]
            if not all(target["radius"] <= target[axis] <= 1-target["radius"] for axis in ("x", "y")):
                raise ProjectError("component_unsafe_target", "目標圈選範圍超出畫面。", status_code=422)
        profile = {"kind": "program_component", "component_id": component["component_id"],
                   "skill_id": skill["skill_id"], "skill_version": skill["version"], "skill_hash": skill["content_hash"],
                   "presets": presets, "visual_style_id":visual_style, "font_style_id":font_style, "eyebrow":component.get('eyebrow',''),"anchor_item_id": shot.item_id,
                   "start_offset_frames": args["start_offset_frames"], "evidence": args["evidence"],
                   **position, **({"target": args["target"]} if "target" in args else {}),
                   **({} if model_edit else {"authority": "user"})}
        style_id = "agent.component." + digest(profile)[:24]
        text=args['text']+('\n'+args['detail'] if args.get('detail') else '')
        item = TextClip(args["element_id"], shot.start_frame+args["start_offset_frames"], args["duration_frames"], text, style_id)
        styles = load_text_styles(self.repository.projects, self.project_id)
        styles[style_id] = profile
        # Fail early on actual typography overflow, before publishing a revision.
        from PIL import Image, ImageDraw
        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.titles import TitleCardRenderer
        font = TitleCardRenderer._font_path()
        if font is None:
            raise ProjectError("preview_title_font_missing", "缺少可用的中文字型。", status_code=409)
        from clipen.rendering.titles import TitleRenderError
        try:
            draw_program_component(ImageDraw.Draw(Image.new("RGBA", (timeline.width, timeline.height))), font, timeline, item, profile, 8)
        except TitleRenderError as error:
            message='缺少這套字型或字型檔校驗失敗，請重新安裝對應版本。' if error.code=='program_font_unavailable' else '目前字型無法顯示部分字元，請使用可用字型或等價標點。' if error.code=='program_component_missing_glyph' else '文字超出元件版面，請縮短文字或調整位置。'
            raise ProjectError(error.code,message,status_code=422) from error
        atomic_json(text_style_path(self.repository.projects, self.project_id), styles)
        tracks = tuple(replace(t, items=tuple(i for i in t.items if i.item_id != item.item_id)) for t in timeline.tracks if not (t.track_id == "program-components" or t.track_id.startswith("program-components-")))
        previous = [i for t in timeline.tracks if (t.track_id == "program-components" or t.track_id.startswith("program-components-")) for i in t.items if i.item_id != item.item_id]
        from .production_limits import limits_for
        if len(previous) >= limits_for(self).components:
            raise ProjectError("component_limit", "已達本次製作的畫面元件上限。", status_code=422)
        # Separate overlay lanes preserve the domain rule: clips on one track
        # cannot overlap. Spatial collisions remain delivery findings below.
        lanes = []
        for element in sorted(previous+[item], key=lambda i: (i.start_frame, i.item_id)):
            lane = next((lane for lane in lanes if lane[-1].start_frame+lane[-1].duration_frames <= element.start_frame), None)
            if lane is None:
                lane = []
                lanes.append(lane)
            lane.append(element)
        tracks += tuple(Track("program-components" if n == 0 else f"program-components-{n+1}",
                              TrackKind.TITLE, tuple(lane)) for n, lane in enumerate(lanes))
        return self._save(replace(timeline, tracks=tracks), args["summary"], element_id=item.item_id,
                          anchor_item_id=shot.item_id, inspection_required="Check actual target, faces, timing and usefulness in preview.")

    def _component_issues(self):
        from PIL import Image, ImageDraw
        from clipen.rendering.program_components import component_bounds
        from clipen.rendering.titles import TitleCardRenderer, TitleRenderError
        from .production_media import load_text_styles
        timeline=self._timeline();styles=load_text_styles(self.repository.projects,self.project_id)
        shots={i.item_id:i for i in self._video().items}
        elements=[i for t in timeline.tracks if (t.track_id=='program-components' or t.track_id.startswith('program-components-')) for i in t.items]
        font=TitleCardRenderer._font_path();issues=[];boxes=[]
        for element in elements:
            profile=styles.get(element.style_id,{})
            anchor=shots.get(profile.get('anchor_item_id'))
            if profile.get('kind')!='program_component' or anchor is None:
                issues.append({'code':'component_anchor_missing','item_id':element.item_id});continue
            if element.start_frame<anchor.start_frame or element.start_frame+element.duration_frames>anchor.start_frame+anchor.duration_frames:
                issues.append({'code':'component_anchor_out_of_bounds','item_id':element.item_id});continue
            if font is None:
                issues.append({'code':'preview_title_font_missing','item_id':element.item_id});continue
            try:
                box,_,_=component_bounds(ImageDraw.Draw(Image.new('RGBA',(1,1))),font,timeline,element,profile)
                boxes.append((element,box))
            except TitleRenderError as error:issues.append({'code':error.code,'item_id':element.item_id})
            # Subtitles may be rebuilt after a graphic was placed; recheck the pair here.
            from .component_review import RESTATE_RATIO,concurrent_subtitles,restated_ratio
            if profile.get('authority')!='user' and restated_ratio(element.text.partition('\n')[0],concurrent_subtitles(timeline,element.start_frame,element.duration_frames))>=RESTATE_RATIO:
                issues.append({'code':'component_restates_subtitle','item_id':element.item_id,
                               'message':'元件文字和同時出現的字幕重複，請改寫成補充資訊或移除。'})
        for n,(a,left) in enumerate(boxes):
            for b,right in boxes[n+1:]:
                if (a.start_frame<b.start_frame+b.duration_frames and b.start_frame<a.start_frame+a.duration_frames
                    and left[0]<right[2] and right[0]<left[2] and left[1]<right[3] and right[1]<left[3]):
                    issues.append({'code':'program_component_collision','item_ids':[a.item_id,b.item_id]})
        return issues

    def _component_design_notes(self):
        from .component_review import design_notes
        from .production_media import load_text_styles
        timeline=self._timeline()
        elements=[i for t in timeline.tracks if (t.track_id=='program-components' or t.track_id.startswith('program-components-')) for i in t.items]
        return design_notes(timeline,elements,load_text_styles(self.repository.projects,self.project_id))

    def _production_restyle_program_components(self, args):
        from clipen.finishing.models import digest
        from .production_media import atomic_json,load_text_styles,text_style_path
        from PIL import Image,ImageDraw
        from clipen.rendering.program_components import component_bounds
        from clipen.rendering.titles import TitleCardRenderer,TitleRenderError
        timeline=self._timeline();styles=load_text_styles(self.repository.projects,self.project_id)
        identifier,presets=style_presets(self._skill_value(),args['visual_style_id'])
        eligible={i.item_id:i for t in timeline.tracks for i in t.items
                  if isinstance(i,TextClip) and styles.get(i.style_id,{}).get('kind')=='program_component'}
        targets=set(args.get('element_ids',eligible))
        if not targets or not targets<=eligible.keys():
            raise ProjectError('component_not_found','找不到要更換風格的畫面元件。',status_code=422)
        font=TitleCardRenderer._font_path()
        if font is None:raise ProjectError('preview_title_font_missing','缺少可用的中文字型。',status_code=409)
        replacements={}
        for key in targets:
            item=eligible[key]
            font_style,resolved=font_presets(self._skill_value(),presets,args.get('font_style_id',styles[item.style_id].get('font_style_id')))
            profile={**styles[item.style_id],'presets':resolved,'visual_style_id':identifier,'font_style_id':font_style}
            try:component_bounds(ImageDraw.Draw(Image.new('RGBA',(1,1))),font,timeline,item,profile)
            except TitleRenderError as error:
                message='缺少這套字型或字型檔校驗失敗，請重新安裝對應版本。' if error.code=='program_font_unavailable' else '這套風格的字型無法顯示部分字元，請更換風格或使用等價標點。' if error.code=='program_component_missing_glyph' else '這套風格的文字超出版面，請先縮短文字或調整位置。'
                raise ProjectError(error.code,message,status_code=422) from error
            style_id='agent.component.'+digest(profile)[:24];styles[style_id]=profile
            replacements[key]=replace(item,style_id=style_id)
        # Publish nothing until every replacement passes actual font/layout checks.
        atomic_json(text_style_path(self.repository.projects,self.project_id),styles)
        tracks=tuple(replace(t,items=tuple(replacements.get(i.item_id,i) for i in t.items)) for t in timeline.tracks)
        return self._save(replace(timeline,tracks=tracks),args['summary'],visual_style_id=identifier,
                          changed_element_ids=sorted(targets),inspection_required='Render and inspect this exact revision; font changes can affect composition.')

    def _production_remove_program_component(self, args):
        timeline = self._timeline()
        if not any(i.item_id == args["element_id"] for t in timeline.tracks if (t.track_id == "program-components" or t.track_id.startswith("program-components-")) for i in t.items):
            raise ProjectError("component_not_found", "找不到這個畫面元件。", status_code=404)
        return self._save(replace(timeline, tracks=tuple(replace(t, items=tuple(i for i in t.items if i.item_id != args["element_id"]))
                                                       for t in timeline.tracks)), args["summary"])
