from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticatedOrReadOnly, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Product, UserProductDecision
from .serializers import (
    ProductSerializer, 
    UserProductDecisionSerializer,
    ProductDecisionUpdateSerializer,
    ProductAnalysisResponseSerializer
)


class ProductListCreateAPIView(generics.ListCreateAPIView):
    queryset = Product.objects.select_related("owner").all()
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_authenticated:
            return queryset.filter(owner=user) | queryset.filter(owner__isnull=True)
        return queryset

    def perform_create(self, serializer):
        owner = self.request.user if self.request.user.is_authenticated else None
        serializer.save(owner=owner)


class UpdateProductDecisionView(APIView):
    """PATCH endpoint for user to approve/reject a product"""
    permission_classes = [IsAuthenticated]
    
    def patch(self, request, pk):
        try:
            product = Product.objects.get(pk=pk)
        except Product.DoesNotExist:
            return Response(
                {"detail": "Product not found."},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = ProductDecisionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        decision_data = serializer.validated_data
        decision_value = decision_data["decision"]
        notes = decision_data.get("notes", "")
        
        # Get or create user decision
        user_decision, created = UserProductDecision.objects.get_or_create(
            user=request.user,
            product=product,
            defaults={"decision": decision_value, "notes": notes}
        )
        
        # Update if not created
        if not created:
            user_decision.decision = decision_value
            user_decision.notes = notes
            user_decision.save()
        
        return Response(
            {
                "message": f"Product {decision_value} successfully.",
                "decision": UserProductDecisionSerializer(user_decision).data
            },
            status=status.HTTP_200_OK
        )


class ProductAnalysisView(APIView):
    """GET endpoint to get product analysis with user decision"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request, pk):
        try:
            product = Product.objects.get(pk=pk)
        except Product.DoesNotExist:
            return Response(
                {"detail": "Product not found."},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get user decision for this product
        user_decision = UserProductDecision.objects.filter(
            user=request.user,
            product=product
        ).first()
        
        # Get all scan images for this product
        scan_images = []
        if hasattr(product, 'scans'):
            scan_images = [scan.image.url for scan in product.scans.all() if scan.image]
        
        response_data = {
            "product": ProductSerializer(product).data,
            "user_decision": UserProductDecisionSerializer(user_decision).data if user_decision else None,
            "scan_images": scan_images
        }
        
        return Response(response_data, status=status.HTTP_200_OK)


class UserProductsView(generics.ListAPIView):
    """Get user's products with decision info - filter by decision type"""
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        status_filter = self.request.query_params.get('status', 'active')
        
        if status_filter == 'all':
            decisions = ['approved', 'saved', 'pending', 'rejected']
        elif status_filter == 'approved':
            decisions = ['approved']
        elif status_filter == 'saved':
            decisions = ['saved']
        elif status_filter == 'pending':
            decisions = ['pending']
        elif status_filter == 'rejected':
            decisions = ['rejected']
        else:  # 'active' - default
            decisions = ['approved', 'saved']
        
        product_ids = UserProductDecision.objects.filter(
            user=user,
            decision__in=decisions
        ).values_list('product_id', flat=True)
        
        return Product.objects.filter(id__in=product_ids)
    
    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        
        user_decisions = UserProductDecision.objects.filter(user=request.user)
        
        decision_dict = {
            d.product_id: {
                'decision': d.decision,
                'notes': d.notes,
                'updated_at': d.updated_at
            }
            for d in user_decisions
        }
        
        products_with_decisions = []
        for product_data in serializer.data:
            product_id = product_data['id']
            if product_id in decision_dict:
                product_data['user_decision'] = decision_dict[product_id]['decision']
                product_data['user_decision_notes'] = decision_dict[product_id]['notes']
            else:
                product_data['user_decision'] = None
                product_data['user_decision_notes'] = ""
            products_with_decisions.append(product_data)
        
        counts = {
            'approved': user_decisions.filter(decision='approved').count(),
            'saved': user_decisions.filter(decision='saved').count(),
            'pending': user_decisions.filter(decision='pending').count(),
            'rejected': user_decisions.filter(decision='rejected').count(),
            'total': user_decisions.count(),
        }
        
        return Response({
            'products': products_with_decisions,
            'counts': counts
        })