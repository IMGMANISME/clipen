"""Skill discovery and source-anchored programme graphics exposed via MCP."""
from dataclasses import dataclass, replace
import base64
import hashlib
import os

from clipen.domain import TextClip, Track, TrackKind
from clipen.projects import ProjectError
from clipen.skills import get_skill, list_skills
from .agent_models import ToolResult
from .production_catalog import COMPONENT_FIELDS
from clipen.skills.visual_styles import style_presets, public_styles, font_presets, public_font_styles

# The colours a person may change on a graphic: its accent, its light and its dark colour.
COMPONENT_COLORS = frozenset({"accent", "paper", "ink"})



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
                **({'core':value['core'],'skill_chain':value['skill_chain']} if value.get('skill_chain') else {}),
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
        # The default read must actually deliver the selected instructions,
        # even if the director has not discovered the nested resources yet.
        # Sessions frozen before the core/skill split carry one selected skill.
        applied=skill.get('skill_chain') or ([skill['selected_template_skill']] if skill.get('selected_template_skill') else [])
        if resource=='SKILL.md':
            for selected in applied:
                kind={'custom':'custom'}.get(selected.get('source'),'template' if 'skill_chain' not in skill else 'built-in')
                content+='\n\n---\n\n'+f"Selected {kind} Skill: {selected['skill_id']} v{selected['version']}\nResource: {selected['resource']}\n\n"+skill['resources'][selected['resource']]
        else:
            applied=[]
        return ToolResult({"skill": self._skill_summary(), "resource": resource,
                           "content": content, 'applied_template_skills':applied,
                           "available_resources": list(skill["resources"])+list(skill.get('assets',{}))})

    def _production_list_program_components(self, args):
        """How graphics are made now, and the ones already in the film."""
        from .production_catalog import CUSTOM_COMPONENT_HELP
        from .production_media import load_text_styles
        styles = load_text_styles(self.repository.projects, self.project_id)
        timeline = self._timeline()
        existing = [{"element_id": item.item_id, "start_frame": item.start_frame, "duration_frames": item.duration_frames,
                     "text": item.text, "designed_freely": styles.get(item.style_id, {}).get("component_id") == "custom"}
                    for track in timeline.tracks if track.track_id == "program-components" or track.track_id.startswith("program-components-")
                    for item in track.items]
        return ToolResult({"skill": self._skill_summary(), "how": "free_design_only", "tool": "add_custom_component",
                           "guidance": CUSTOM_COMPONENT_HELP,
                           "canvas": {"width": timeline.width, "height": timeline.height},
                           "existing_graphics": existing,
                           "older_graphics": "Graphics not designed_freely use retired built-in templates. Keep them, remove them with remove_program_component, "
                                             "or replace them: remove, then design the new one with add_custom_component."})

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
        if updating and load_text_styles(self.repository.projects, self.project_id).get(current.style_id, {}).get("component_id") == "custom":
            raise ProjectError("component_identity_conflict", "這是自由設計元件，請用 update_custom_component 修改。", status_code=409)
        skill = self._skill_value()
        component = next((c for c in skill["components"] if c["component_id"] == args["component_id"]), None)
        if component is None:
            allowed = (skill.get("custom_skill") or {}).get("binding", {}).get("components")
            if allowed:
                # The selected custom Skill narrows the vocabulary; this is enforced, not advisory.
                raise ProjectError("component_not_allowed", "這套 Skill 只允許使用這些元件：" + "、".join(allowed) + "。", status_code=422)
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
        # Content judgement belongs to the Agent; these are advice, returned with the result.
        notes = []
        if not updating and not {"x", "y"} <= set(args):
            notes.append("沒有指定 x、y，用了這種元件的預設位置；請看預覽確認沒有擋到人物或重點。")
        if component["component_id"] == "fact" and not fact_has_information(args["text"], args.get("detail", "")):
            notes.append("fact 通常帶有畫面看不出的具體資訊（品名、價格、數字）。")
        fps = timeline.frame_rate.numerator/timeline.frame_rate.denominator
        if component["component_id"] in REACTION_COMPONENTS and args["duration_frames"] > REACTION_MAX_SECONDS*fps:
            notes.append(f"反應類元件通常對準反應那一刻，{REACTION_MAX_SECONDS} 秒以內比較有力；確定要停久一點就保留。")
        start_frame = shot.start_frame+args["start_offset_frames"]
        if restated_ratio(args["text"], concurrent_subtitles(timeline, start_frame, args["duration_frames"])) >= RESTATE_RATIO:
            notes.append("這段文字和同時出現的字幕大部分重複；如果不是刻意強調，可以改寫成補充資訊。")
        previous_style=load_text_styles(self.repository.projects,self.project_id).get(current.style_id,{}) if updating else {}
        visual_style,presets=style_presets(skill,args.get('visual_style_id',previous_style.get('visual_style_id')))
        font_style,presets=font_presets(skill,presets,args.get('font_style_id',previous_style.get('font_style_id')))
        defaults = presets.get('component_positions', {}).get(component['component_id'], component['default_position'])
        position = {axis: args.get(axis, previous_style.get(axis, defaults[axis])) for axis in ('x', 'y')}
        if not 0 <= position["x"] < 1 or not 0 <= position["y"] < 1:
            raise ProjectError("component_unsafe_position", "元件位置要在畫面內。", status_code=422)
        if not .055 <= position["x"] <= .65 or not .055 <= position["y"] <= .68:
            notes.append("元件靠近畫面邊緣或底部字幕區；請看預覽確認沒有被切到或擋住字幕。")
        if "target" in args:
            target = args["target"]
            if not all(target["radius"] <= target[axis] <= 1-target["radius"] for axis in ("x", "y")):
                raise ProjectError("component_unsafe_target", "目標圈選範圍超出畫面。", status_code=422)
        profile = {"kind": "program_component", "component_id": component["component_id"],
                   "skill_id": skill["skill_id"], "skill_version": skill["version"], "skill_hash": skill["content_hash"],
                   "presets": presets, "visual_style_id":visual_style, "font_style_id":font_style, "eyebrow":component.get('eyebrow',''),"anchor_item_id": shot.item_id,
                   "start_offset_frames": args["start_offset_frames"], "evidence": args["evidence"],
                   **position, **({"target": args["target"]} if "target" in args else {})}
        style_id = "agent.component." + digest(profile)[:24]
        text=args['text']+('\n'+args['detail'] if args.get('detail') else '')
        item = TextClip(args["element_id"], shot.start_frame+args["start_offset_frames"], args["duration_frames"], text, style_id)
        styles = load_text_styles(self.repository.projects, self.project_id)
        styles[item.style_id] = profile
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
        previous = [i for t in timeline.tracks if (t.track_id == "program-components" or t.track_id.startswith("program-components-")) for i in t.items if i.item_id != item.item_id]
        from .production_limits import limits_for
        if not updating and len(previous) >= limits_for(self).components:
            notes.append(f"畫面元件已經有 {len(previous)+1} 個，超過這類作品建議的 {limits_for(self).components} 個；請確認每一個都有必要。")
        # Separate overlay lanes preserve the domain rule: clips on one track
        # cannot overlap. Spatial collisions remain delivery findings below.
        tracks = self._component_lanes(item)
        return self._save(replace(timeline, tracks=tracks), args["summary"], element_id=item.item_id,
                          anchor_item_id=shot.item_id, inspection_required="Check actual target, faces, timing and usefulness in preview.",
                          **({"notes": notes} if notes else {}))

    def _component_draft(self, args):
        """A programme graphic as a person's changes would leave it, checked but not saved.

        args holds element_id plus any of text, detail, x, y, scale, colors ({accent, paper, ink}
        as #rrggbb), visual_style_id, font_style_id, start_offset_frames and duration_frames;
        anything left out keeps its current value.
        Returns the changed clip, its style profile and the clip as it is now."""
        from clipen.finishing.models import digest
        from .production_media import load_text_styles
        if not isinstance(args, dict) or not isinstance(args.get("element_id"), str):
            raise ProjectError("agent_input_invalid", "元件設定格式不正確。", status_code=422)
        timeline = self._timeline()
        item = next((i for t in timeline.tracks if t.track_id == "program-components" or t.track_id.startswith("program-components-")
                     for i in t.items if i.item_id == args["element_id"]), None)
        profile = load_text_styles(self.repository.projects, self.project_id).get(item.style_id, {}) if isinstance(item, TextClip) else {}
        if profile.get("kind") != "program_component":
            raise ProjectError("component_not_found", "找不到這個畫面元件，影片可能已經更新，請重新讀取。", status_code=404)
        if profile.get("component_id") == "custom":
            return self._custom_draft(args, timeline, item, profile)
        saved_text, _, saved_detail = item.text.partition("\n")
        text, detail = args.get("text", saved_text), args.get("detail", saved_detail)
        if not isinstance(text, str) or not isinstance(detail, str):
            raise ProjectError("agent_input_invalid", "元件文字格式不正確。", status_code=422)
        text, detail = text.strip(), detail.strip()
        if not text:
            raise ProjectError("component_text_empty", "元件的主要文字不能空白。", status_code=422)
        if "\n" in text or "\n" in detail:
            raise ProjectError("component_text_overflow", "主要文字與補充文字各寫一行，換行由元件處理。", status_code=422)
        skill = self._skill_value()
        component = next((c for c in skill["components"] if c["component_id"] == profile.get("component_id")), None)
        limit = component["max_characters"] if component else 48
        if len(text) > limit or len(detail) > 48:
            raise ProjectError("component_text_overflow", f"這個元件的主要文字最多 {limit} 個字，補充文字最多 48 個字。", status_code=422)
        def number(key, low, high):
            value = args.get(key, profile.get(key))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ProjectError("component_unsafe_position", "元件要留在畫面裡。", status_code=422)
            return round(float(value), 4)
        x, y = number("x", 0, .95), number("y", 0, .95)
        shot = next((i for i in self._video().items if i.item_id == profile.get("anchor_item_id")), None)
        if shot is None:
            raise ProjectError("component_anchor_missing", "這個元件所在的鏡頭已經不在影片裡。", status_code=409)
        start = args.get("start_offset_frames", item.start_frame - shot.start_frame)
        duration = args.get("duration_frames", item.duration_frames)
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, duration)) or start < 0 or duration < 6 or start + duration > shot.duration_frames:
            raise ProjectError("component_time_out_of_bounds", "元件要待在同一個鏡頭裡，至少顯示 0.2 秒。", status_code=422)
        visual_style, presets = style_presets(skill, args.get("visual_style_id", profile.get("visual_style_id")))
        font_style, presets = font_presets(skill, presets, args.get("font_style_id", profile.get("font_style_id")))
        scale = args.get("scale", profile.get("scale", 1))
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not .6 <= scale <= 1.6:
            raise ProjectError("component_scale_invalid", "元件大小要在 60% 到 160% 之間。", status_code=422)
        colors = args.get("colors", profile.get("colors", {}))
        if not isinstance(colors, dict) or not set(colors) <= COMPONENT_COLORS or not all(
                isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:]) for v in colors.values()):
            raise ProjectError("component_color_invalid", "顏色格式不正確。", status_code=422)
        # A person's colours replace the style's, keeping each colour's own transparency.
        for name, value in colors.items():
            presets["palette"][name] = [int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16), presets["palette"][name][3]]
        changed_profile = {key: value for key, value in profile.items() if key not in {"scale", "colors"}}
        changed_profile.update({"presets": presets, "visual_style_id": visual_style, "font_style_id": font_style,
                                "x": x, "y": y, "start_offset_frames": start})
        # Designed size and the style's colours are left out, so untouched graphics keep their identity.
        if round(float(scale), 2) != 1:
            changed_profile["scale"] = round(float(scale), 2)
        if colors:
            changed_profile["colors"] = {name: value.lower() for name, value in sorted(colors.items())}
        style_id = "agent.component." + digest(changed_profile)[:24]
        draft = replace(item, text=text + ("\n" + detail if detail else ""), start_frame=shot.start_frame + start,
                        duration_frames=duration, style_id=style_id)
        from PIL import Image, ImageDraw
        from clipen.rendering.program_components import draw_program_component
        from clipen.rendering.titles import TitleCardRenderer, TitleRenderError
        font = TitleCardRenderer._font_path()
        if font is None:
            raise ProjectError("preview_title_font_missing", "缺少可用的中文字型。", status_code=409)
        try:
            draw_program_component(ImageDraw.Draw(Image.new("RGBA", (timeline.width, timeline.height))), font, timeline, draft, changed_profile, 8)
        except TitleRenderError as error:
            message = ("這套字型無法顯示部分字元，請換個寫法或換字體。" if error.code == "program_component_missing_glyph"
                       else "缺少這套字型，請換一套字體。" if error.code == "program_font_unavailable"
                       else "元件超出畫面了，請縮小一點、縮短文字或往左上移。")
            raise ProjectError(error.code, message, status_code=422) from error
        return draft, changed_profile, item

    def _custom_draft(self, args, timeline, item, profile):
        """A free design as a person's move or retime would leave it: x, y shift the whole page."""
        from clipen.finishing.models import digest
        from clipen.rendering import custom_components
        def shift(key):
            value = args.get(key, profile.get(key, 0))
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not -1 < value < 1:
                raise ProjectError("component_unsafe_position", "元件要留在畫面裡。", status_code=422)
            return round(float(value), 4)
        shot = next((i for i in self._video().items if i.item_id == profile.get("anchor_item_id")), None)
        if shot is None:
            raise ProjectError("component_anchor_missing", "這個元件所在的鏡頭已經不在影片裡。", status_code=409)
        start = args.get("start_offset_frames", item.start_frame - shot.start_frame)
        duration = args.get("duration_frames", item.duration_frames)
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, duration)) or start < 0 or duration < 6 or start + duration > shot.duration_frames:
            raise ProjectError("component_time_out_of_bounds", "元件要待在同一個鏡頭裡，至少顯示 0.2 秒。", status_code=422)
        changed = {**profile, "x": shift("x"), "y": shift("y"), "start_offset_frames": start}
        fields = profile.get("fields") or {"texts": [], "colors": []}
        edits = {kind: dict((profile.get("edits") or {}).get(kind, {})) for kind in ("texts", "colors")}
        for kind, check_value, problem in (
                ("texts", lambda v: isinstance(v, str) and v.strip() and len(v) <= 120, "文字不能空白，最多 120 個字。"),
                ("colors", lambda v: isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:]), "顏色格式不正確。")):
            given = args.get(kind)
            if given is None:
                continue
            designed = {f["key"]: f["value"] for f in fields[kind]}
            if not isinstance(given, dict) or not set(given) <= set(designed) or not all(check_value(v) for v in given.values()):
                raise ProjectError("component_field_invalid", problem if isinstance(given, dict) and set(given) <= set(designed) else "這個元件沒有這個可編輯欄位。", status_code=422)
            # Only what differs from the design is kept, so an untouched field follows a redesign.
            edits[kind] = {k: (v.strip() if kind == "texts" else v.lower()) for k, v in given.items()
                           if (v.strip() if kind == "texts" else v.lower()) != designed[k]}
        changed.pop("edits", None)
        if any(edits.values()):
            changed["edits"] = {kind: values for kind, values in edits.items() if values}
        draft = replace(item, start_frame=shot.start_frame + start, duration_frames=duration,
                        style_id="agent.component." + digest(changed)[:24])
        try:
            custom_components.check(timeline, draft, changed)
        except custom_components.CustomRenderError as error:
            raise ProjectError(error.code, _custom_problem(error), status_code=422) from error
        return draft, changed, item

    def _edit_component(self, args):
        """A person changes one programme graphic: wording, place, look, typeface or timing."""
        from .production_media import atomic_json, load_text_styles, text_style_path
        draft, profile, item = self._component_draft(args)
        if (draft.text, draft.start_frame, draft.duration_frames, draft.style_id) == (item.text, item.start_frame, item.duration_frames, item.style_id):
            raise ProjectError("component_unchanged", "沒有任何變動。", status_code=422)
        styles = load_text_styles(self.repository.projects, self.project_id)
        styles[draft.style_id] = profile
        atomic_json(text_style_path(self.repository.projects, self.project_id), styles)
        return self._save(replace(self._timeline(), tracks=self._component_lanes(draft)), args.get("summary") or "使用者修改元件")

    def _component_lanes(self, item):
        """The timeline's tracks with item placed: graphics that overlap in time sit on separate lanes."""
        timeline = self._timeline()
        components = lambda track: track.track_id == "program-components" or track.track_id.startswith("program-components-")
        tracks = tuple(t for t in timeline.tracks if not components(t))
        previous = [i for t in timeline.tracks if components(t) for i in t.items if i.item_id != item.item_id]
        lanes = []
        for element in sorted(previous+[item], key=lambda i: (i.start_frame, i.item_id)):
            lane = next((lane for lane in lanes if lane[-1].start_frame+lane[-1].duration_frames <= element.start_frame), None)
            if lane is None:
                lane = []
                lanes.append(lane)
            lane.append(element)
        return tracks + tuple(Track("program-components" if n == 0 else f"program-components-{n+1}", TrackKind.TITLE, tuple(lane))
                              for n, lane in enumerate(lanes))

    def _component_editing(self):
        """What the review page needs to edit graphics: each one's settings and the run's styles."""
        from .production_media import load_text_styles
        skill = self._skill_value()
        styles = load_text_styles(self.repository.projects, self.project_id)
        shots = {i.item_id: i for i in self._video().items}
        limits = {c["component_id"]: c["max_characters"] for c in skill["components"]}
        names = {c["component_id"]: c.get("name", c["component_id"]) for c in skill["components"]}
        result = {}
        for track in self._timeline().tracks:
            if not (track.track_id == "program-components" or track.track_id.startswith("program-components-")):
                continue
            for item in track.items:
                profile = styles.get(item.style_id, {})
                shot = shots.get(profile.get("anchor_item_id"))
                if profile.get("kind") != "program_component" or shot is None:
                    continue
                custom = profile["component_id"] == "custom"
                result[item.item_id] = {
                    "component_id": profile["component_id"], "custom": custom,
                    "name": item.text if custom else names.get(profile["component_id"], profile["component_id"]),
                    "max_characters": limits.get(profile["component_id"], 48), "x": profile.get("x"), "y": profile.get("y"),
                    "visual_style_id": profile.get("visual_style_id"), "font_style_id": profile.get("font_style_id"),
                    "scale": profile.get("scale", 1), "colors": profile.get("colors", {}),
                    **({"fields": profile.get("fields") or {"texts": [], "colors": []}, "edits": profile.get("edits") or {}} if custom else {}),
                    "shot_start_frame": shot.start_frame, "shot_duration_frames": shot.duration_frames}
        def palette(identifier):
            colours = style_presets(skill, identifier)[1]["palette"]
            return {name: "#%02x%02x%02x" % tuple(colours[name][:3]) for name in sorted(COMPONENT_COLORS)}
        return {"components": result, "visual_styles": [{"id": v["visual_style_id"], "name": v["name"], "colors": palette(v["visual_style_id"])} for v in public_styles(skill)],
                "font_styles": [{"id": f["font_style_id"], "name": f["name"]} for f in public_font_styles(skill)]}

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
            if profile.get('authority')!='user' and profile.get('component_id')!='custom' and restated_ratio(element.text.partition('\n')[0],concurrent_subtitles(timeline,element.start_frame,element.duration_frames))>=RESTATE_RATIO:
                issues.append({'code':'component_restates_subtitle','item_id':element.item_id,'advisory':True,
                               'message':'元件文字和同時出現的字幕大部分重複；不是刻意強調的話可以改寫。'})
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
                  if isinstance(i,TextClip) and styles.get(i.style_id,{}).get('kind')=='program_component'
                  and styles[i.style_id].get('component_id')!='custom'}
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

    def _production_add_custom_component(self, args):
        return self._write_custom_component(args, updating=False)

    def _production_update_custom_component(self, args):
        return self._write_custom_component(args, updating=True)

    def _write_custom_component(self, args, *, updating):
        """A graphic the Agent designs itself as a web page; only technical checks apply."""
        from clipen.finishing.models import digest
        from clipen.rendering import custom_components
        from .production_media import atomic_json, load_text_styles, text_style_path
        timeline = self._timeline()
        styles = load_text_styles(self.repository.projects, self.project_id)
        current = next((i for t in timeline.tracks for i in t.items if i.item_id == args["element_id"]), None)
        previous = styles.get(current.style_id, {}) if isinstance(current, TextClip) else {}
        if updating and not custom_components.is_custom(previous):
            raise ProjectError("component_not_found", "找不到要更新的自由元件；內建元件請用 update_program_component。", status_code=404)
        if not updating and current is not None:
            raise ProjectError("component_identity_conflict", "這個 element_id 已經被使用。", status_code=409)
        settings = {**({"item_id": previous["anchor_item_id"], "start_offset_frames": previous["start_offset_frames"],
                        "duration_frames": current.duration_frames, "name": current.text, "html": previous["html"],
                        "evidence": previous.get("evidence", "")} if updating else {}), **args}
        shot = next((i for i in self._video().items if i.item_id == settings["item_id"]), None)
        if shot is None:
            raise ProjectError("component_anchor_missing", "畫面元件需要有效的鏡頭錨點。", status_code=422)
        if settings["start_offset_frames"] + settings["duration_frames"] > shot.duration_frames:
            raise ProjectError("component_time_out_of_bounds", f"元件必須位於錨定鏡頭內（這個鏡頭長 {shot.duration_frames} 格）。", status_code=422)
        skill = self._skill_value()
        profile = {"kind": "program_component", "component_id": custom_components.CUSTOM,
                   "skill_id": skill["skill_id"], "skill_version": skill["version"], "skill_hash": skill["content_hash"],
                   "html": settings["html"], "anchor_item_id": shot.item_id, "start_offset_frames": settings["start_offset_frames"],
                   "evidence": settings.get("evidence", ""), "x": previous.get("x", 0) if updating else 0, "y": previous.get("y", 0) if updating else 0}
        item = TextClip(args["element_id"], shot.start_frame + settings["start_offset_frames"], settings["duration_frames"], settings["name"].strip() or "自由元件", "pending")
        try:
            profile["fields"] = custom_components.fields(timeline, item, profile)
            # A person's word and colour changes survive a redesign wherever the new page keeps that field.
            keep = {kind: {k: v for k, v in (previous.get("edits") or {}).get(kind, {}).items() if k in {f["key"] for f in profile["fields"][kind]}}
                    for kind in ("texts", "colors")} if updating else {}
            if any(keep.values()):
                profile["edits"] = {kind: values for kind, values in keep.items() if values}
            item = replace(item, style_id="agent.component." + digest(profile)[:24])
            custom_components.check(timeline, item, profile)
        except custom_components.CustomRenderError as error:
            raise ProjectError(error.code, _custom_problem(error), status_code=422) from error
        styles[item.style_id] = profile
        atomic_json(text_style_path(self.repository.projects, self.project_id), styles)
        editable = {kind: [f["label"] for f in profile["fields"][kind]] for kind in ("texts", "colors")}
        result = self._save(replace(timeline, tracks=self._component_lanes(item)), args["summary"], element_id=item.item_id,
                            anchor_item_id=shot.item_id, editable_by_people=editable,
                            inspection_required="Look at the still: legibility, faces, subtitles, and whether it serves this moment.",
                            **({} if any(editable.values()) else {"notes": ["沒有標出可編輯的文字或顏色，使用者只能請你重做。用 data-clipen-text 與 --clipen-* 顏色變數標出主要文字和顏色。"]}))
        return ToolResult(result.payload, self._custom_still(result.payload["revision"]["revision_id"], item))

    def _custom_still(self, revision_id, item):
        """The graphic over the actual footage at its resting frame, for the Agent to look at."""
        import base64
        from .visual_preview import frame
        path = self._workdir("custom-components") / f"{item.item_id}-{revision_id[-12:]}.jpg"
        try:
            still = frame(self.repository, self.project_id, self.run_id, revision_id, item.start_frame + item.duration_frames // 2)
            path.write_bytes(base64.b64decode(still["image_data_url"].split(",", 1)[1]))
        except ProjectError:
            # Footage offline: show the design alone on grey, so there is still something to look at.
            from PIL import Image, ImageDraw
            from clipen.rendering import custom_components
            timeline = self._timeline()
            picture = Image.new("RGBA", (timeline.width, timeline.height), (128, 128, 128, 255))
            custom_components.draw(ImageDraw.Draw(picture), timeline, item, load_style(self, item.style_id))
            picture = picture.convert("RGB")
            picture.thumbnail((960, 960))
            picture.save(path, "JPEG", quality=88)
        return (path,)

    def _production_remove_program_component(self, args):
        timeline = self._timeline()
        if not any(i.item_id == args["element_id"] for t in timeline.tracks if (t.track_id == "program-components" or t.track_id.startswith("program-components-")) for i in t.items):
            raise ProjectError("component_not_found", "找不到這個畫面元件。", status_code=404)
        return self._save(replace(timeline, tracks=tuple(replace(t, items=tuple(i for i in t.items if i.item_id != args["element_id"]))
                                                       for t in timeline.tracks)), args["summary"])


def _custom_problem(error):
    """What went wrong with an Agent's page, in words it can act on."""
    detail = f"（{error.detail}）" if getattr(error, "detail", "") else ""
    return {
        "custom_component_html_missing": "請提供元件的 HTML。",
        "custom_component_html_too_large": "元件頁面超過 200 KB，請精簡或改用向量繪製。",
        "custom_component_empty": "頁面在開頭、中間和結尾都沒有畫出任何東西；請確認元素可見，背景以外的地方保持透明。",
        "custom_component_load_timeout": "頁面在 10 秒內沒有載入完成，可能有無窮迴圈或等待網路。",
        "custom_component_load_failed": "頁面載入失敗" + detail + "。",
        "custom_component_script_error": "頁面的程式出錯" + detail + "。",
        "custom_component_frame_timeout": "有一格超過 10 秒還沒畫完；clipenFrame 要能直接畫出指定的那一格。",
        "custom_component_renderer_missing": "這台電腦沒有可用的 Chrome，無法繪製自由元件" + detail + "。",
    }.get(error.code, "自由元件繪製失敗" + detail + "。")


def load_style(tools, style_id):
    from .production_media import load_text_styles
    return load_text_styles(tools.repository.projects, tools.project_id)[style_id]
