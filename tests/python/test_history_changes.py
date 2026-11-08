"""Each version says what changed, in words, rather than a number."""
import unittest

from clipen.editing.history_changes import describe, summary


def film(*, components=(), cut=((0, 300),), subtitles=(), duration=None, music='song-a'):
    video = [{'kind': 'media', 'item_id': f'shot-{n}', 'start_frame': start, 'duration_frames': length, 'source': {'asset': 'a', 'from': start},
              'effects': [{'effect_id': 'audio.layers', 'parameters': {'layers': [{'asset_id': music, 'start_frame': start}]}}] if n == 0 else []}
             for n, (start, length) in enumerate(cut)]
    return {'frame_rate': {'num': 30, 'den': 1}, 'duration_frames': duration or sum(length for _, length in cut),
            'tracks': [{'track_id': 'video-main', 'kind': 'video', 'items': video},
                       {'track_id': 'program-components', 'kind': 'title', 'items': [
                           {'kind': 'text', 'item_id': key, 'start_frame': start, 'duration_frames': 30, 'text': text, 'style_id': style}
                           for key, text, start, style in components]},
                       {'track_id': 'agent-subtitles', 'kind': 'title', 'items': [
                           {'kind': 'text', 'item_id': f's{n}', 'start_frame': n * 30, 'duration_frames': 30, 'text': text, 'style_id': 'caption'}
                           for n, text in enumerate(subtitles)]}]}


class HistoryChangeTests(unittest.TestCase):
    def test_graphic_edits_read_as_sentences(self):
        styles = {'a': {'x': .1, 'y': .1}, 'b': {'x': .5, 'y': .1}, 'c': {'x': .5, 'y': .1, 'scale': 1.3},
                  'd': {'x': .5, 'y': .1, 'scale': 1.3, 'colors': {'accent': '#ff0000'}}, 'e': {'html': '<b>1</b>'}, 'f': {'html': '<b>2</b>'}}
        before = film(components=[('loc', '阿夫利拉麵｜AFURI', 10, 'a')])
        self.assertEqual(describe(before, film(components=[('loc', '阿拉麵｜AFURI', 10, 'a')])), ['把「阿夫利拉麵｜AFURI」改成「阿拉麵｜AFURI」'])
        self.assertEqual(describe(before, film(components=[('loc', '阿夫利拉麵｜AFURI', 10, 'b')]), styles), ['移動「阿夫利拉麵｜AFURI」'])
        self.assertEqual(describe(film(components=[('loc', '拉麵', 10, 'b')]), film(components=[('loc', '拉麵', 10, 'c')]), styles), ['放大「拉麵」'])
        self.assertEqual(describe(film(components=[('loc', '拉麵', 10, 'c')]), film(components=[('loc', '拉麵', 10, 'd')]), styles), ['換了「拉麵」的顏色'])
        self.assertEqual(describe(film(components=[('t', '標題', 10, 'e')]), film(components=[('t', '標題', 10, 'f')]), styles), ['重新設計「標題」'])
        self.assertEqual(describe(before, film()), ['刪除「阿夫利拉麵｜AFURI」'])
        self.assertEqual(describe(before, film(components=[('loc', '阿夫利拉麵｜AFURI', 40, 'a')])), ['調整「阿夫利拉麵｜AFURI」出現的時間'])
        self.assertEqual(describe(before, before), [])

    def test_a_new_cut_is_one_change_not_every_graphic_that_moved_with_it(self):
        before = film(components=[('loc', '新宿', 100, 'a')], cut=((0, 300), (300, 300)))
        after = film(components=[('loc', '新宿', 60, 'a')], cut=((0, 260), (260, 300)))
        self.assertEqual(describe(before, after), ['重新剪輯，片長 0:20 → 0:19'])
        self.assertEqual(describe(film(), film(music='song-b')), ['換了配樂'])
        self.assertEqual(describe(film(subtitles=['一', '二']), film(subtitles=['一', '三'])), ['修改 1 句字幕'])

    def test_long_lists_are_shortened(self):
        self.assertEqual(summary(['甲', '乙', '丙', '丁']), '甲、乙，還有 2 處')
        self.assertIsNone(summary([]))


if __name__ == '__main__':
    unittest.main()
