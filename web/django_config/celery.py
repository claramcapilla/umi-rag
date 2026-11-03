"""Configuración de Celery para umi-rag."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "django_config.settings")

app = Celery("django_config")

# Leer configuración CELERY_* desde settings.py.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Descubrir las tareas de las aplicaciones Django.
app.autodiscover_tasks()