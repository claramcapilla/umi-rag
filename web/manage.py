#!/usr/bin/env python
import os
import sys
from pathlib import Path

# Directorio web/
WEB_DIR = Path(__file__).resolve().parent

# Raíz
PROJECT_ROOT = WEB_DIR.parent

# Permite importar módulos
sys.path.insert(0, str(PROJECT_ROOT))

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "chatbot.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
