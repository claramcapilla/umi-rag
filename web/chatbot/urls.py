from django.contrib import admin
from django.urls import include, path

from chat import views

urlpatterns = [
    path("", views.index, name="chat"),
    path("api/chat/", views.chat_api, name="chat_api"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
]
