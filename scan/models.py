from django.conf import settings
from django.db import models


class Scan(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="scans",
        null=True,
        blank=True,
    )
    # NEW FIELD - Link to product
    product = models.ForeignKey(
        'products.Product',
        on_delete=models.SET_NULL,
        related_name="scans",
        null=True,
        blank=True,
    )
    image = models.ImageField(upload_to="scans/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Scan {self.id} - Product: {self.product.name if self.product else 'None'}"