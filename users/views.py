from django.contrib.auth import authenticate
from rest_framework import generics, status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Allergy, User, UserAllergy
from .serializers import (
    AllergySerializer,
    CurrentUserAllergyUpdateSerializer,
    UserAllergySerializer,
    UserRegistrationSerializer,
    UserSerializer,
    UserAIReportUpdateSerializer,
    UserTypeUpdateSerializer,
)


from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

class UserListCreateAPIView(generics.ListCreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class RegisterAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": UserSerializer(user).data}, status=status.HTTP_201_CREATED)


class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email", "")
        password = request.data.get("password", "")
        user = authenticate(request, email=email, password=password)
        if user is None:
            return Response({"detail": "Invalid email or password."}, status=status.HTTP_400_BAD_REQUEST)

        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": UserSerializer(user).data}, status=status.HTTP_200_OK)


class CurrentUserAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data, status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)


class CurrentUserAllergiesAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        allergies = Allergy.objects.filter(allergic_users__user=request.user).distinct().order_by("name")
        selected_ids = list(request.user.user_allergies.values_list("allergy_id", flat=True).order_by("allergy__name"))
        return Response(
            {
                "selected_ids": selected_ids,
                "allergies": AllergySerializer(allergies, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    def put(self, request):
        serializer = CurrentUserAllergyUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        allergy_ids = serializer.validated_data["allergy_ids"]
        allergies = list(Allergy.objects.filter(id__in=allergy_ids))
        if len(allergies) != len(set(allergy_ids)):
            return Response({"detail": "One or more allergy_ids are invalid."}, status=status.HTTP_400_BAD_REQUEST)

        UserAllergy.objects.filter(user=request.user).delete()
        UserAllergy.objects.bulk_create(
            [UserAllergy(user=request.user, allergy=allergy) for allergy in allergies],
            ignore_conflicts=True,
        )
        return self.get(request)

    def patch(self, request):
        return self.put(request)


class CurrentUserAIReportAPIView(APIView):
    """View for updating user's AI report"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get current user's AI report"""
        return Response({"ai_report": request.user.ai_report}, status=status.HTTP_200_OK)

    def put(self, request):
        """Update user's AI report"""
        serializer = UserAIReportUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report_data = serializer.validated_data["report_data"]
        
        # Update AI report
        updated_report = request.user.update_ai_report(report_data)
        
        return Response(
            {
                "message": "AI report updated successfully",
                "ai_report": updated_report
            },
            status=status.HTTP_200_OK
        )


class CurrentUserTypeAPIView(APIView):
    """View for updating user type"""
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        """Update user type"""
        serializer = UserTypeUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_type = serializer.validated_data["user_type"]
        
        request.user.user_type = user_type
        request.user.save(update_fields=["user_type", "updated_at"])
        
        return Response(
            {
                "message": "User type updated successfully",
                "user_type": request.user.user_type,
                "user": UserSerializer(request.user).data
            },
            status=status.HTTP_200_OK
        )


class AllergyListCreateAPIView(generics.ListCreateAPIView):
    queryset = Allergy.objects.all()
    serializer_class = AllergySerializer


class UserAllergyListCreateAPIView(generics.ListCreateAPIView):
    queryset = UserAllergy.objects.select_related("user", "allergy").all()
    serializer_class = UserAllergySerializer


from django.shortcuts import render

def test_cumulative(request):
    return render(request, 'test_cumulative.html')



class CumulativeSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        report = request.user.ai_report
        if not report or not isinstance(report, dict):
            return Response({"detail": "No cumulative report available."}, status=status.HTTP_404_NOT_FOUND)

        extracted = {
            "global_summary": report.get("global_summary"),
            "product_verdicts": report.get("product_verdicts"),
            "scoring_analysis": report.get("scoring_analysis"),
            "combination_risks": report.get("combination_risks"),
            "overall_assessment": report.get("overall_assessment"),
            "safe_ingredients": report.get("safe_ingredients"),
            "unverified_chemicals": report.get("unverified_chemicals"),
        }
        return Response(extracted)