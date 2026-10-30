import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "chat",
    "library.apps.LibraryConfig",
    "documents.apps.DocumentsConfig",
    "simple_history",
    "pipeline.apps.PipelineConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "chatbot.urls"
WSGI_APPLICATION = "chatbot.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"},
}

LANGUAGE_CODE = "es-es"
TIME_ZONE = "Europe/Madrid"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "chat"
LOGOUT_REDIRECT_URL = "login"

# --- Modelo servido con vLLM ---
VLLM_URL = os.environ.get("VLLM_URL", "http://compute01:8000")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "qwen3.8-27b-nvfp4a16")
SYSTEM_PROMPT = os.environ.get(
    "CHAT_SYSTEM_PROMPT",
    "Actúa como asistente clínico para profesionales sanitarios. Da recomendaciones breves, "
    "prácticas y directamente aplicables, sin bibliografía, DOI ni avisos legales.",
)
MAX_HISTORY_MESSAGES = 20      # mensajes previos que se reenvian al modelo
MAX_MESSAGE_CHARS = 8000       # longitud maxima de cada mensaje
MAX_TOKENS = 1024              # longitud maxima de cada respuesta
TEMPERATURE = 0.3


# --- Ollama: generación de JSON clínico (docsrepo) ---

LLM_API_KEY = "ollama"

LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL",
    "http://127.0.0.1:11434/v1"
)

LLM_ALLOWED_ENDPOINTS = [
    "http://127.0.0.1:11434/v1"
]

LLM_JSON_MODEL = os.environ.get(
    "LLM_JSON_MODEL",
    "qwen2.5:7b"
)

LLM_REASONING = ""
LLM_SERVICE_TIER = ""
LLM_TOKENS_PARAM = "max_tokens"

LLM_WHOLE_DOCUMENT = False
LLM_CONTEXT_TOKENS = 0
LLM_TABLE_GRID = False
LLM_MAX_PARALLEL_WORKERS = 1

# Celery y Redis
CELERY_BROKER_URL = "redis://127.0.0.1:6379/0"
CELERY_RESULT_BACKEND = "redis://127.0.0.1:6379/1"

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]

CELERY_TIMEZONE = "Europe/Madrid"

# Activar la indexación automática cuando termine el procesamiento.
# De momento la dejamos desactivada hasta probar Qdrant.
INGEST_INDEXING_ENABLED = False