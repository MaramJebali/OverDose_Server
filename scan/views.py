# scan/views.py
import logging
import hashlib

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import get_user_model

from scan.legacy.config import get_settings
from scan.legacy.runtime import (
    analyze_image,
    analyze_selected_products,
    get_cloudinary_service,
    segment_image,
    should_cleanup_cloudinary,
)

from recommendation.views import build_mock_recommendations
from risk.services import (
    analyze_ingredients_risks,
    analyze_cumulative_risks,
    extract_filtering_report,
    extract_investigation_report
)

from products.models import Product, UserProductDecision

from .serializers import (
    AnalysisBatchResponseSerializer,
    AnalyzeSelectedRequestSerializer,
    ScanPipelineResponseSerializer,
    ScanSerializer,
    SegmentationRequestSerializer,
    SegmentationResponseSerializer,
)

from .models import Scan

User = get_user_model()
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

        # Store analysis results
        product_name = None
        product_brand = None
        product_barcode = None
        product_category = None

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
                product_name = analysis.name
                product_brand = analysis.brand
                product_barcode = analysis.barcode

                if hasattr(analysis, 'category') and analysis.category:
                    product_category = analysis.category
                else:
                    product_category = "unknown"

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
            except Exception:
                logger.exception("Real scan pipeline failed for scan_id=%s", scan.id)
                ingredients = []

        product = None
        user_decision = "pending"
        agent_debug_log = None
        saved_report_path = None

        if ingredients:
            # Generate ingredients hash for lookup
            ingredients_str = ",".join(sorted([i.strip().lower() for i in ingredients if i]))
            ingredients_hash = hashlib.md5(ingredients_str.encode()).hexdigest()

            # Try to find existing product by barcode first, then by ingredients hash
            existing_product = None

            if product_barcode:
                existing_product = Product.objects.filter(barcode=product_barcode).first()

            if not existing_product and ingredients_hash:
                existing_product = Product.objects.filter(ingredients_hash=ingredients_hash).first()

            if existing_product:
                product = existing_product
                logger.info(f"Product found: {product.id} - {product.name}")

                # Check if investigation report is empty
                if not product.investigation_report or product.investigation_report == {}:
                    logger.info(f"Product {product.id} has empty investigation_report, refilling...")
                    user_type = request.user.user_type if request.user.is_authenticated else None
                    risk_items, full_agent_report, debug_log, file_path = analyze_ingredients_risks(
                        ingredients,
                        user_type=user_type,
                        user_id=request.user.id if request.user.is_authenticated else None,
                        product_id=product.id
                    )
                    agent_debug_log = debug_log
                    saved_report_path = file_path

                    # Extract filtering and investigation parts
                    filtering_data = extract_filtering_report(full_agent_report)
                    investigation_data = extract_investigation_report(full_agent_report)

                    product.filtering_report = filtering_data
                    product.investigation_report = investigation_data
                    product.save(update_fields=["filtering_report", "investigation_report", "updated_at"])
                else:
                    risk_items = []
                    full_agent_report = product.investigation_report
                    logger.info(f"Using cached investigation_report for product {product.id}")
            else:
                # Product not found - create new
                logger.info(f"Product not found, creating new with hash: {ingredients_hash}")

                extraction_method = "unknown"
                if analysis_payload:
                    if analysis_payload.get("source") == "barcode":
                        extraction_method = "barcode"
                    elif analysis_payload.get("source") == "lens":
                        extraction_method = "lens"

                product = Product.objects.create(
                    name=product_name or "Unknown Product",
                    brand=product_brand or "Unknown Brand",
                    category=product_category or "unknown",
                    ingredients=ingredients,
                    barcode=product_barcode or "",
                    extraction_method=extraction_method,
                    ingredients_hash=ingredients_hash,
                    owner=request.user if request.user.is_authenticated else None,
                )

                user_type = request.user.user_type if request.user.is_authenticated else None
                risk_items, full_agent_report, debug_log, file_path = analyze_ingredients_risks(
                    ingredients,
                    user_type=user_type,
                    user_id=request.user.id if request.user.is_authenticated else None,
                    product_id=product.id
                )
                agent_debug_log = debug_log
                saved_report_path = file_path

                # Extract filtering and investigation parts
                filtering_data = extract_filtering_report(full_agent_report)
                investigation_data = extract_investigation_report(full_agent_report)

                product.filtering_report = filtering_data
                product.investigation_report = investigation_data
                product.save(update_fields=["filtering_report", "investigation_report", "updated_at"])

            # Link scan to product
            scan.product = product
            scan.save(update_fields=["product"])

            # Create or update user decision
            if request.user.is_authenticated:
                user_decision_obj, created = UserProductDecision.objects.get_or_create(
                    user=request.user,
                    product=product,
                    defaults={"decision": "pending"}
                )
                user_decision = user_decision_obj.decision

            # --- CUMULATIVE ANALYSIS (Phases C, D, E) ---
            cumulative_report = None
            if request.user.is_authenticated:
                try:
                    approved_saved = UserProductDecision.objects.filter(
                        user=request.user,
                        decision__in=['approved', 'saved']
                    ).select_related('product').order_by('-updated_at')[:10]

                    products_for_cumulative = []

                    for dec in approved_saved:
                        p = dec.product
                        if p.investigation_report and isinstance(p.investigation_report, dict):
                            products_for_cumulative.append({
                                "product_id": str(p.id),
                                "product_name": p.name,
                                "product_usage": p.category,
                                "exposure_type": "skin" if p.category == "cosmetic" else "ingestion",
                                "ingredient_list": [{"name": ing} for ing in p.ingredients],
                                "investigation_report": p.investigation_report,
                            })

                    if product and product.investigation_report:
                        products_for_cumulative.append({
                            "product_id": str(product.id),
                            "product_name": product.name,
                            "product_usage": product.category,
                            "exposure_type": "skin" if product.category == "cosmetic" else "ingestion",
                            "ingredient_list": [{"name": ing} for ing in product.ingredients],
                            "investigation_report": product.investigation_report,
                        })

                    if len(products_for_cumulative) >= 2:
                        cumulative_report = analyze_cumulative_risks(
                            products_for_cumulative,
                            user_type=request.user.user_type if hasattr(request.user, 'user_type') else None,
                            timeout_seconds=120
                        )
                        request.user.ai_report = cumulative_report
                        request.user.save(update_fields=['ai_report', 'updated_at'])
                    else:
                        cumulative_report = {"info": "Only one product in user's list, cumulative analysis skipped."}

                except Exception as e:
                    logger.error(f"Cumulative analysis failed: {e}", exc_info=True)
                    cumulative_report = {"error": f"Cumulative analysis failed: {str(e)}"}

            # Extract risks from investigation_report if not already set
            if product.investigation_report and not risk_items:
                try:
                    report = product.investigation_report
                    if isinstance(report, dict) and 'ingredients' in report:
                        for chem in report.get('ingredients', {}).get('chemicals_evaluated', []):
                            name = chem.get('name')
                            danger = chem.get('verdict', {}).get('danger_level', 'UNKNOWN')
                            if danger in ("CRITICAL", "HIGH"):
                                level = "high"
                            elif danger == "MODERATE":
                                level = "medium"
                            else:
                                level = "low"
                            risk_items.append({"ingredient": name, "level": level})
                except Exception as e:
                    logger.warning(f"Could not extract risks from report: {e}")
        else:
            # No ingredients extracted, use mock risks
            risk_items, full_agent_report, debug_log, file_path = analyze_ingredients_risks([])
            agent_debug_log = debug_log
            saved_report_path = file_path

        # Keep mock recommendations
        recommendation_result = build_mock_recommendations(scan.id, risk_items)

        payload = {
            "scan_id": scan.id,
            "product_id": product.id if product else None,
            "ingredients": ingredients,
            "risks": risk_items,
            "recommendations": recommendation_result["recommendations"],
            "user_decision": user_decision,
            "cumulative_report": cumulative_report if 'cumulative_report' in locals() else None,
            "agent_debug_log": agent_debug_log,
            "saved_report_path": saved_report_path,
        }
        if analysis_payload is not None:
            payload["analysis"] = analysis_payload

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