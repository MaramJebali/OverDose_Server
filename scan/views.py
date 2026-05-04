# scan/views.py
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from scan.legacy.config import get_settings
from scan.legacy.runtime import (
    analyze_image,
    analyze_selected_products,
    get_cloudinary_service,
    segment_image,
    should_cleanup_cloudinary,
)

from recommendation.views import build_mock_recommendations
# Replace mock risks with real agent
from risk.services import analyze_ingredients_risks

from .serializers import (
    AnalysisBatchResponseSerializer,
    AnalyzeSelectedRequestSerializer,
    ScanPipelineResponseSerializer,
    ScanSerializer,
    SegmentationRequestSerializer,
    SegmentationResponseSerializer,
)


logger = logging.getLogger(__name__)


class ScanPipelineAPIView(APIView):
    def post(self, request):
        if "image" not in request.FILES:
            return Response({"detail": "image is required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scan = serializer.save()

        image_path = scan.image.path if scan.image else None
        image_url = scan.image.url if scan.image else None
        ingredients: list[str] = []
        analysis_payload: dict[str, object] | None = None
        cloudinary_public_id: str | None = None
        settings = get_settings()
        cloudinary_service = get_cloudinary_service()
        cloudinary_ready, _ = cloudinary_service.get_readiness()
        cloudinary_folder = settings.cloudinary_folder or None

        if image_path:
            try:
                if cloudinary_ready:
                    upload_result = cloudinary_service.upload_file(
                        image_path,
                        folder=cloudinary_folder,
                    )
                    if upload_result:
                        image_url = upload_result.url
                        cloudinary_public_id = upload_result.public_id
                    else:
                        logger.warning("[CLOUDINARY] Upload failed for scan_id=%s", scan.id)

                analysis = analyze_image(
                    scan_id=scan.id,
                    image_path=image_path,
                    image_url=image_url,
                )
                if cloudinary_public_id and should_cleanup_cloudinary(analysis.debug):
                    cloudinary_service.destroy(cloudinary_public_id)
                ingredients = analysis.ingredients
                analysis_payload = {
                    "source": analysis.source,
                    "confidence": analysis.confidence,
                    "category": analysis.category,
                    "name": analysis.name,
                    "brand": analysis.brand,
                    "barcode": analysis.barcode,
                    "lens_title": analysis.lens_title,
                    "debug": analysis.debug,
                }
            except Exception as exc:
                logger.exception("Real scan pipeline failed for scan_id=%s", scan.id)
                ingredients = []
                analysis_payload = {
                    "source": "failed",
                    "confidence": 0.0,
                    "category": "unknown",
                    "name": "unknown",
                    "brand": None,
                    "barcode": None,
                    "lens_title": None,
                    "debug": {"error": str(exc)},
                }

        # Use real agent for risk analysis
        risk_items, full_agent_report = analyze_ingredients_risks(ingredients)
        # Keep mock recommendations for now (can be replaced later)
        recommendation_result = build_mock_recommendations(scan.id, risk_items)

        payload = {
            "scan_id": scan.id,
            "ingredients": ingredients,
            "risks": risk_items,   # real risks from agent
            "recommendations": recommendation_result["recommendations"],
        }
        if analysis_payload is not None:
            payload["analysis"] = analysis_payload
        # Optionally include full agent report for debugging
        # payload["agent_report"] = full_agent_report

        response_serializer = ScanPipelineResponseSerializer(data=payload)
        response_serializer.is_valid(raise_exception=True)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class ScanSegmentationAPIView(APIView):
    def post(self, request):
        image = request.FILES.get("image")
        if image is None:
            return Response({"detail": "image is required."}, status=status.HTTP_400_BAD_REQUEST)

        content_type = getattr(image, "content_type", "") or ""
        if not content_type.startswith("image/"):
            return Response({"detail": "File must be an image"}, status=status.HTTP_400_BAD_REQUEST)

        input_serializer = SegmentationRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        mode = str(data.get("segmentation_mode", "auto")).strip().lower()
        expected_products = data.get("expected_products")

        try:
            session_id, products = segment_image(
                image_bytes=image.read(),
                filename=getattr(image, "name", "upload.jpg"),
                segmentation_mode=mode,
                expected_products=expected_products,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        payload = {
            "session_id": session_id,
            "segmentation_mode": mode,
            "expected_products": expected_products,
            "total_products": len(products),
            "products": products,
        }
        output_serializer = SegmentationResponseSerializer(data=payload)
        output_serializer.is_valid(raise_exception=True)
        return Response(output_serializer.data, status=status.HTTP_200_OK)


class ScanSelectedAnalysisAPIView(APIView):
    def post(self, request):
        serializer = AnalyzeSelectedRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        session_id = data["session_id"]
        product_ids = data["product_ids"]

        try:
            results = analyze_selected_products(session_id=session_id, product_ids=product_ids)
        except LookupError:
            return Response({"detail": "Session not found"}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        payload = {
            "session_id": session_id,
            "analyzed_count": len(results),
            "results": results,
        }
        output_serializer = AnalysisBatchResponseSerializer(data=payload)
        output_serializer.is_valid(raise_exception=True)
        return Response(output_serializer.data, status=status.HTTP_200_OK)