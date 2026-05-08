from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Allergy, User, UserAllergy


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("-created_at",)
    list_display = (
        "id",
        "email",
        "first_name",
        "last_name",
        "age",
        "user_type",
        "gender",
        "date_of_birth",
        "is_staff",
        "created_at",
    )
    list_filter = ("gender", "user_type", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            "Personal info",
            {"fields": ("first_name", "last_name", "age", "user_type", "gender", "date_of_birth", "ai_report")},
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    readonly_fields = ("created_at", "updated_at", "last_login")
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "is_staff", "is_superuser"),
            },
        ),
    )


@admin.register(Allergy)
class AllergyAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(UserAllergy)
class UserAllergyAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "allergy")
    autocomplete_fields = ("user", "allergy")