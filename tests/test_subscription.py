import unittest
from unittest.mock import Mock
from bot import Bot
from reliability import TelegramError


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.api=Mock()
        self.bot=Bot(self.api,'bot','-10042','https://t.me/test','rules',owner_user_id=99,require_membership=True)
        self.bot.show_help=Mock()
        self.bot.private_welcome=Mock()

    def message(self, uid=7):
        self.bot.handle({'message':{'chat':{'id':uid,'type':'private'},'from':{'id':uid},'text':'/aide'}})

    def test_nonmembers_blocked(self):
        for status in ('left','kicked','restricted','unknown'):
            self.api.call.return_value={'status':status,'is_member':False}
            self.message()
            self.bot.show_help.assert_not_called()
            buttons=self.api.call.call_args.kwargs['reply_markup']['inline_keyboard']
            self.assertEqual(buttons[1][0]['callback_data'],'check_subscription')

    def test_members_and_owner_allowed(self):
        for status in ('member','creator','administrator'):
            self.api.call.return_value={'status':status}
            self.message()
            self.bot.show_help.assert_called_with(7,7)
        self.api.reset_mock()
        self.message(99)
        self.api.call.assert_not_called()

    def test_existing_buttons_cannot_bypass_gate(self):
        self.api.call.return_value={'status':'left'}
        self.bot.handle({'callback_query':{'id':'q','from':{'id':7},'message':{'chat':{'id':7,'type':'private'}},'data':'help'}})
        self.bot.show_help.assert_not_called()

    def test_join_then_verify_opens_home_and_leaving_revokes(self):
        callback={'callback_query':{'id':'q','from':{'id':7},'message':{'chat':{'id':7,'type':'private'}},'data':'check_subscription'}}
        self.api.call.return_value={'status':'member'}
        self.bot.handle(callback)
        self.bot.private_welcome.assert_called_once_with(7)
        self.api.call.return_value={'status':'left'}
        self.message()
        self.bot.show_help.assert_not_called()

    def test_network_error_does_not_grant_access(self):
        self.api.call.side_effect=[TelegramError(503),{}]
        self.message()
        self.bot.show_help.assert_not_called()
        self.assertIn('temporairement', self.api.call.call_args.kwargs['text'])
