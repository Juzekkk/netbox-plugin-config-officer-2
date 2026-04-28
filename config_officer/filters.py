import django_filters
from dcim.models import Device, DeviceRole, DeviceType
from django.db.models import Q
from extras.filters import TagFilter
from netbox.filtersets import PrimaryModelFilterSet

from .choices import ServiceComplianceChoices
from .models import Collection


class CollectionFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(method="search")

    status = django_filters.CharFilter(field_name="status", lookup_expr="icontains")
    failed_reason = django_filters.CharFilter(field_name="failed_reason", lookup_expr="icontains")

    class Meta:
        model = Collection
        fields = ["status", "failed_reason"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(status__icontains=value) | Q(failed_reason__icontains=value))


class ServiceMappingFilter(PrimaryModelFilterSet):
    q = django_filters.CharFilter(
        method="search",
        label="Search device or service",
    )

    device_type = django_filters.ModelMultipleChoiceFilter(
        field_name="device_type__slug",
        queryset=DeviceType.objects.all(),
        to_field_name="slug",
        label="Device type (slug)",
    )

    role_id = django_filters.ModelMultipleChoiceFilter(
        field_name="role_id",
        queryset=DeviceRole.objects.all(),
        label="Role (ID)",
    )

    role = django_filters.ModelMultipleChoiceFilter(
        field_name="role__slug",
        queryset=DeviceRole.objects.all(),
        to_field_name="slug",
        label="Role (slug)",
    )

    compliance_status = django_filters.MultipleChoiceFilter(
        field_name="compliance__status",
        choices=ServiceComplianceChoices,
        null_value=None,
    )

    tag = TagFilter()

    class Meta:
        model = Device
        fields = ["id", "status", "name", "asset_tag"]

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(name__icontains=value)
            | Q(asset_tag__icontains=value.strip())
            | Q(compliance__services__contains=value.strip().splitlines())
        ).distinct()
