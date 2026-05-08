from rest_framework import serializers
from .models import Product, UserProductDecision


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "brand",
            "owner",
            "category",
            "ingredients",
            "barcode",
            "extraction_method",
            "ingredients_hash",
            "investigation_report",
            "filtering_report",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "owner", "ingredients_hash", "created_at", "updated_at"]


class UserProductDecisionSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    
    class Meta:
        model = UserProductDecision
        fields = [
            "id",
            "user",
            "user_email",
            "product",
            "product_name",
            "decision",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ProductDecisionUpdateSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approved", "rejected", "saved"])
    notes = serializers.CharField(required=False, allow_blank=True)


class ProductAnalysisResponseSerializer(serializers.Serializer):
    product = ProductSerializer()
    user_decision = UserProductDecisionSerializer()
    scan_images = serializers.ListField(child=serializers.URLField(), required=False)