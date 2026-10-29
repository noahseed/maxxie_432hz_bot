"""Point d'entrée isolé : seuls les modules installés par root sont importés."""
import sys
sys.path.insert(0, '/opt/maxxie-bot')
from vps_updates import main
main()
