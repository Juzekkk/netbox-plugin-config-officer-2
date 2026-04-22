"""Views for config_officer plugin – NetBox 4.x compatible."""

from copy import deepcopy
from datetime import datetime
import io
import os

import django_tables2 as tables_lib
import pytz
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django_rq import get_queue

from netbox.views.generic import (
    ObjectListView,
    ObjectEditView,
    ObjectDeleteView,
    BulkDeleteView,
)

from dcim.models import Device

from .choices import CollectStatusChoices
from .filters import CollectionFilter, ServiceMappingFilter
from .forms import (
    CollectionFilterForm,
    TemplateForm,
    ServiceForm,
    ServiceRuleForm,
    ServiceMappingForm,
    ServiceMappingCreateForm,
    ServiceMappingFilterForm,
)
from .git_manager import get_device_config, get_config_update_date, get_file_repo_state
from .models import Collection, Template, Service, ServiceRule, ServiceMapping, Compliance
from .tables import (
    CollectionTable,
    TemplateListTable,
    ServiceListTable,
    ServiceRuleListTable,
    ServiceMappingListTable,
)

PLUGIN_SETTINGS = settings.PLUGINS_CONFIG.get("config_officer", dict())
NETBOX_DEVICES_CONFIGS_REPO_DIR = PLUGIN_SETTINGS.get("NETBOX_DEVICES_CONFIGS_REPO_DIR", "/device_configs")
NETBOX_DEVICES_CONFIGS_SUBPATH = PLUGIN_SETTINGS.get('NETBOX_DEVICES_CONFIGS_SUBPATH', 'netbox')
NETBOX_DEVICES_CONFIGS_PATH = os.path.join(NETBOX_DEVICES_CONFIGS_REPO_DIR, NETBOX_DEVICES_CONFIGS_SUBPATH)
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")


# Helper: simple paginated list view for plugin models that do NOT inherit
# from NetBox standard models (Collection, Template, Service, ServiceRule).
# Using ObjectListView would force NetBoxTable which auto-generates broken
# action URLs for non-standard models.

class PluginTableView(PermissionRequiredMixin, View):
    """Base view: renders a django-tables2 table with an optional filter form."""
    permission_required = ("dcim.view_device",)
    queryset = None
    table_class = None
    filterset_class = None
    filterset_form_class = None
    template_name = "config_officer/generic_list.html"
    page_title = ""
    add_url = None        # name of 'add' URL for the "+ Add" button

    def get_queryset(self, request):
        return self.queryset

    def get(self, request):
        qs = self.get_queryset(request)

        # Apply filterset if present
        filterset = None
        filter_form = None
        if self.filterset_class:
            filterset = self.filterset_class(request.GET, queryset=qs)
            qs = filterset.qs
        if self.filterset_form_class:
            filter_form = self.filterset_form_class(request.GET)

        table = self.table_class(qs)
        tables_lib.RequestConfig(request, paginate={"per_page": 50}).configure(table)

        return render(request, self.template_name, {
            "table": table,
            "filter_form": filter_form,
            "page_title": self.page_title,
            "add_url": self.add_url,
        })



# Global collection

def global_collection():
    devices_collecting = Collection.objects.filter(
        Q(status__iexact=CollectStatusChoices.STATUS_PENDING)
        | Q(status__iexact=CollectStatusChoices.STATUS_RUNNING)
    )
    count = devices_collecting.count()
    if count > 0:
        return (
            f"Global collection not possible now. There are {count} devices in "
            f"{CollectStatusChoices.STATUS_PENDING} or {CollectStatusChoices.STATUS_RUNNING} state."
        )
    get_queue("default").enqueue("config_officer.worker.collect_all_devices_configs")
    return "Global sync was started."


class GlobalCollectionDeviceConfigs(View):
    def get(self, request):
        message = global_collection()
        return render(request, "config_officer/collection_message.html", {"message": message})



# Collection status

class CollectStatusListView(PluginTableView):
    queryset = Collection.objects.all().order_by("-id")
    table_class = CollectionTable
    filterset_class = CollectionFilter
    filterset_form_class = CollectionFilterForm
    template_name = "config_officer/collect_configs_list.html"
    page_title = "Collect running-config tasks"


class CollectTaskDelete(PermissionRequiredMixin, View):
    """Simple single-object delete for Collection records (via ?pk= param)."""
    permission_required = ("dcim.view_device",)

    def get(self, request):
        pk = request.GET.get("pk")
        if pk:
            Collection.objects.filter(pk=pk).delete()
            messages.success(request, "Collection task deleted.")
        else:
            # Bulk delete from POST (checkbox selection)
            pk_list = request.POST.getlist("pk")
            Collection.objects.filter(pk__in=pk_list).delete()
            messages.success(request, f"{len(pk_list)} task(s) deleted.")
        return redirect(reverse("plugins:config_officer:collection_status"))

    def post(self, request):
        pk_list = request.POST.getlist("pk")
        Collection.objects.filter(pk__in=pk_list).delete()
        messages.success(request, f"{len(pk_list)} task(s) deleted.")
        return redirect(reverse("plugins:config_officer:collection_status"))


