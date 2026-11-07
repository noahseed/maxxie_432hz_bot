import json
import unittest
from unittest.mock import Mock, patch

from bot import Bot
from openai_chat import OpenAIChat, OpenAIError
from reliability import TaskStore


class OpenAIClientTests(unittest.TestCase):
    @patch('openai_chat.HTTPSConnection')
    def test_responses_request_and_text_extraction(self, connection):
        response = connection.return_value.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({'status': 'completed', 'output': [
            {'type': 'reasoning'}, {'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'Bonjour !'}]}]}).encode()
        self.assertIn('Bonjour !', OpenAIChat('secret', 'test-model').answer('Salut'))
        args = connection.return_value.request.call_args.args
        self.assertEqual(args[:2], ('POST', '/v1/responses'))
        body = json.loads(args[2])
        self.assertFalse(body['store'])
        self.assertEqual(body['input'], [{'role': 'user', 'content': 'Salut'}])
        self.assertEqual(body['model'], 'test-model')
        connection.return_value.close.assert_called_once()

    @patch('openai_chat.HTTPSConnection')
    def test_error_does_not_expose_key_or_api_body(self, connection):
        response = connection.return_value.getresponse.return_value
        response.status = 401
        response.read.return_value = b'sensitive error'
        with self.assertRaises(OpenAIError) as error:
            OpenAIChat('secret', 'model').answer('hi')
        self.assertNotIn('secret', str(error.exception))
        self.assertNotIn('sensitive', str(error.exception))

    def test_requires_key_and_model(self):
        with patch.dict('os.environ', {'OPENAI_API_KEY': '', 'OPENAI_MODEL': 'model'}):
            self.assertIsNone(OpenAIChat.from_environment())
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'secret', 'OPENAI_MODEL': ''}):
            self.assertIsNone(OpenAIChat.from_environment())


class PrivateAITests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store.close)
        self.api = Mock()
        self.bot = Bot(self.api, 'bot', '-10042', 'https://t.me/test', 'Rules', owner_user_id=99)
        self.bot.background = self.store
        self.bot.ai = Mock()
        self.bot.ai.answer.return_value = '🤖 Réponse IA\n\nBonjour'

    def send(self, message_id=1, text='Bonjour', kind='private', **extra):
        self.bot.handle({'message': {'chat': {'id': 7, 'type': kind},
                        'from': {'id': 7}, 'message_id': message_id, 'text': text, **extra}})

    def test_private_text_and_cached_retry(self):
        self.send()
        self.send()
        self.bot.ai.answer.assert_called_once()
        self.assertEqual(self.bot.ai.answer.call_args.args, ('Bonjour',))
        self.assertTrue(callable(self.bot.ai.answer.call_args.kwargs['catalogue']))
        self.assertEqual(self.api.call.call_args.kwargs['chat_id'], 7)

    def test_commands_group_edits_and_media_do_not_call_openai(self):
        self.send(text='/unknown')
        self.send(kind='supergroup')
        self.bot.handle({'edited_message': {'chat': {'id': 7, 'type': 'private'}, 'text': 'edit'}})
        self.send(text='', photo=[{'file_id': 'x'}])
        self.bot.ai.answer.assert_not_called()

    def test_contact_reply_takes_priority(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO contact_prompts VALUES (7,100,9999999999)')
        self.send(reply_to_message={'message_id': 100})
        self.bot.ai.answer.assert_not_called()
        self.assertIn('forwardMessage', [call.args[0] for call in self.api.call.call_args_list])

    def test_daily_limit(self):
        for index in range(21):
            self.send(message_id=index)
        self.assertEqual(self.bot.ai.answer.call_count, 20)
        self.assertIn('limite quotidienne', self.api.call.call_args.kwargs['text'])

    def test_failure_is_cached_and_reported(self):
        self.bot.ai.answer.side_effect = OpenAIError('Service indisponible')
        self.send()
        self.send()
        self.bot.ai.answer.assert_called_once()
        self.assertEqual(self.api.call.call_args.kwargs['text'], 'Service indisponible')

    def test_interruption_is_not_replayed(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO ai_replies VALUES (7,1,?,NULL,0)', ('2026-11-07',))
        self.send()
        self.bot.ai.answer.assert_not_called()
        self.assertIn('interrompue', self.api.call.call_args.kwargs['text'])

    def test_long_input_is_rejected(self):
        self.send(text='a' * 4001)
        self.bot.ai.answer.assert_not_called()
        self.assertIn('4 000', self.api.call.call_args.kwargs['text'])

class CatalogueToolTests(unittest.TestCase):
    def setUp(self):
        from ai_catalogue import CatalogueReader
        from catalogue import index_music
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store.close)
        self.reader = CatalogueReader(self.store, '-10042', 'https://t.me/TestChannel')
        for destination, mid, artist, title in [('-10042', 10, 'Édith Piaf', 'La vie en rose'),
                                                ('-10042', 11, 'Jacques Brel', 'Amsterdam'),
                                                ('-10099', 12, 'Édith Piaf', 'Autre canal')]:
            index_music(self.store, destination, {'message_id': mid, 'audio': {
                'mime_type': 'audio/mpeg', 'performer': artist, 'title': title}})

    def test_search_is_scoped_and_returns_real_links(self):
        result = self.reader({'operation': 'search', 'query': 'edith', 'offset': 0})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['tracks'][0]['url'], 'https://t.me/TestChannel/10')
        self.assertNotIn('Autre canal', json.dumps(result))

    def test_artists_random_summary_and_empty_search(self):
        result = self.reader({'operation': 'summary', 'query': '', 'offset': 0})
        self.assertEqual(result['tracks'], 2)
        artists = self.reader({'operation': 'artists', 'query': '', 'offset': 0})
        self.assertEqual(artists['total'], 2)
        random = self.reader({'operation': 'random', 'query': '', 'offset': 0})
        self.assertIn(random['tracks'][0]['url'], ['https://t.me/TestChannel/10', 'https://t.me/TestChannel/11'])
        self.assertEqual(self.reader({'operation': 'search', 'query': 'absent', 'offset': 0})['total'], 0)

    def test_invalid_arguments_and_sql_are_not_executed(self):
        for value in (None, [], {}, {'operation': 'search', 'query': 'x', 'offset': -1},
                      {'operation': 'summary', 'query': '', 'offset': True},
                      {'operation': 'delete', 'query': '', 'offset': 0}):
            self.assertIn('error', self.reader(value))
        result = self.reader({'operation': 'search', 'query': "'; DROP TABLE music;--", 'offset': 0})
        self.assertEqual(result['total'], 0)
        self.assertEqual(self.reader({'operation': 'summary', 'query': '', 'offset': 0})['tracks'], 2)

    @patch.object(OpenAIChat, '_request')
    def test_tool_round_trip_preserves_reasoning_and_store_false(self, request):
        call = {'type': 'function_call', 'name': 'catalogue', 'call_id': 'call1',
                'arguments': json.dumps({'operation': 'search', 'query': 'Piaf', 'offset': 0})}
        reasoning = {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'encrypted'}
        request.side_effect = [
            {'output': [reasoning, call]},
            {'output': [{'type': 'message', 'content': [{'type': 'output_text',
              'text': 'Édith Piaf : https://t.me/TestChannel/10'}]}]}]
        answer = OpenAIChat('key', 'model').answer('As-tu du Piaf ?', catalogue=self.reader)
        self.assertIn('https://t.me/TestChannel/10', answer)
        payload = request.call_args.args[0]
        self.assertFalse(payload['store'])
        self.assertIn(reasoning, payload['input'])
        result = payload['input'][-1]
        self.assertEqual(result['call_id'], 'call1')
        self.assertIn('https://t.me/TestChannel/10', result['output'])
        self.assertNotIn('Autre canal', result['output'])

    @patch.object(OpenAIChat, '_request')
    def test_unknown_tool_cannot_run(self, request):
        request.return_value = {'output': [{'type': 'function_call', 'name': 'shell',
                                           'call_id': 'evil', 'arguments': '{}'}]}
        with self.assertRaises(OpenAIError):
            OpenAIChat('key', 'model').answer('x', catalogue=self.reader)

    @patch.object(OpenAIChat, '_request')
    def test_tool_loop_is_bounded(self, request):
        request.return_value = {'output': [{'type': 'function_call', 'name': 'catalogue',
                   'call_id': 'again', 'arguments': '{"operation":"summary","query":"","offset":0}'}]}
        with self.assertRaises(OpenAIError):
            OpenAIChat('key', 'model').answer('x', catalogue=self.reader)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args.args[0]['tool_choice'], 'none')
