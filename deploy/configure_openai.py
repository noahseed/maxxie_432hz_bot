"""Activation interactive : sudo python3 /opt/maxxie-bot/configure_openai.py."""
from getpass import getpass
import os
from pathlib import Path
import re
import tempfile


def main():
    if os.geteuid() != 0:
        raise SystemExit('Exécutez ce script avec sudo.')
    key = getpass('Clé API OpenAI (saisie masquée) : ').strip()
    model = input('Identifiant du modèle OpenAI autorisé dans votre projet : ').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', key) or not re.fullmatch(r'[A-Za-z0-9._:-]+', model):
        raise SystemExit('Clé ou identifiant de modèle invalide.')
    target = Path('/etc/maxxie-bot.env')
    lines = [line for line in target.read_text().splitlines()
             if not line.startswith(('OPENAI_API_KEY=', 'OPENAI_MODEL='))]
    fd, temporary = tempfile.mkstemp(prefix='.maxxie-openai-', dir='/etc')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(lines) + f'\nOPENAI_API_KEY={key}\nOPENAI_MODEL={model}\n')
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('Configuration enregistrée. Activez-la : sudo systemctl restart maxxie-bot')


if __name__ == '__main__':
    main()
