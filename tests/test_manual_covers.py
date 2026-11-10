import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from reliability import TaskStore
from catalogue import index_music
from manual_covers import ManualCovers
from random_artwork import RandomArtwork
from bot import Bot


class ManualCoverTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store.close)
        self.api = Mock()
        self.manual = ManualCovers(self.api,self.store,'-10042',self.root.name)
        self.send = Mock()
        self.source = {'message_id':12,'audio':{'performer':'Artist','title':'Song','file_id':'mp3','file_unique_id':'u','file_size':100,'file_name':'song.mp3'}}
        index_music(self.store,'-10042',self.source)
        self.manual.media.download = Mock(side_effect=lambda fid,path: path.write_bytes(b'data'))
        self.manual.media.edit = Mock(return_value=self.source)

    def handle(self, **kwargs):
        return self.manual.handle({'from':{'id':99},**kwargs},self.send)

    def select(self):
        self.handle(text='/pochette')
        self.handle(**self.source,forward_origin={'type':'channel','chat':{'id':-10042},'message_id':12})

    def test_success_protected_and_cleaned(self):
        self.select()
        with patch('manual_covers.images',return_value=(b'jpeg',b'thumb')), patch('manual_covers.embed_cover'):
            self.handle(photo=[{'file_id':'image'}])
        self.manual.media.edit.assert_called_once()
        row=self.store.db.execute('SELECT jpeg,source_url FROM music_artwork').fetchone()
        self.assertEqual(tuple(row),(b'jpeg','manual:owner'))
        self.assertEqual(list(Path(self.root.name).glob('manual-cover-*')),[])
        resolver=RandomArtwork(self.api,self.store,'-10042',99,self.root.name)
        with patch('random_artwork.find_cover',side_effect=AssertionError('must not search')):
            self.assertEqual(resolver.resolve(12),b'jpeg')

    def test_uncertain_never_replays(self):
        self.select()
        self.manual.media.edit.side_effect=TimeoutError()
        with patch('manual_covers.images',return_value=(b'jpeg',b'thumb')), patch('manual_covers.embed_cover'):
            self.handle(photo=[{'file_id':'image'}])
            self.handle(photo=[{'file_id':'image'}])
        self.manual.media.edit.assert_called_once()
        self.assertEqual(self.store.db.execute('SELECT status FROM manual_covers').fetchone()[0],'uncertain')
        self.assertEqual(list(Path(self.root.name).glob('manual-cover-*')),[])

    def test_owner_only(self):
        bot=Bot(self.api,'TestBot','-10042','url','rules',owner_user_id=99)
        bot.manual_covers=self.manual
        bot.handle({'message':{'chat':{'id':7,'type':'private'},'from':{'id':7},'text':'/pochette'}})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM manual_covers').fetchone()[0],0)
        self.assertIn('réservée',self.api.call.call_args.kwargs['text'])

    def test_cancel(self):
        self.select()
        self.handle(text='/annuler')
        self.assertFalse(self.handle(photo=[{'file_id':'image'}]))
