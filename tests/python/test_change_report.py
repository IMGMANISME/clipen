"""Every AI change comes with an account the person can read: what changed, why, and each note answered."""
import unittest

from clipen.editing.change_report import check_report
from clipen.editing.versions import versions
from clipen.projects import ProjectError
from test_versions import database

CHANGE = {'what': '開場縮短 3 秒', 'why': '你說開場太慢'}
INTENT = {'latest_revision_request': {'references': {'items': [{'reference_id': 'n1', 'kind': 'change', 'note': '開場太慢'},
                                                               {'reference_id': 'n2', 'kind': 'keep', 'note': '這段很好'}]}}}


class ChangeReportTests(unittest.TestCase):
    def refused(self, arguments, intent=INTENT):
        with self.assertRaises(ProjectError) as caught:
            check_report(arguments, intent)
        return caught.exception.code

    def test_the_ai_says_what_it_changed_and_answers_every_note(self):
        answers = [{'reference_id': 'n1', 'outcome': 'done', 'explanation': '剪掉等車的鏡頭'},
                   {'reference_id': 'n2', 'outcome': 'kept', 'explanation': '沒有動'}]
        report = check_report({'summary': '節奏變快', 'changes': [CHANGE], 'notes': answers, 'not_done': ['配樂還沒換']}, INTENT)
        self.assertEqual((report['changes'], report['not_done']), ([CHANGE], ['配樂還沒換']))
        self.assertEqual(self.refused({'summary': 'x'}), 'change_report_invalid')
        self.assertEqual(self.refused({'summary': 'x', 'changes': [{'what': '改了', 'why': ''}], 'notes': answers}), 'change_report_invalid')
        # A note left unanswered, or answered twice, is refused.
        self.assertEqual(self.refused({'summary': 'x', 'changes': [CHANGE], 'notes': answers[:1]}), 'change_report_invalid')
        self.assertEqual(self.refused({'summary': 'x', 'changes': [CHANGE], 'notes': [answers[0]] * 2 + [answers[1]]}), 'change_report_invalid')
        # The first cut has no notes to answer.
        self.assertEqual(check_report({'summary': 'x', 'changes': [CHANGE]}, {})['notes'], [])

    def test_the_history_shows_the_report_next_to_the_notes_it_answers(self):
        report = {'changes': [CHANGE], 'notes': [{'reference_id': 'n1', 'outcome': 'partly', 'explanation': '只剪了一點'}], 'not_done': []}
        db = database([
            ('run_created', {}), ('revision_created', {'revision_id': 'r0'}), ('run_finished', {'summary': '第一版'}),
            ('refinement_requested', {'instruction': '開場快一點', 'base_revision_id': 'r0',
                                      'references': {'items': [{'reference_id': 'n1', 'kind': 'change', 'note': '開場太慢'}]}}),
            ('revision_created', {'revision_id': 'r1'}), ('run_finished', {'summary': '開場變快了', 'report': report}),
        ])
        newest, first = versions(db, 'run', 'claude')
        self.assertEqual(newest['report']['summary'], '開場變快了')
        self.assertEqual(newest['report']['notes'][0] | {}, {'reference_id': 'n1', 'outcome': 'partly', 'explanation': '只剪了一點',
                                                             'outcome_label': '部分處理', 'kind': 'change', 'note': '開場太慢'})
        # Older versions finished before reports existed still show their summary.
        self.assertEqual((first['report']['summary'], first['report']['changes']), ('第一版', []))


if __name__ == '__main__':
    unittest.main()