def collect_device_config(request, slug):
    """Trigger single-device collection via RQ."""
    if not Device.objects.filter(name__iexact=slug).exists():
        message = f"Device '{slug}' not found."
        return render(request, "config_officer/collection_message.html", {"message": message})
    try:
        get_queue("default").enqueue(
            "config_officer.worker.collect_device_config_hostname", hostname=slug
        )
        return redirect(reverse("plugins:config_officer:collection_status"))
    except Exception as exc:
        return render(request, "config_officer/collection_message.html", {"message": str(exc)})



# Template views

class TemplateListView(PluginTableView):
    queryset = Template.objects.all()
    table_class = TemplateListTable
    template_name = "config_officer/template_list.html"
    page_title = "Device configuration templates"
    add_url = "plugins:config_officer:template_add"


class TemplateCreateView(PermissionRequiredMixin, ObjectEditView):
    permission_required = ("dcim.view_device",)
    queryset = Template.objects.all()
    form = TemplateForm
    default_return_url = "plugins:config_officer:template_list"


class TemplateEditView(TemplateCreateView):
    pass


class TemplateView(PermissionRequiredMixin, View):
    permission_required = ("dcim.view_device",)

    def get(self, request, pk):
        template = get_object_or_404(Template, pk=pk)
        return render(request, "config_officer/template_view.html", {"template": template})


class TemplateDeleteView(PermissionRequiredMixin, ObjectDeleteView):
    permission_required = ("dcim.view_device",)
    queryset = Template.objects.all()
    default_return_url = "plugins:config_officer:template_list"



# Service views

class ServiceListView(PluginTableView):
    queryset = Service.objects.all()
    table_class = ServiceListTable
    template_name = "config_officer/service_list.html"
    page_title = "Device services"
    add_url = "plugins:config_officer:service_add"


class ServiceCreateView(PermissionRequiredMixin, ObjectEditView):
    permission_required = ("dcim.view_device",)
    queryset = Service.objects.all()
    form = ServiceForm
    default_return_url = "plugins:config_officer:service_list"


class ServiceEditView(ServiceCreateView):
    pass


class ServiceView(PermissionRequiredMixin, View):
    permission_required = ("dcim.view_device",)

    def get(self, request, pk):
        service = get_object_or_404(Service, pk=pk)
        service_rules = ServiceRule.objects.filter(service=service)
        return render(
            request,
            "config_officer/service_view.html",
            {"service": service, "service_rules": service_rules},
        )


class ServiceDeleteView(PermissionRequiredMixin, ObjectDeleteView):
    permission_required = ("dcim.view_device",)
    queryset = Service.objects.all()
    default_return_url = "plugins:config_officer:service_list"



# ServiceRule views

class ServiceRuleListView(PluginTableView):
    queryset = ServiceRule.objects.all().order_by("service")
    table_class = ServiceRuleListTable
    template_name = "config_officer/service_rule_list.html"
    page_title = "Service rules"
    add_url = "plugins:config_officer:service_rule_add"


class ServiceRuleCreateView(PermissionRequiredMixin, ObjectEditView):
    permission_required = ("dcim.view_device",)
    queryset = ServiceRule.objects.all()
    form = ServiceRuleForm
    default_return_url = "plugins:config_officer:service_rules_list"


class ServiceRuleEditView(ServiceRuleCreateView):
    pass


class ServiceRuleDeleteView(PermissionRequiredMixin, ObjectDeleteView):
    permission_required = ("dcim.view_device",)
    queryset = ServiceRule.objects.all()
    default_return_url = "plugins:config_officer:service_rules_list"



# Compliance view

class ComplianceView(PermissionRequiredMixin, View):
    permission_required = ("dcim.view_device",)

    def get(self, request, device):
        record = get_object_or_404(Compliance, device=device)
        device_config = get_device_config(NETBOX_DEVICES_CONFIGS_PATH, record.device.name, "running")
        config_update_date = get_config_update_date(NETBOX_DEVICES_CONFIGS_PATH, record.device.name, "running")
        return render(
            request,
            "config_officer/compliance_view.html",
            {
                "record": record,
                "device_config": device_config,
                "config_update_date": config_update_date,
            },
        )



# ServiceMapping views
# Device is a standard NetBox model so ObjectListView + NetBoxTable work fine.

