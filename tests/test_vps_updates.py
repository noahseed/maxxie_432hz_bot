import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import vps_updates as vps
from bot import Bot


class MaintenanceTests(unittest.TestCase):
    def test_only_private_owner_can_request(self):
        bot = Bot(Mock(), 'bot', '-1001', 'https://t.me/test', 'rules', owner_user_id=42)
        with patch.object(vps, 'request_upgrade', return_value=True) as request:
            for user, kind in [(7, 'private'), (42, 'group'), (42, 'private')]:
                bot.handle({'callback_query': {'id': 'x', 'from': {'id': user},
                           'message': {'chat': {'id': user, 'type': kind}},
                           'data': 'vps:' + 'a' * 32}})
            request.assert_called_once_with('a' * 32)

    def test_check_and_single_use_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vps, 'STATE', Path(directory)), \
                patch.object(vps, 'apt') as apt, patch.object(vps, 'packages', return_value=['linux-image']), \
                patch.object(vps, 'take_request') as request:
            state = {}
            vps.process(state, 'check')
            self.assertIn('reply_markup', state['outbox'][0])
            request.return_value = state['token']
            vps.process(state, 'request')
            self.assertNotIn('token', state)
            calls = apt.call_count
            vps.process(state, 'request')
            self.assertEqual(apt.call_count, calls)

    def test_no_updates_no_button_or_notification(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vps, 'STATE', Path(directory)), \
                patch.object(vps, 'apt'), patch.object(vps, 'packages', return_value=[]):
            state = {'token': 'old'}
            vps.process(state, 'check')
            self.assertNotIn('token', state)
            self.assertFalse(state.get('outbox'))

    def test_update_failure_prevents_upgrade(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vps, 'STATE', Path(directory)), \
                patch.object(vps, 'apt', side_effect=RuntimeError('repository failure')) as apt, \
                patch.object(vps, 'take_request', return_value='valid'):
            state = {'token': 'valid', 'expires': time.time() + 60}
            vps.process(state, 'request')
            apt.assert_called_once_with('--error-on=any', 'update')
            self.assertIn('Échec', state['outbox'][-1]['text'])
            self.assertNotIn('token', state)

    def test_expired_request_does_not_run_apt(self):
        with patch.object(vps, 'apt') as apt, patch.object(vps, 'take_request', return_value='old'):
            vps.process({'token': 'old', 'expires': 0}, 'request')
            apt.assert_not_called()

    def test_report_has_reboot_button_only_when_required(self):
        for required in (False, True):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as directory, \
                    patch.object(vps, 'STATE', Path(directory)), patch.object(vps, 'apt'), \
                    patch.object(vps, 'packages', return_value=[]), \
                    patch.object(vps, 'take_request', return_value='valid'), \
                    patch.object(vps, 'REBOOT_REQUIRED') as marker:
                marker.exists.return_value = required
                state = {'token': 'valid', 'expires': time.time() + 60}
                vps.process(state, 'request')
                report = state['outbox'][-1]
                self.assertEqual('reply_markup' in report, required)
                if required:
                    button = report['reply_markup']['inline_keyboard'][0][0]
                    self.assertEqual(button['text'], 'Redémarrer le VPS')
                    self.assertEqual(button['callback_data'], 'vps-reboot:' + state['reboot_token'])

    def test_reboot_is_single_use_and_requires_current_marker(self):
        for required in (False, True):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as directory, \
                    patch.object(vps, 'STATE', Path(directory)), \
                    patch.object(vps, 'REBOOT_REQUIRED') as marker, \
                    patch.object(vps.subprocess, 'run') as run:
                marker.exists.return_value = required
                state = {'reboot_token': 'valid', 'reboot_expires': time.time() + 60}
                vps.process_reboot(state, 'wrong')
                run.assert_not_called()
                vps.process_reboot(state, 'valid')
                vps.process_reboot(state, 'valid')
                self.assertEqual(run.call_count, int(required))
                self.assertNotIn('reboot_token', state)

    def test_reboot_callback_reserved_to_private_owner(self):
        bot = Bot(Mock(), 'bot', '-1001', 'https://t.me/test', 'rules', owner_user_id=42)
        with patch.object(vps, 'request_upgrade', return_value=True) as request:
            for user, kind in [(7, 'private'), (42, 'group'), (42, 'private')]:
                bot.handle({'callback_query': {'id': 'x', 'from': {'id': user},
                           'message': {'chat': {'id': user, 'type': kind}},
                           'data': 'vps-reboot:' + 'a' * 32}})
            request.assert_called_once_with('a' * 32, reboot=True)

    def test_reboot_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vps, 'STATE', Path(directory)), \
                patch.object(vps, 'REBOOT_REQUIRED') as marker, \
                patch.object(vps.subprocess, 'run', side_effect=OSError):
            marker.exists.return_value = True
            state = {'reboot_token': 'valid', 'reboot_expires': time.time() + 60}
            vps.process_reboot(state, 'valid')
            self.assertIn('Échec', state['outbox'][-1]['text'])

    def test_manual_check_command_only_private_owner(self):
        bot = Bot(Mock(), 'bot', '-1001', 'https://t.me/test', 'rules', owner_user_id=42)
        with patch.object(vps, 'request_check') as request:
            for user, kind, command in [(7, 'private', '/update'), (42, 'group', '/update'),
                                        (42, 'private', '/update@OtherBot')]:
                bot.handle({'message': {'chat': {'id': user, 'type': kind},
                            'from': {'id': user}, 'text': command}})
            request.assert_not_called()
            for command in ('/update', '/update@bot'):
                bot.handle({'message': {'chat': {'id': 42, 'type': 'private'},
                            'from': {'id': 42}, 'text': command}})
            self.assertEqual(request.call_count, 2)

    def test_manual_check_without_updates_reports_result(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vps, 'STATE', Path(directory)), \
                patch.object(vps, 'apt') as apt, patch.object(vps, 'packages', return_value=[]), \
                patch.object(vps, 'take_request', side_effect=['', 'check']):
            state = {}
            vps.process(state, 'request')
            apt.assert_called_once_with('--error-on=any', 'update')
            self.assertIn('à jour', state['outbox'][-1]['text'])
            self.assertNotIn('reply_markup', state['outbox'][-1])

    def test_manual_check_with_updates_has_button_and_never_upgrades(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(vps, 'STATE', Path(directory)), \
                patch.object(vps, 'apt') as apt, patch.object(vps, 'packages', return_value=['example']):
            state = {}
            vps.process(state, 'manual-check')
            apt.assert_called_once_with('--error-on=any', 'update')
            self.assertIn('reply_markup', state['outbox'][-1])

    def test_check_request_uses_separate_file(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(vps, 'REQUEST', Path(directory) / 'upgrade'), \
                patch.object(vps, 'CHECK_REQUEST', Path(directory) / 'check'):
            vps.request_upgrade('a' * 32)
            vps.request_check()
            self.assertEqual(vps.take_request(), 'a' * 32)
            self.assertEqual(vps.take_request(vps.CHECK_REQUEST), 'check')
