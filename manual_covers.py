"""Correction privée, persistante et protégée des pochettes par le propriétaire."""
import fcntl
import json
import tempfile
import time
from pathlib import Path
from covers import MediaAPI, MAX_AUDIO, images, embed_cover
from catalogue import index_music, track_identity


class ManualCovers:
    def __init__(self, api, store, group, directory):
        self.api, self.store, self.group = api, store, str(group)
        self.directory = Path(directory)
        self.media = MediaAPI(api)
        with store.lock, store.db:
            store.db.execute('CREATE TABLE IF NOT EXISTS manual_covers (owner INTEGER PRIMARY KEY, mid INTEGER, source TEXT, status TEXT NOT NULL)')

    def handle(self, message, send):
        owner = message['from']['id']
        command = message.get('text', '').split('@')[0].strip().lower()
        if command in ('/pochette', '/annuler'):
            with self.store.lock, self.store.db:
                self.store.db.execute('DELETE FROM manual_covers WHERE owner=?', (owner,))
                if command == '/pochette':
                    self.store.db.execute("INSERT INTO manual_covers VALUES (?,NULL,NULL,'track')", (owner,))
            send('Transférez le MP3 depuis le canal, puis envoyez sa pochette en photo ou fichier JPEG/PNG. /annuler pour quitter.' if command == '/pochette' else 'Correction manuelle quittée.')
            return True
        with self.store.lock:
            row = self.store.db.execute('SELECT mid,source,status FROM manual_covers WHERE owner=?', (owner,)).fetchone()
        if not row:
            return False
        mid, source, status = row
        if status in ('applying', 'uncertain'):
            send('La modification précédente doit être vérifiée dans le canal. Aucun nouvel envoi automatique. /pochette pour recommencer explicitement.')
            return True
        origin = message.get('forward_origin', {})
        audio = message.get('audio') or message.get('document') or {}
        if origin.get('type') == 'channel' and str(origin.get('chat', {}).get('id')) == self.group and (message.get('audio') or audio.get('mime_type') == 'audio/mpeg'):
            mid = origin.get('message_id')
            with self.store.lock:
                known = self.store.db.execute('SELECT track_key FROM music WHERE group_id=? AND message_id=?', (self.group,mid)).fetchone()
            if not known:
                send('Ce morceau est absent du catalogue. Quittez avec /annuler et transférez-le au bot pour l’indexer, puis réessayez.')
                return True
            if message.get('audio') and track_identity(audio.get('performer',''), audio.get('title','')) != known[0]:
                send('Le fichier transféré ne correspond plus au morceau du catalogue. Transférez sa version actuelle depuis le canal.')
                return True
            if audio.get('file_size', 0) > MAX_AUDIO:
                send('Ce MP3 dépasse la limite de téléchargement de 20 Mo. Choisissez un autre morceau ou /annuler.')
                return True
            source = {k: message[k] for k in ('audio','document','caption','caption_entities') if k in message}
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE manual_covers SET mid=?,source=?,status='image' WHERE owner=?", (mid,json.dumps(source),owner))
            send('Morceau sélectionné. Envoyez maintenant la bonne pochette en photo ou en fichier JPEG/PNG.')
            return True
        photo = (message.get('photo') or [{}])[-1]
        document = message.get('document') or {}
        image = photo if photo.get('file_id') else document if document.get('mime_type') in ('image/jpeg','image/png') else {}
        if status != 'image' or not image.get('file_id'):
            send('Transférez d’abord un MP3 de ce canal, puis sa pochette en photo ou fichier JPEG/PNG. /annuler pour quitter.')
            return True
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                send('Un traitement de pochettes est en cours. Renvoyez l’image après sa fin.')
                return True
            applying = False
            try:
                source = json.loads(source)
                with tempfile.TemporaryDirectory(prefix='manual-cover-', dir=self.directory) as folder:
                    folder = Path(folder)
                    self.media.download(image['file_id'], folder/'image')
                    cover, thumb = images((folder/'image').read_bytes())
                    media = source.get('audio') or source['document']
                    self.media.download(media['file_id'], folder/'track.mp3')
                    embed_cover(folder/'track.mp3', cover)
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE manual_covers SET status='applying' WHERE owner=?", (owner,))
                    with self.store.lock, self.store.db:
                        key = self.store.db.execute('SELECT track_key FROM music WHERE group_id=? AND message_id=?', (self.group,mid)).fetchone()[0]
                        self.store.db.execute('INSERT INTO music_artwork VALUES (?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET track_key=excluded.track_key,jpeg=excluded.jpeg,source_url=excluded.source_url,updated=excluded.updated', (self.group,mid,key,cover,'manual:owner',time.time()))
                    applying = True
                    result = self.media.edit(self.group,mid,source,folder/'track.mp3',thumb)
                    if not isinstance(result,dict) or result.get('message_id') != mid:
                        raise RuntimeError('Modification non confirmée')
                    index_music(self.store,self.group,result)
                    with self.store.lock, self.store.db:
                        key = self.store.db.execute('SELECT track_key FROM music WHERE group_id=? AND message_id=?', (self.group,mid)).fetchone()[0]
                        self.store.db.execute('INSERT INTO music_artwork VALUES (?,?,?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET track_key=excluded.track_key,jpeg=excluded.jpeg,source_url=excluded.source_url,updated=excluded.updated', (self.group,mid,key,cover,'manual:owner',time.time()))
                        self.store.db.execute('DELETE FROM manual_covers WHERE owner=?', (owner,))
            except Exception:
                if applying:
                    with self.store.lock, self.store.db:
                        self.store.db.execute("UPDATE manual_covers SET status='uncertain' WHERE owner=?", (owner,))
                send('Résultat incertain : vérifiez le message dans le canal avant de recommencer avec /pochette.' if applying else 'Correction impossible : vérifiez le fichier et les droits du bot, puis renvoyez l’image. Aucun remplacement n’a été envoyé.')
                return True
        send('Pochette intégrée au MP3 et enregistrée ✅ Elle est protégée des recherches automatiques. Les fichiers temporaires ont été supprimés.')
        return True
