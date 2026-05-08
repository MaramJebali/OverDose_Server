from rest_framework import serializers

from .models import Allergy, User, UserAllergy


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "id",
            "first_name",
            "last_name",
            "email",
            "age",
            "user_type",
            "gender",
            "date_of_birth",
            "notes",
            "ai_report",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "ai_report"]


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6)

    class Meta:
        model = User
        fields = [
            "id",
            "first_name",
            "last_name",
            "email",
            "age",
            "user_type",
            "gender",
            "date_of_birth",
            "notes",
            "password",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        password = validated_data.pop("password")
        return User.objects.create_user(password=password, **validated_data)


class AllergySerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergy
        fields = ["id", "name"]
        read_only_fields = ["id"]


class UserAllergySerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAllergy
        fields = ["id", "user", "allergy"]
        read_only_fields = ["id"]


class CurrentUserAllergyUpdateSerializer(serializers.Serializer):
    allergy_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=True)


class UserAIReportUpdateSerializer(serializers.Serializer):
    """Serializer for updating AI report"""
    report_data = serializers.JSONField(required=True)


class UserTypeUpdateSerializer(serializers.Serializer):
    """Serializer for updating user type"""
    user_type = serializers.ChoiceField(choices=User.USER_TYPE_CHOICES, required=True)