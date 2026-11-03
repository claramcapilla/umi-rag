"""Control de acceso de la app documental."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponseBase


class StaffRequiredMiddleware(LoginRequiredMiddleware):
    """Exige sesión **y** `is_staff` en toda la app.

    `LoginRequiredMiddleware` solo comprueba autenticación. El único filtro de rol que
    existía era que `AdminAuthenticationForm` rechaza a los no-staff: un control prestado
    del formulario del admin, que desaparecería al añadir login propio, SSO o rutas DRF.
    Aquí queda explícito y cubierto por tests, para que el contrato de acceso no dependa
    de por dónde se entra.

    El anónimo se redirige al login (lo hace la clase base); el autenticado sin `is_staff`
    recibe 403, porque redirigirlo al login no le serviría de nada.
    """

    def process_view(
        self,
        request: HttpRequest,
        view_func: Any,
        view_args: Any,
        view_kwargs: Any,
    ) -> HttpResponseBase | None:
        response = super().process_view(request, view_func, view_args, view_kwargs)
        if response is not None:
            return response
        if not getattr(view_func, "login_required", True):
            return None
        if request.user.is_staff:
            return None
        raise PermissionDenied
