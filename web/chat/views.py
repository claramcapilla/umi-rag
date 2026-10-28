import json

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from rag.service import create_pipeline

# El pipeline se carga una sola vez al arrancar Django
pipeline = create_pipeline()


@login_required
def index(request):
    return render(request, "chat/index.html", {"model": settings.VLLM_MODEL})


def clean_history(raw) -> list[dict] | None:
    """Valida el historial que envia el navegador y se queda con los ultimos mensajes."""
    if not isinstance(raw, list):
        return None
    history = [
        {"role": m["role"], "content": m["content"][: settings.MAX_MESSAGE_CHARS]}
        for m in raw
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ][-settings.MAX_HISTORY_MESSAGES :]
    # Tras recortar, la conversacion debe empezar por el usuario
    while history and history[0]["role"] != "user":
        history.pop(0)
    if not history or history[-1]["role"] != "user":
        return None
    return history


def answer_from_rag(question: str, history: list[dict]):
    """Genera una respuesta utilizando el pipeline RAG."""

    try:
        answer, _ = pipeline.answer(
            question=question,
            history=history,
        )
        yield answer

    except Exception as exc:
        print(f"Error en RAG: {exc}")
        yield (
            "\n\n[No se pudo generar la respuesta.]"
        )


@login_required
@require_POST
def chat_api(request):

    try:
        history = clean_history(
            json.loads(request.body).get("messages")
        )
    except (json.JSONDecodeError, AttributeError):
        history = None

    if history is None:
        return HttpResponseBadRequest(
            "Conversación no válida"
        )

    question = history[-1]["content"]
    previous_messages = history[:-1]

    return StreamingHttpResponse(
        answer_from_rag(question, previous_messages),
        content_type="text/plain; charset=utf-8",
    )
