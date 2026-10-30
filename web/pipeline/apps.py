from django.apps import AppConfig


class PipelineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pipeline"
    label = "pipeline"
    verbose_name = "Procesamiento documental"