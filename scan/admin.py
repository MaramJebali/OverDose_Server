from django.contrib import admin
from .models import Scan


@admin.register(Scan)
class ScanAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "product", "created_at")
    list_filter = ("created_at",)
    autocomplete_fields = ("user", "product")
    search_fields = ("id", "user__email", "product__name")
