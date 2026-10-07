import unittest
from unittest.mock import Mock, patch
from app.backends import WindowsBackend


class WindowsControlTests(unittest.TestCase):
    def test_only_matching_document_is_controlled(self):
        win = Mock(JOB_CONTROL_PAUSE=1, JOB_CONTROL_RESUME=2, JOB_CONTROL_DELETE=5)
        win.EnumJobs.return_value = [{'JobId': 12, 'pDocument': 'task50_g001.pdf'},
                                     {'JobId': 13, 'pDocument': 'other.pdf'}]
        with patch.dict('sys.modules', {'win32print':win}):
            self.assertEqual(WindowsBackend().control_group({'queue_name':'printer'}, r'C:\data\task50_g001.pdf','cancel'),1)
        win.SetJob.assert_called_once_with(win.OpenPrinter.return_value,12,0,None,5)
        win.ClosePrinter.assert_called_once()

    def test_unknown_jobs_are_not_purged(self):
        win = Mock()
        win.EnumJobs.return_value = [{'JobId': 13, 'pDocument': 'other.pdf'}]
        with patch.dict('sys.modules', {'win32print':win}), self.assertRaisesRegex(RuntimeError,'无法准确识别'):
            WindowsBackend().control_group({'queue_name':'printer'}, 'task.pdf','cancel')
        win.SetJob.assert_not_called()
        win.ClosePrinter.assert_called_once()