class ServiceMappingListView(PermissionRequiredMixin, ObjectListView):
    permission_required = ("dcim.view_device",)
    queryset = Device.objects.all()
    filterset = ServiceMappingFilter
    filterset_form = ServiceMappingFilterForm
    table = ServiceMappingListTable
    template_name = "config_officer/service_mapping_list.html"

    def _export_to_excel(self):
        try:
            import xlsxwriter
        except ImportError:
            return None

        output = io.BytesIO()
        header = [
            {"header": "Hostname"}, {"header": "PID"}, {"header": "Role"},
            {"header": "IP"}, {"header": "Tenant"}, {"header": "Compliance"},
            {"header": "Diff"}, {"header": "Notes"},
        ]
        width = [len(i["header"]) + 2 for i in header]
        data = []

        for d in Device.objects.all().order_by("tenant"):
            if hasattr(d, "compliance"):
                row = [
                    d.name, d.device_type.model, d.device_role.name,
                    str(d.primary_ip4).split("/")[0] if d.primary_ip4 else "",
                    str(d.tenant), d.compliance.status,
                    d.compliance.diff or "", d.compliance.notes or "",
                ]
            else:
                row = [
                    d.name, d.device_type.model, d.device_role.name,
                    str(d.primary_ip4).split("/")[0] if d.primary_ip4 else "",
                    str(d.tenant), "service not assigned", "", "",
                ]
            data.append(row)
            w = [len(str(i)) if i else 40 for i in row]
            width = [max(width[i], w[i]) for i in range(len(width))]

        workbook = xlsxwriter.Workbook(output, {"remove_timezone": True, "default_date_format": "yyyy-mm-dd"})
        worksheet = workbook.add_worksheet("compliance")
        worksheet.add_table(0, 0, Device.objects.count(), len(header) - 1, {"columns": header, "data": data})
        for i, w in enumerate(width):
            worksheet.set_column(i, i, w)
        workbook.close()
        output.seek(0)
        return output

    def post(self, request, *args, **kwargs):
        if "_create" in request.POST:
            form = ServiceMappingCreateForm(request.POST)
            if form.is_valid():
                data = deepcopy(form.cleaned_data)
                services = data["service"]
                if not services:
                    messages.error(request, "No services selected.")
                else:
                    Compliance.objects.filter(device__in=data["pk"]).delete()
                    for device in data["pk"]:
                        ServiceMapping.objects.filter(device=device).delete()
                        for service in services:
                            ServiceMapping.objects.update_or_create(device=device, service=service)
                        get_queue("default").enqueue(
                            "config_officer.worker.check_device_config_compliance", device=device
                        )
                    messages.success(request, f"{list(services)} attached to {len(data['pk'])} device(s).")
            else:
                messages.error(request, "Form is not valid.")
        return redirect(request.get_full_path())

    def get(self, request, *args, **kwargs):
        if "to_excel" in request.GET:
            output = self._export_to_excel()
            if output:
                filename = (
                    f"compliance_{datetime.now().astimezone(pytz.timezone(TIME_ZONE)).strftime('%Y%m%d_%H%M%S')}.xlsx"
                )
                response = HttpResponse(
                    output,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                response["Content-Disposition"] = f'attachment; filename="{filename}"'
                return response
        return super().get(request, *args, **kwargs)


class ServiceMappingCreateView(PermissionRequiredMixin, ObjectEditView):
    permission_required = ("dcim.view_device",)
    queryset = ServiceMapping.objects.all()
    form = ServiceMappingForm
    default_return_url = "plugins:config_officer:service_mapping_list"


class ServiceMappingDeleteView(PermissionRequiredMixin, ObjectDeleteView):
    permission_required = ("dcim.view_device",)
    queryset = ServiceMapping.objects.all()
    default_return_url = "plugins:config_officer:service_mapping_list"


class ServiceAssign(PermissionRequiredMixin, View):
    permission_required = ("dcim.view_device",)

    def post(self, request):
        pk_list = [int(pk) for pk in request.POST.getlist("pk")]
        selected_devices = Device.objects.filter(pk__in=pk_list)

        if not selected_devices.exists():
            messages.warning(request, "No devices were selected.")
            return redirect(reverse("plugins:config_officer:service_mapping_list"))

        return render(
            request,
            "generic/object_bulk_add_component.html",
            {
                "form": ServiceMappingCreateForm(initial={"pk": pk_list}),
                "parent_model_name": "Devices",
                "model_name": "Service",
                "table": ServiceMappingListTable(selected_devices),
                "return_url": reverse("plugins:config_officer:service_mapping_list"),
            },
        )


class ServiceDetach(PermissionRequiredMixin, View):
    permission_required = ("dcim.view_device",)

    def post(self, request):
        pk_list = [int(pk) for pk in request.POST.getlist("pk")]
        selected_devices = Device.objects.filter(pk__in=pk_list)

        if not selected_devices.exists():
            messages.warning(request, "No devices were selected.")
            return redirect(reverse("plugins:config_officer:service_mapping_list"))

        ServiceMapping.objects.filter(device__in=selected_devices).delete()
        Compliance.objects.filter(device__in=selected_devices).delete()
        messages.success(request, f"{selected_devices.count()} device(s) de-attached from service.")
        return redirect(reverse("plugins:config_officer:service_mapping_list"))



# Running config page (Custom Link target)

def running_config(request, hostname):
    """Show device running-config page – called via NetBox Custom Link."""
    running = get_device_config(NETBOX_DEVICES_CONFIGS_PATH, hostname, "running")
    message: dict = {}
    if not running:
        message["status"] = False
        message["comment"] = "Error reading running config file from directory."
    else:
        message["status"] = True
        message["running_config"] = running
    message["repo_state"] = get_file_repo_state(
        NETBOX_DEVICES_CONFIGS_PATH, f"{hostname}_running.txt"
    )
    return render(request, "config_officer/device_running_config.html", {"message": message})