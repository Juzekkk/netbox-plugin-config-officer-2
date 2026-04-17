from django import forms

from netbox.forms import NetBoxModelForm
from utilities.forms.fields import DynamicModelMultipleChoiceField

from tenancy.models import Tenant
from dcim.models import DeviceRole, DeviceType, Device

from .choices import CollectStatusChoices, ServiceComplianceChoices
from .models import (
    Collection,
    Template,
    Service,
    ServiceMapping,
    ServiceRule,
)


BLANK_CHOICE = (('', '---------'),)


# -----------------------------
# Collection Filter
# -----------------------------
class CollectionFilterForm(forms.Form):
    status = forms.ChoiceField(
        choices=BLANK_CHOICE + CollectStatusChoices.CHOICES,
        required=False,
        label='Status'
    )

    failed_reason = forms.ChoiceField(
        choices=BLANK_CHOICE + CollectStatusChoices.CHOICES,
        required=False,
        label='Failed Reason',
    )


# -----------------------------
# Template
# -----------------------------
class TemplateForm(NetBoxModelForm):
    class Meta:
        model = Template
        fields = ['name', 'description', 'configuration']


# -----------------------------
# Service
# -----------------------------
class ServiceForm(NetBoxModelForm):
    class Meta:
        model = Service
        fields = ['name', 'description']


# -----------------------------
# Service Rule
# -----------------------------
class ServiceRuleForm(NetBoxModelForm):
    service = forms.ModelChoiceField(
        queryset=Service.objects.all()
    )

    device_role = DynamicModelMultipleChoiceField(
        queryset=DeviceRole.objects.all(),
        required=True,
    )

    device_type = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        label='Model',
    )

    template = forms.ModelChoiceField(
        queryset=Template.objects.order_by('name')
    )

    class Meta:
        model = ServiceRule
        fields = [
            'service',
            'device_role',
            'device_type',
            'template',
            'description',
        ]


# -----------------------------
# Service Mapping
# -----------------------------
class ServiceMappingForm(NetBoxModelForm):
    service = forms.ModelChoiceField(
        queryset=Service.objects.all()
    )

    device = forms.ModelChoiceField(
        queryset=Device.objects.all()
    )

    class Meta:
        model = ServiceMapping
        fields = ['service', 'device']


# -----------------------------
# Bulk create mapping
# -----------------------------
class ServiceMappingCreateForm(forms.Form):
    pk = forms.ModelMultipleChoiceField(
        queryset=Device.objects.all(),
        widget=forms.MultipleHiddenInput()
    )

    service = forms.ModelMultipleChoiceField(
        queryset=Service.objects.all(),
        label='Service'
    )


# -----------------------------
# Filter form (NO NetBox dynamic fields here in 4.x style)
# -----------------------------
class ServiceMappingFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        label='Search device or service'
    )

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
        label='Model',
    )

    status = forms.MultipleChoiceField(
        label='Status',
        choices=ServiceComplianceChoices,
        required=False,
    )