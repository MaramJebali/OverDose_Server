from django.conf import settings
from django.db import models
import hashlib


class Product(models.Model):
    CATEGORY_FOOD = "food"
    CATEGORY_COSMETIC = "cosmetic"
    CATEGORY_UNKNOWN = "unknown"
    CATEGORY_CHOICES = [
        (CATEGORY_FOOD, "Food"),
        (CATEGORY_COSMETIC, "Cosmetic"),
        (CATEGORY_UNKNOWN, "Unknown"),
    ]

    EXTRACTION_LENS = "lens"
    EXTRACTION_BARCODE = "barcode"
    EXTRACTION_UNKNOWN = "unknown"
    EXTRACTION_CHOICES = [
        (EXTRACTION_LENS, "Lens"),
        (EXTRACTION_BARCODE, "Barcode"),
        (EXTRACTION_UNKNOWN, "Unknown"),
    ]

    name = models.CharField(max_length=255)
    brand = models.CharField(max_length=255)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="products",
        null=True,
        blank=True,
    )
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES)
    ingredients = models.JSONField(default=list, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    extraction_method = models.CharField(max_length=32, choices=EXTRACTION_CHOICES)
    
    # NEW FIELDS
    ingredients_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    investigation_report = models.JSONField(null=True, blank=True, default=dict)
    filtering_report = models.JSONField(null=True, blank=True, default=dict)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.brand} - {self.name}"
    
    def generate_ingredients_hash(self):
        """Generate a unique hash from sorted ingredients list"""
        if not self.ingredients:
            return None
        ingredients_str = ",".join(sorted([i.strip().lower() for i in self.ingredients if i]))
        return hashlib.md5(ingredients_str.encode()).hexdigest()
    
    def save(self, *args, **kwargs):
        if self.ingredients and not self.ingredients_hash:
            self.ingredients_hash = self.generate_ingredients_hash()
        super().save(*args, **kwargs)


class UserProductDecision(models.Model):
    DECISION_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("saved", "Saved for later"),
    ]
    
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="product_decisions"
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="user_decisions"
    )
    decision = models.CharField(max_length=20, choices=DECISION_CHOICES, default="pending")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ["user", "product"]
        ordering = ["-created_at"]
    
    def __str__(self):
        return f"{self.user.email} - {self.product.name} - {self.decision}"