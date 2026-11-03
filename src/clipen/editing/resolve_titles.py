"""Turn Clipen's components into DaVinci Resolve titles whose words stay editable.

Resolve keeps Fusion titles only in its own timeline format (.drt), which it
writes but never reads from FCPXML. So the hand-off goes through Resolve: the
FCPXML places each component as a transparent PNG, the person exports that
timeline from Resolve as .drt, and convert() swaps every component PNG on it
for a Fusion title. The title draws the component's rows with Text+ (font,
size, tracking, colour and shadow as Clipen set them) over a picture of
everything else (boxes, rules, icons), and publishes each row's words to the
Edit page Inspector.

A .drt is a zip of XML. A clip's Fusion composition is a qCompress'd Qt
QVariantMap whose "0_data" is the composition header, a NUL, a little-endian
length and the zlib'd tool list; DaVinci Resolve 21 writes and reads that.
"""
from __future__ import annotations

import html
import json
import re
import struct
import unicodedata
import uuid
import zipfile
import zlib
from pathlib import Path

from clipen.projects import ProjectError

MANIFEST = '.clipen 元件.json'
# Text+ Size is a fraction of the frame width, and a glyph's em is 1/1.76 of it; Text+ tracking adds
# (CharacterSpacing - 1) x Size x width between letters. Measured in Resolve 21 on PingFang TC and Songti TC.
_SIZE = 1.76
_ITEM = re.compile(r'<Sm2TiVideoClip DbId="[^"]+">.*?</Sm2TiVideoClip>', re.S)


