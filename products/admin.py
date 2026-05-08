from django.contrib import admin
from .models import Product, UserProductDecision


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "id", 
        "name", 
        "brand", 
        "category", 
        "extraction_method", 
        "has_investigation",
        "has_filtering",
        "created_at"
    )
    list_filter = ("category", "extraction_method")
    search_fields = ("name", "brand", "barcode", "ingredients_hash")
    readonly_fields = ("ingredients_hash", "created_at", "updated_at")
    
    fieldsets = (
        (None, {"fields": ("name", "brand", "category", "barcode")}),
        ("Ingredients", {"fields": ("ingredients", "ingredients_hash")}),
        ("AI Analysis", {"fields": ("investigation_report", "filtering_report")}),
        ("Metadata", {"fields": ("owner", "extraction_method", "created_at", "updated_at")}),
    )
    
    def has_investigation(self, obj):
        return obj.investigation_report not in (None, {}, [])
    has_investigation.boolean = True
    has_investigation.short_description = "Investigation"
    
    def has_filtering(self, obj):
        return obj.filtering_report not in (None, {}, [])
    has_filtering.boolean = True
    has_filtering.short_description = "Filtering"


@admin.register(UserProductDecision)
class UserProductDecisionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "product", "decision", "created_at")
    list_filter = ("decision", "created_at")
    search_fields = ("user__email", "product__name")
    autocomplete_fields = ("user", "product")
    readonly_fields = ("created_at", "updated_at")