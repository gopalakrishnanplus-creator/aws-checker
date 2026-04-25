from django.urls import path

from . import views

app_name = "checker"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("resources/<int:pk>/", views.resource_detail, name="resource-detail"),
    path("runs/", views.run_history, name="run-history"),
    path("runs/<int:pk>/", views.run_detail, name="run-detail"),
    path("actions/run/all/", views.run_all, name="run-all"),
    path("actions/run/service/<str:service_type>/", views.run_service, name="run-service"),
    path("actions/run/resource/<int:pk>/", views.run_resource, name="run-resource"),
]
