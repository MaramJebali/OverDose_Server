from rest_framework import serializers
from .models import Scan


class ScanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Scan
        fields = ["id", "user", "product", "image", "created_at"]
        read_only_fields = ["id", "created_at"]


class ScanPipelineAnalysisSerializer(serializers.Serializer):
    source = serializers.CharField()
    confidence = serializers.FloatField()
    category = serializers.CharField()
    name = serializers.CharField()
    brand = serializers.CharField(required=False, allow_null=True)
    barcode = serializers.CharField(required=False, allow_null=True)
    lens_title = serializers.CharField(required=False, allow_null=True)
    debug = serializers.DictField()


class ScanPipelineResponseSerializer(serializers.Serializer):
    scan_id = serializers.IntegerField()
    product_id = serializers.IntegerField(required=False, allow_null=True)
    ingredients = serializers.ListField(child=serializers.CharField())
    risks = serializers.ListField(child=serializers.DictField())
    recommendations = serializers.ListField(child=serializers.DictField())
    analysis = ScanPipelineAnalysisSerializer(required=False)
    user_decision = serializers.CharField(required=False, allow_null=True)
    cumulative_report = serializers.DictField(required=False, allow_null=True)
    agent_debug_log = serializers.ListField(child=serializers.CharField(), required=False, allow_null=True)
    saved_report_path = serializers.CharField(required=False, allow_null=True)


class SegmentationRequestSerializer(serializers.Serializer):
    segmentation_mode = serializers.ChoiceField(choices=["auto", "single", "multi"], default="auto")
    expected_products = serializers.IntegerField(required=False, min_value=1)


class BBoxSerializer(serializers.Serializer):
    x = serializers.IntegerField()
    y = serializers.IntegerField()
    width = serializers.IntegerField()
    height = serializers.IntegerField()


class SegmentedProductSerializer(serializers.Serializer):
    product_id = serializers.CharField()
    label = serializers.CharField()
    confidence = serializers.FloatField()
    bbox = BBoxSerializer()
    crop_url = serializers.CharField()


class SegmentationResponseSerializer(serializers.Serializer):
    session_id = serializers.CharField()
    segmentation_mode = serializers.CharField()
    expected_products = serializers.IntegerField(required=False, allow_null=True)
    total_products = serializers.IntegerField()
    products = SegmentedProductSerializer(many=True)


class AnalyzeSelectedRequestSerializer(serializers.Serializer):
    session_id = serializers.CharField()
    product_ids = serializers.ListField(child=serializers.CharField(), allow_empty=False)


class AnalysisBatchResponseSerializer(serializers.Serializer):
    session_id = serializers.CharField()
    analyzed_count = serializers.IntegerField()
    results = serializers.ListField(child=serializers.DictField())