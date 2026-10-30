from __future__ import annotations

from django.contrib import admin

from .models import Collection, Tag


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "is_specialty_root")
    list_filter = ("is_specialty_root",)
    search_fields = ("name",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    search_fields = ("name",)
