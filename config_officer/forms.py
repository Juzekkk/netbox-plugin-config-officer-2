from dcim.models import Device, DeviceRole, DeviceType
from django import forms
from django.utils import timezone
from netbox.forms import NetBoxModelForm
from tenancy.models import Tenant
from utilities.forms.fields import DynamicModelMultipleChoiceField

from .choices import CollectFailChoices, CollectStatusChoices, ServiceComplianceChoices
from .models import (
    CollectSchedule,
    Service,
    ServiceMapping,
    ServiceRule,
    Template,
)

BLANK_CHOICE = (("", "---------"),)


class CollectionFilterForm(forms.Form):
    status = forms.ChoiceField(
        choices=BLANK_CHOICE + CollectStatusChoices.CHOICES,
        required=False,
        label="Status",
    )
    failed_reason = forms.ChoiceField(
        choices=BLANK_CHOICE + CollectFailChoices.CHOICES,
        required=False,
        label="Failed Reason",
    )


class TemplateForm(NetBoxModelForm):
    class Meta:
        model = Template
        fields = ["name", "description", "configuration"]


class ServiceForm(NetBoxModelForm):
    class Meta:
        model = Service
        fields = ["name", "description"]


class ServiceRuleForm(NetBoxModelForm):
    service = forms.ModelChoiceField(queryset=Service.objects.all())

    device_role = DynamicModelMultipleChoiceField(
        queryset=DeviceRole.objects.all(),
        required=False,
    )

    device_type = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        label="Model",
    )

    template = forms.ModelChoiceField(queryset=Template.objects.order_by("name"))

    class Meta:
        model = ServiceRule
        fields = ["service", "device_role", "device_type", "template", "description"]


class ServiceMappingForm(NetBoxModelForm):
    service = forms.ModelChoiceField(queryset=Service.objects.all())
    device = forms.ModelChoiceField(queryset=Device.objects.all())

    class Meta:
        model = ServiceMapping
        fields = ["service", "device"]


class ServiceMappingCreateForm(forms.Form):
    pk = forms.ModelMultipleChoiceField(
        queryset=Device.objects.all(),
        widget=forms.MultipleHiddenInput(),
    )
    service = forms.ModelMultipleChoiceField(
        queryset=Service.objects.all(),
        label="Service",
    )


class ServiceMappingFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search device or service")

    tenant = forms.ModelMultipleChoiceField(
        queryset=Tenant.objects.all(),
        required=False,
    )
    role = forms.ModelMultipleChoiceField(
        queryset=DeviceRole.objects.all(),
        required=False,
    )
    device_type_id = forms.ModelMultipleChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        label="Model",
    )
    status = forms.MultipleChoiceField(
        label="Compliance Status",
        choices=ServiceComplianceChoices.CHOICES,
        required=False,
    )


INTERVAL_PRESETS = [
    (1, "Every hour"),
    (4, "Every 4 hours"),
    (6, "Every 6 hours"),
    (12, "Every 12 hours"),
    (24, "Once a day"),
    (48, "Every 2 days"),
    (168, "Once a week"),
]


class CollectScheduleForm(forms.ModelForm):
    """Create / edit a CollectSchedule."""

    devices = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(),
        required=True,
        label="Devices",
        help_text="Select one or more devices to include in this schedule.",
    )

    interval_hours = forms.ChoiceField(
        choices=INTERVAL_PRESETS,
        initial=24,
        label="Interval",
        help_text="How often the collection should run.",
    )

    next_run = forms.DateTimeField(
        initial=timezone.now,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        label="First run",
        help_text="When the schedule should fire for the first time (your local time).",
    )

    enabled = forms.BooleanField(
        required=False,
        initial=True,
        label="Enabled",
        help_text="Uncheck to create the schedule in a paused state.",
    )

    class Meta:
        model = CollectSchedule
        fields = ["name", "devices", "interval_hours", "next_run", "enabled"]

    def clean_interval_hours(self) -> int:
        value = self.cleaned_data["interval_hours"]
        try:
            hours = int(value)
        except (TypeError, ValueError) as e:
            raise forms.ValidationError("Enter a valid number of hours.") from e
        if hours < 1:
            raise forms.ValidationError("Interval must be at least 1 hour.")
        return hours
