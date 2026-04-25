from collections import defaultdict

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import CheckDefinition, CheckResult, CheckRun, ManagedResource
from .services.check_runner import CheckRunner


def dashboard(request):
    resources_by_service = []
    counts = (
        ManagedResource.objects.filter(is_active=True)
        .values("service_type")
        .annotate(total=Count("id"))
    )
    count_map = {row["service_type"]: row["total"] for row in counts}

    for service_type, label in ManagedResource.ServiceType.choices:
        resources = list(
            ManagedResource.objects.filter(service_type=service_type, is_active=True).order_by("name")
        )
        resources_by_service.append(
            {
                "service_type": service_type,
                "label": label,
                "count": count_map.get(service_type, 0),
                "resources": resources,
            }
        )

    context = {
        "resource_total": ManagedResource.objects.filter(is_active=True).count(),
        "resources_by_service": resources_by_service,
        "recent_runs": CheckRun.objects.select_related("resource").all()[:10],
    }
    return render(request, "checker/dashboard.html", context)


def resource_detail(request, pk):
    resource = get_object_or_404(ManagedResource, pk=pk)
    definitions = list(
        CheckDefinition.objects.filter(service_type=resource.service_type, is_active=True).order_by(
            "sort_order", "id"
        )
    )
    recent_results = (
        CheckResult.objects.filter(resource=resource)
        .select_related("check_definition", "run")
        .order_by("check_definition_id", "-created_at")
    )
    latest_by_definition = {}
    for result in recent_results:
        latest_by_definition.setdefault(result.check_definition_id, result)

    checklist_rows = [
        {"definition": definition, "latest_result": latest_by_definition.get(definition.id)}
        for definition in definitions
    ]

    context = {
        "resource": resource,
        "checklist_rows": checklist_rows,
        "recent_runs": CheckRun.objects.filter(results__resource=resource)
        .distinct()
        .order_by("-started_at")[:8],
    }
    return render(request, "checker/resource_detail.html", context)


def run_history(request):
    runs = CheckRun.objects.select_related("resource").all()[:50]
    return render(request, "checker/run_history.html", {"runs": runs})


def run_detail(request, pk):
    run = get_object_or_404(CheckRun.objects.select_related("resource"), pk=pk)
    grouped_results = defaultdict(list)
    for result in run.results.select_related("resource", "check_definition").all():
        key = result.resource.name if result.resource else "General"
        grouped_results[key].append(result)

    return render(
        request,
        "checker/run_detail.html",
        {
            "run": run,
            "grouped_results": dict(grouped_results),
        },
    )


@require_POST
def run_resource(request, pk):
    resource = get_object_or_404(ManagedResource, pk=pk)
    run = CheckRunner().run_resource(resource)
    messages.success(request, f"Completed checks for {resource.name}.")
    return redirect("checker:run-detail", pk=run.pk)


@require_POST
def run_service(request, service_type):
    run = CheckRunner().run_service(service_type)
    label = dict(ManagedResource.ServiceType.choices).get(service_type, service_type.upper())
    messages.success(request, f"Completed the {label} bulk run.")
    return redirect("checker:run-detail", pk=run.pk)


@require_POST
def run_all(request):
    run = CheckRunner().run_all()
    messages.success(request, "Completed the full AWS bulk run.")
    return redirect("checker:run-detail", pk=run.pk)
