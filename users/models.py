from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    # User type choices
    USER_TYPE_ASTHMA = "asthma"
    USER_TYPE_DIABETES = "diabetes"
    USER_TYPE_NEWBORN = "newborn"
    USER_TYPE_FETAL = "fetal"
    USER_TYPE_CARDIOVASCULAR = "cardiovascular"
    USER_TYPE_PCOS = "pcos"

    USER_TYPE_CHOICES = [
        (USER_TYPE_ASTHMA, "Asthma"),
        (USER_TYPE_DIABETES, "Diabetes"),
        (USER_TYPE_NEWBORN, "Newborn"),
        (USER_TYPE_FETAL, "Fetal"),
        (USER_TYPE_CARDIOVASCULAR, "Cardiovascular"),
        (USER_TYPE_PCOS, "PCOS"),
    ]

    GENDER_MALE = "male"
    GENDER_FEMALE = "female"
    GENDER_OTHER = "other"
    GENDER_PREFER_NOT_TO_SAY = "prefer_not_to_say"

    GENDER_CHOICES = [
        (GENDER_MALE, "Male"),
        (GENDER_FEMALE, "Female"),
        (GENDER_OTHER, "Other"),
        (GENDER_PREFER_NOT_TO_SAY, "Prefer not to say"),
    ]

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    age = models.PositiveIntegerField(null=True, blank=True)  # Added age field
    user_type = models.CharField(max_length=32, choices=USER_TYPE_CHOICES, blank=True)  # Added user_type field
    gender = models.CharField(max_length=32, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    ai_report = models.JSONField(null=True, blank=True, default=dict)  # Added AI report field
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.email

    def update_ai_report(self, report_data):
        """Method to update AI report"""
        self.ai_report = report_data
        self.save(update_fields=["ai_report", "updated_at"])
        return self.ai_report


class Allergy(models.Model):
    name = models.CharField(max_length=255, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class UserAllergy(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_allergies")
    allergy = models.ForeignKey(Allergy, on_delete=models.CASCADE, related_name="allergic_users")

    class Meta:
        unique_together = ("user", "allergy")
        ordering = ["user_id", "allergy_id"]

    def __str__(self):
        return f"{self.user.email} - {self.allergy.name}"