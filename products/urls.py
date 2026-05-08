from django.urls import path
from .views import (
    ProductListCreateAPIView,
    UpdateProductDecisionView,
    ProductAnalysisView,
    UserProductsView,  # ADD THIS IMPORT
)

urlpatterns = [
    path("", ProductListCreateAPIView.as_view(), name="product-list-create"),
    path("my-products/", UserProductsView.as_view(), name="my-products"),  # ADD THIS LINE
    path("<int:pk>/decision/", UpdateProductDecisionView.as_view(), name="product-decision"),
    path("<int:pk>/analysis/", ProductAnalysisView.as_view(), name="product-analysis"),
]