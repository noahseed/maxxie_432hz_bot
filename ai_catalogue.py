"""Accès en lecture au seul catalogue de la destination configurée."""
from catalogue import artists_page, message_link, music_page, random_music

TOOL = {
    'type': 'function', 'name': 'catalogue',
    'description': 'Consulter le catalogue musical indexé du canal : rechercher des titres/artistes, lister les artistes, proposer un morceau au hasard ou obtenir les totaux. Recherche : mots significatifs du titre/artiste uniquement, tous les mots doivent correspondre. Ne pas inclure les mots de politesse. Pagination par 10 résultats.',
    'strict': True,
    'parameters': {'type': 'object', 'properties': {
        'operation': {'type': 'string', 'enum': ['search', 'artists', 'random', 'summary']},
        'query': {'type': 'string', 'description': 'Artiste ou titre à chercher, vide pour les autres opérations.'},
        'offset': {'type': 'integer', 'description': 'Décalage de pagination, 0 pour la première page.'}},
        'required': ['operation', 'query', 'offset'], 'additionalProperties': False}}


class CatalogueReader:
    def __init__(self, store, group_id, group_url):
        self.store, self.group_id, self.group_url = store, str(group_id), group_url

    def __call__(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) != {'operation', 'query', 'offset'}:
            return {'error': 'Arguments invalides.'}
        operation, query, offset = (arguments[key] for key in ('operation', 'query', 'offset'))
        if (not isinstance(query, str) or len(query) > 120 or type(offset) is not int
                or not 0 <= offset <= 1000000):
            return {'error': 'Recherche limitée à 120 caractères, décalage entre 0 et 1000000.'}
        if operation == 'summary':
            with self.store.lock:
                total, artists = self.store.db.execute(
                    'SELECT count(*), count(DISTINCT artist_key) FROM music WHERE group_id=?',
                    (self.group_id,)).fetchone()
            return {'tracks': total, 'artists': artists, 'scope': 'Catalogue indexé uniquement, pas tout l’historique Telegram.'}
        if operation == 'artists':
            rows, total = artists_page(self.store, self.group_id, offset=offset)
            return {'total': total, 'offset': offset,
                    'artists': [{'name': name[:200], 'tracks': count} for _, name, count in rows]}
        if operation == 'random':
            row = random_music(self.store, self.group_id)
            rows, total = ([row] if row else []), int(row is not None)
        elif operation == 'search':
            if len(query.strip()) < 2:
                return {'error': 'Indiquer au moins deux caractères pour une recherche.'}
            rows, total = music_page(self.store, self.group_id, 'search', query, offset=offset)
        else:
            return {'error': 'Opération inconnue.'}
        return {'total': total, 'offset': offset,
                'tracks': [{'label': label[:300],
                            'url': message_link(self.group_id, self.group_url, mid)} for mid, label in rows]}
