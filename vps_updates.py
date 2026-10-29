"""Maintenance APT : exécutée uniquement par les services root dédiés."""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from bot import Telegram

STATE = Path('/var/lib/maxxie-vps')
REQUEST = Path('/var/lib/maxxie-bot/vps-update-request')
CHECK_REQUEST = Path('/var/lib/maxxie-bot/vps-check-request')
REBOOT_REQUIRED = Path('/var/run/reboot-required')


def request_upgrade(token, reboot=False):
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        return False
    publish_request(REQUEST, ('reboot:' if reboot else '') + token)
    return True


def request_check():
    publish_request(CHECK_REQUEST, 'check')


def publish_request(path, content):
    # Atomic publication; no command or path is accepted from Telegram.
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
    os.replace(temporary, path)


def apt(*args):
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C',
           'DEBIAN_FRONTEND': 'noninteractive', 'APT_LISTCHANGES_FRONTEND': 'none'}
    result = subprocess.run(['/usr/bin/apt-get', '-o', 'DPkg::Lock::Timeout=120', *args],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=env, check=False)
    (STATE / 'last-apt.log').write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f'APT a échoué (code {result.returncode}). Consulter journalctl et /var/lib/maxxie-vps/last-apt.log.')
    return result.stdout


def packages():
    output = apt('--simulate', '--with-new-pkgs', 'upgrade')
    return [line.split()[1] for line in output.splitlines() if line.startswith('Inst ')]


def save(state):
    temporary = STATE / 'state.tmp'
    temporary.write_text(json.dumps(state))
    temporary.replace(STATE / 'state.json')


def queue(state, text, keyboard=None):
    message = {'text': text}
    if keyboard:
        reboot = keyboard == 'reboot'
        message['reply_markup'] = {'inline_keyboard': [[{
            'text': 'Redémarrer le VPS' if reboot else 'Mettre le VPS à jour',
            'callback_data': ('vps-reboot:' + state['reboot_token']) if reboot else 'vps:' + state['token']}]]}
    state.setdefault('outbox', []).append(message)
    save(state)


def take_request(path=None):
    path = REQUEST if path is None else path
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ''
    except OSError:
        path.unlink(missing_ok=True)
        return ''
    try:
        with os.fdopen(fd) as stream:
            token = stream.read(128)
    finally:
        path.unlink(missing_ok=True)
    return token


def process_reboot(state, token):
    if (not token or token != state.get('reboot_token')
            or time.time() > state.get('reboot_expires', 0)):
        return
    state.pop('reboot_token', None)
    save(state)  # A reboot request must never be replayed after a restart.
    if not REBOOT_REQUIRED.exists():
        queue(state, "Le VPS ne signale plus de redémarrage nécessaire. Aucun redémarrage lancé.")
        return
    try:
        subprocess.run(['/usr/sbin/shutdown', '-r', '+1'], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError):
        queue(state, "Échec de la programmation du redémarrage du VPS. Vérifiez le service de maintenance.")
        return
    queue(state, "Redémarrage du VPS programmé dans une minute. Le bot sera temporairement indisponible.")


def process(state, mode):
    if mode == 'request':
        token = take_request()
        if not token:
            if take_request(CHECK_REQUEST) != 'check':
                return
            process(state, 'manual-check')
            return
        if token.startswith('reboot:'):
            process_reboot(state, token[7:])
            return
        if not token or token != state.get('token') or time.time() > state.get('expires', 0):
            return
        state.pop('token', None)  # Consume before executing; never replay an upgrade.
        state['running'] = True
        save(state)
    try:
        if mode == 'request':
            apt('--error-on=any', 'update')
            before = packages()
            apt('-y', '--with-new-pkgs', '-o', 'Dpkg::Options::=--force-confold', 'upgrade')
            remaining = packages()
            reboot = REBOOT_REQUIRED.exists()
            state.pop('reboot_token', None)
            if reboot:
                state['reboot_token'] = secrets.token_hex(16)
                state['reboot_expires'] = time.time() + 86400
            queue(state, 'Mise à jour du VPS terminée.\n'
                  f'Paquets proposés avant opération : {len(before)}.\n'
                  + ', '.join(before)[:2000] + '\n'
                  + f'Paquets encore à mettre à jour : {len(remaining)}.\n'
                  + ('Redémarrage nécessaire.' if reboot else
                     'Aucun redémarrage signalé par le système.'),
                  keyboard='reboot' if reboot else None)
            state.pop('running', None)
            save(state)
        else:
            apt('--error-on=any', 'update')
            available = packages()
            state.pop('token', None)
            if available:
                state['token'] = secrets.token_hex(16)
                state['expires'] = time.time() + 86400
                queue(state, f'{len(available)} paquet(s) à mettre à jour sur le VPS.\n'
                      + ', '.join(available)[:2800], keyboard=True)
            elif mode == 'manual-check':
                queue(state, 'Vérification terminée : le VPS est à jour. Aucune mise à jour disponible.')
            save(state)
    except (RuntimeError, OSError) as error:
        state.pop('running', None)
        queue(state, f'Échec de la maintenance du VPS : {error}')


def main():
    owner = os.environ.get('OWNER_USER_ID', '').strip() or os.environ.get('NOTIFY_CHAT_ID', '').strip()
    if not owner.isdigit() or int(owner) <= 0:
        raise SystemExit('Configurer OWNER_USER_ID avec votre identifiant privé positif.')
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = STATE / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        if state.pop('running', False):
            queue(state, 'Maintenance interrompue : résultat incertain. Vérifiez APT sur le VPS avant de relancer.')
        process(state, sys.argv[1])
        api = Telegram(os.environ['BOT_TOKEN'], ipv4_only=os.environ.get('TELEGRAM_IPV4_ONLY', '1') == '1')
        try:
            while state.get('outbox'):
                api.call('sendMessage', chat_id=int(owner), **state['outbox'][0])
                state['outbox'].pop(0)
                save(state)
        finally:
            api.close()


if __name__ == '__main__':
    main()