def _lua(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def _number(value: float) -> str:
    return f'{value:.6g}'


def tools(rows: list[dict], picture: Path | None, width: int, height: int, frames: int) -> str:
    """The Fusion tool list for one component: rows of Text+ merged over its picture."""
    last = frames - 1
    frame = f'GlobalOut = Input {{ Value = {last}, }}, Width = Input {{ Value = {width}, }}, Height = Input {{ Value = {height}, }}, UseFrameFormatSettings = Input {{ Value = 1, }}, '
    nodes, inputs = [], []
    if picture:
        nodes.append(f'Picture = Loader {{ Clips = {{ Clip {{ ID = "Clip1", Filename = {_lua(str(picture))}, FormatID = "PNGFormat", '
                     f'StartFrame = -1, LengthSetManually = true, TrimIn = 0, TrimOut = 0, ExtendFirst = 0, ExtendLast = {last}, '
                     f'Loop = 0, AspectMode = 0, Depth = 0, TimeCode = 0, GlobalStart = 0, GlobalEnd = {last} }} }}, '
                     'ViewInfo = OperatorInfo { Pos = { 0, 0 } }, }')
    # Each row merges over what lies below it: the picture, or the first row when there is none.
    below = 'Picture' if picture else None
    for number, row in enumerate(rows, 1):
        name, label = f'Row{number}', '' if len(rows) == 1 else f'第 {number} 行'
        red, green, blue, alpha = (v / 255 for v in row['fill'])
        shading = ''
        if row['shadow']:
            sr, sg, sb, sa = (v / 255 for v in row['shadow']['colour'])
            offset = row['shadow']['offset']
            shading = (f'Enabled2 = Input {{ Value = 1, }}, Name2 = Input {{ Value = "Shadow", }}, ElementShape2 = Input {{ Value = 0, }}, '
                       f'Red2 = Input {{ Value = {_number(sr)}, }}, Green2 = Input {{ Value = {_number(sg)}, }}, Blue2 = Input {{ Value = {_number(sb)}, }}, '
                       f'Alpha2 = Input {{ Value = {_number(sa)}, }}, Offset2 = Input {{ Value = {{ {_number(offset / width)}, {_number(-offset / height)} }}, }}, '
                       f'Softness2 = Input {{ Value = 1, }}, SoftnessX2 = Input {{ Value = {row["shadow"]["blur"]}, }}, SoftnessY2 = Input {{ Value = {row["shadow"]["blur"]}, }}, ')
        nodes.append(
            f'{name} = TextPlus {{ Inputs = {{ {frame}'
            f'Center = Input {{ Value = {{ {_number(row["x"] / width)}, {_number(1 - row["baseline"] / height)} }}, }}, '
            'CenterOnBaseOfFirstLine = Input { Value = 1, }, HorizontalLeftCenterRight = Input { Value = -1, }, '
            'VerticalJustificationNew = Input { Value = 3, }, HorizontalJustificationNew = Input { Value = 3, }, '
            f'StyledText = Input {{ Value = {_lua(row["text"])}, }}, Font = Input {{ Value = {_lua(row["family"])}, }}, '
            f'Style = Input {{ Value = {_lua(row["style"])}, }}, Size = Input {{ Value = {_number(row["size"] * _SIZE / width)}, }}, '
            f'CharacterSpacing = Input {{ Value = {_number(1 + row["track"] / _SIZE)}, }}, '
            f'Red1 = Input {{ Value = {_number(red)}, }}, Green1 = Input {{ Value = {_number(green)}, }}, Blue1 = Input {{ Value = {_number(blue)}, }}, '
            f'Alpha1 = Input {{ Value = {_number(alpha)}, }}, {shading}}}, ViewInfo = OperatorInfo {{ Pos = {{ 110, {60 * number} }} }}, }}')
        if below:
            nodes.append(f'Merge{number} = Merge {{ Inputs = {{ Background = Input {{ SourceOp = "{below}", Source = "Output", }}, '
                         f'Foreground = Input {{ SourceOp = "{name}", Source = "Output", }}, PerformDepthMerge = Input {{ Value = 0, }}, }}, '
                         f'ViewInfo = OperatorInfo {{ Pos = {{ 220, {60 * number} }} }}, }}')
        below = f'Merge{number}' if below else name
        group = 10 * number
        inputs += [f'{name}Text = InstanceInput {{ SourceOp = "{name}", Source = "StyledText", Name = {_lua((label + " 文字").strip())}, }}',
                   f'{name}Font = InstanceInput {{ SourceOp = "{name}", Source = "Font", Name = {_lua((label + " 字型").strip())}, ControlGroup = {group}, }}',
                   f'{name}Style = InstanceInput {{ SourceOp = "{name}", Source = "Style", ControlGroup = {group}, }}',
                   f'{name}Size = InstanceInput {{ SourceOp = "{name}", Source = "Size", Name = {_lua((label + " 大小").strip())}, }}',
                   f'{name}Red = InstanceInput {{ SourceOp = "{name}", Source = "Red1Clone", Name = {_lua((label + " 顏色").strip())}, ControlGroup = {group + 1}, }}',
                   f'{name}Green = InstanceInput {{ SourceOp = "{name}", Source = "Green1Clone", ControlGroup = {group + 1}, }}',
                   f'{name}Blue = InstanceInput {{ SourceOp = "{name}", Source = "Blue1Clone", ControlGroup = {group + 1}, }}',
                   f'{name}Alpha = InstanceInput {{ SourceOp = "{name}", Source = "Alpha1Clone", ControlGroup = {group + 1}, }}']
    # Resolve shows a Text+ title's controls from a tool called Template, and MediaOut reads the tool inside the group.
    return ('{ Template = GroupOperator { CtrlWZoom = false, Inputs = ordered() { ' + ', '.join(inputs) + ' }, '
            f'Outputs = {{ MainOutput1 = InstanceOutput {{ SourceOp = "{below}", Source = "Output", }} }}, '
            'ViewInfo = GroupInfo { Pos = { 0, 0 } }, Tools = ordered() { ' + ', '.join(nodes) + ' }, }, '
            f'MediaOut1 = MediaOut {{ Inputs = {{ Index = Input {{ Value = "0", }}, Input = Input {{ SourceOp = "{below}", Source = "Output", }} }}, '
            'ViewInfo = OperatorInfo { Pos = { 330, 0 } }, } }')


def _qstring(value: str) -> bytes:
    data = value.encode('utf-16-be')
    return struct.pack('>I', len(data)) + data


def composition(tool_list: str, width: int, height: int, frames: int) -> str:
    """A clip's CompositionBA: the tool list wrapped the way Resolve stores a Text+ title's comp."""
    last = frames - 1
    header = (f'Composition {{ CurrentTime = 0, RenderRange = {{ 0, {last} }}, GlobalRange = {{ 0, {last} }}, CurrentID = 1, HiQ = true, '
              'PlaybackUpdateMode = 0, StereoMode = false, Version = "DaVinci Resolve 21.1.2.0010", SavedOutputs = 1, HeldTools = 0, '
              'DisabledTools = 0, LockedTools = 0, AudioOffset = 0, Resumable = true, OutputClips = { }, CustomData = { TEMPLATE_ID = "Text+" }, '
              f'Prefs = {{ Comp = {{ FrameFormat = {{ Width = {width}, Height = {height}, GuideRatio = {_number(width / height)}, '
              f'DepthFull = 3, DepthPreview = 3, DepthInteractive = 3, }}, Unsorted = {{ GlobalEnd = {last} }}, }} }}, Compressed = true, }}')
    inner = tool_list.encode('utf-8')
    data = header.encode('utf-8') + b'\x00' + struct.pack('<I', len(inner)) + zlib.compress(inner, 9)
    variant = struct.pack('>II', 1, 4)
    variant += _qstring('count') + struct.pack('>IBi', 2, 0, 1)
    variant += _qstring('0_name') + struct.pack('>IB', 10, 0) + _qstring('Composition 1')
    variant += _qstring('0_mediaIdList') + struct.pack('>IBI', 11, 0, 0)
    variant += _qstring('0_data') + struct.pack('>IBI', 12, 0, len(data)) + data
    return (struct.pack('>I', len(variant)) + zlib.compress(variant, 9)).hex()


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7f, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _fields(start: int) -> str:
    """A Text+ Fusion Title clip's FieldsBlob: its template name and the frame it starts on."""
    name = b'Text+'
    title = b'\x22' + bytes([len(name)]) + name + b'\x4a' + bytes([len(name)]) + name + b'\xa8\x01' + _varint(start)
    clip = b'\x0a' + _varint(len(title)) + title + b'\x20\x01'
    body = b'\x0a' + _varint(len(clip)) + clip + b'\x12\x10' + bytes.fromhex('0000000000000006ffffffffffffffff')
    return (struct.pack('>II', 2, len(body) + 1) + b'\x80' + body).hex()


def title_item(start: int, duration: int, frame_rate: str, comp: str, indent: str) -> str:
    """A Fusion Title clip in a .drt sequence, shaped like the ones Resolve 21 writes."""
    lines = [
        f'<Sm2TiVideoClip DbId="{uuid.uuid4()}">', f' <FieldsBlob>{_fields(start)}</FieldsBlob>',
        ' <PrettyType>Fusion Title</PrettyType>', ' <Name>Fusion Title</Name>',
        f' <Start>{start}</Start>', f' <Duration>{duration}</Duration>',
        ' <LinkedItemSync/>', ' <WasDisbanded>false</WasDisbanded>', ' <MarkersBA/>', ' <UiMemento>0</UiMemento>',
        ' <Flags>0</Flags>', ' <PriorityIndex>0</PriorityIndex>', ' <EffectFiltersBA/>', ' <ImportExportMetadataBA/>',
        ' <RenderTextEnabled>true</RenderTextEnabled>', ' <RenderTextGanged>true</RenderTextGanged>',
        ' <RenderTextPrefixed>true</RenderTextPrefixed>', ' <CompositionTable>',
        f'  <Sm2TiCompositionTable DbId="{uuid.uuid4()}">',
        '   <FieldsBlob>000000010000000200000016004c006100730074004d006f006400540069006d0065000000040000000000b5a8a2230000002c00460075007300'
        '69006f006e00530074006100720074004600720061006d0065004f00660066007300650074000000020000000000</FieldsBlob>',
        '   <DbSavedTime>0</DbSavedTime>', f'   <CompositionBA>{comp}</CompositionBA>',
        '   <ActiveCompositionIdx>0</ActiveCompositionIdx>', '  </Sm2TiCompositionTable>', ' </CompositionTable>',
        ' <In/>', ' <MixedFrameRateAlignment>0</MixedFrameRateAlignment>', ' <MediaStartTime>0</MediaStartTime>',
        ' <MediaFilePath/>', ' <MediaReelNumber/>', f' <MediaFrameRate>{frame_rate}</MediaFrameRate>',
        ' <MediaTimemapBA>024013ddddddddddde</MediaTimemapBA>', ' <LastChangedTime>0</LastChangedTime>',
        ' <LastRenderedTime>0</LastRenderedTime>', ' <IsMarkedForCaching>false</IsMarkedForCaching>',
        ' <IsForceConformed>true</IsForceConformed>', ' <MatchConflictState>0</MatchConflictState>',
        ' <UseOppositeSrcForLeftEye>false</UseOppositeSrcForLeftEye>', ' <UseOppositeSrcForRightEye>false</UseOppositeSrcForRightEye>',
        ' <RenderCacheBA/>', ' <CurrentSelectorIdx>1</CurrentSelectorIdx>', ' <IsPreConformed>false</IsPreConformed>',
        ' <PreConformMediaExtents>00000100000030c20000010000003042</PreConformMediaExtents>', ' <MediaMetadata/>',
        ' <Thumbnail>', f'  <BtThumnail DbId="{uuid.uuid4()}">', '   <FieldsBlob/>', '   <ImgWidth>-1</ImgWidth>',
        '   <ImgHeight>-1</ImgHeight>', '   <Buffer/>', '  </BtThumnail>', ' </Thumbnail>',
        ' <ThumbnailDirtyFlag>true</ThumbnailDirtyFlag>', '</Sm2TiVideoClip>']
    return ('\n' + indent).join(lines)


def _tag(item: str, name: str) -> str | None:
    found = re.search(f'<{name}>([^<]*)</{name}>', item)
    return html.unescape(found.group(1)) if found else None


def _sequences(archive: zipfile.ZipFile):
    return [info for info in archive.infolist() if info.filename.startswith('SeqContainer/') and info.filename.endswith('.xml')]


def uses(source: Path, components: Path) -> bool:
    """Whether a .drt has clips that point at the component PNGs in components."""
    marker = '<MediaFilePath>' + unicodedata.normalize('NFC', html.escape(str(components), quote=False)) + '/'
    try:
        with zipfile.ZipFile(source) as archive:
            return any(marker in unicodedata.normalize('NFC', archive.read(info).decode('utf-8', 'replace'))
                       for info in _sequences(archive))
    except (OSError, zipfile.BadZipFile):
        return False


def convert(source: Path, target: Path) -> int:
    """Write target: the Resolve timeline at source with every Clipen component PNG made a Fusion title.

    The components' type lives in the manifest beside their PNGs, found through each clip's file
    path. Returns how many clips were replaced."""
    manifests: dict[Path, dict] = {}

    def entry(path: Path):
        folder = path.parent
        if folder not in manifests:
            try:
                manifests[folder] = json.loads((folder / MANIFEST).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                manifests[folder] = {}
        return manifests[folder].get(path.name)

    replaced = 0

    def swap(match: re.Match) -> str:
        nonlocal replaced
        item = match.group(0)
        path = _tag(item, 'MediaFilePath')
        component = entry(Path(path)) if path else None
        if not component:
            return item
        start, duration = int(_tag(item, 'Start')), int(_tag(item, 'Duration'))
        picture = Path(path).parent / component['picture'] if component['picture'] else None
        width, height = component['width'], component['height']
        comp = composition(tools(component['rows'], picture, width, height, duration), width, height, duration)
        indent = match.string[match.string.rfind('\n', 0, match.start()) + 1:match.start()]
        replaced += 1
        return title_item(start, duration, _tag(item, 'MediaFrameRate') or '0000000000003e400000000000000000', comp, indent)

    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise ProjectError('resolve_timeline_unreadable', '這個檔案不是 DaVinci Resolve 匯出的 .drt 時間線。', status_code=409) from error
    with archive, zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as out:
        sequences = {info.filename for info in _sequences(archive)}
        for info in archive.infolist():
            data = archive.read(info)
            if info.filename in sequences:
                data = _ITEM.sub(swap, data.decode('utf-8')).encode('utf-8')
            out.writestr(info, data)
    return replaced
