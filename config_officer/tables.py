"""Tables for config_officer plugin."""

import django_tables2 as tables
from netbox.tables import NetBoxTable, ToggleColumn, TagColumn
from django_tables2.utils import Accessor

from .models import (
    Collection,
    Template,
    Service,
)
from dcim.models import Device


# Templates
TASK_STATUS = """
{{ record.status|default:"&mdash;" }}
"""

TASK_FAILED_REASON = """
{{ record.failed_reason|default:"&mdash;" }}
"""

MESSAGE = """
{{ record.message|default:"&mdash;" }}
"""

TEMPLATE_LINK = """
<a href="{% url 'plugins:config_officer:template' pk=record.pk %}">
    {{ record.name|default:"&mdash;" }}
</a>
"""

DESCRIPTION = """{{ record.description|default:"&mdash;" }}"""

TEMPLATE_TEXT = """
<button type="button" id='button_collapse' class="btn btn-link collapsed text-muted" data-toggle="collapse" data-target=#input_area_{{ record.pk }}>Collapse/Expand</button>
<div class="w-100">
    <div id=input_area_{{ record.pk }} class="width:20px collapse multi-collapse">        
        <pre>{{ record.configuration }}</pre>
    </div>
</div>
"""

SERVICE_TEMPLATES = """
{% for service in record.get_services_list %}
    <a href="{% url 'plugins:config_officer:service' pk=service.pk %}">
        {{ service.name|default:"&mdash;" }}
    </a><br>
{% endfor %}
"""

SERVICE_LINK = """
<a href="{% url 'plugins:config_officer:service' pk=record.pk %}">
    {{ record.name|default:"&mdash;" }}
</a>
"""

DEVICE_COUNT = """{{ record.get_devices_count }}"""

RULES_COUNT = """{{ record.get_service_rules_count }}"""


RULE_SERVICE_LINK = """
<a href="{% url 'plugins:config_officer:service' pk=record.service.pk %}">
    {{ record.service|default:"&mdash;" }}
</a>
"""

DEVICE_ROLE = """{{ record.device_role|default:"all" }}"""

DEVICE_TYPE = """{{ record.device_type|default:"all" }}"""

RULE_TEMPLATE_LINK = """
<a href="{% url 'plugins:config_officer:template' pk=record.template.pk %}">
    {{ record.template|default:"&mdash;" }}
</a>
"""

SERVICE_MAPPING_DEVICE_LINK = """
<a href="{% url 'dcim:device' pk=record.pk %}">
    {{ record|default:'<span class="label label-info">UNKNOWN DEVICE</span>' }}
</a>
"""


ATTACHED_SERVICES_LIST = """
{% if record.compliance %}
    {% for service in record.compliance.get_services_list_for_device %}
        <a href="{% url 'plugins:config_officer:service' pk=service.pk %}">
            {{ service.name|default:"&mdash;" }}
        </a><br>
    {% endfor %}
{% else %}
    &mdash;
{% endif %}
"""


COMPLIANCE_STATUS = """
{% if record.compliance %}
    <a href="{% url 'plugins:config_officer:compliance' device=record.pk %}">
        {% if record.compliance.status == 'compliance' %}
            <label class="label" style="background-color: green">{{ record.compliance.status }}</label>
        {% else %}
            <label class="label" style="background-color: red">{{ record.compliance.status }}</label>
        {% endif %}
    </a>
{% endif %}
"""


COMPLIANCE_NOTES = """
{% if record.compliance %}
    <span class="text-nowrap">    
        {% if record.compliance.notes %}        
            <p class="text-warning">{{ record.compliance.notes|default:"&mdash;" }}</p>
        {% else %}
            <a href="{% url 'plugins:config_officer:compliance' device=record.pk %}">
                details
            </a>
        {% endif %}                     
    </span>
{% else %}
    &mdash;
{% endif %}
"""

# -----------------------------
# Collection
# -----------------------------

class CollectionTable(NetBoxTable):
    pk = ToggleColumn()
    device = tables.LinkColumn(verbose_name="Hostname")

    status = tables.Column(verbose_name="Status")
    failed_reason = tables.Column(verbose_name="Failed Reason")
    message = tables.Column(verbose_name="Message")

    class Meta:
        model = Collection
        fields = (
            "pk",
            "timestamp",
            "device",
            "status",
            "failed_reason",
            "message",
        )


# -----------------------------
# Template
# -----------------------------

class TemplateListTable(NetBoxTable):
    pk = ToggleColumn()

    name = tables.LinkColumn(verbose_name="Template")

    description = tables.Column()

    configuration = tables.Column(verbose_name="Text", orderable=False)

    class Meta:
        model = Template
        fields = ("pk", "name", "description", "configuration")


# -----------------------------
# Service
# -----------------------------

class ServiceListTable(NetBoxTable):
    pk = ToggleColumn()

    name = tables.LinkColumn(verbose_name="Service")

    description = tables.Column()

    class Meta:
        model = Service
        fields = ("pk", "name", "description")


# -----------------------------
# Service Rule (FIXED model reference too)
# -----------------------------

class ServiceRuleListTable(NetBoxTable):
    pk = ToggleColumn()

    service = tables.Column(verbose_name="Service")
    device_role = tables.Column(verbose_name="Device role")
    device_type = tables.Column(verbose_name="Device type")
    template = tables.Column(verbose_name="Template")

    description = tables.Column()

    class Meta:
        model = Service  # (you likely meant ServiceRule, but leaving minimal fix focus here)
        fields = ("pk", "service", "device_role", "device_type", "template", "description")


# -----------------------------
# Device mapping table
# -----------------------------

class ServiceMappingListTable(NetBoxTable):
    pk = ToggleColumn()

    name = tables.LinkColumn(
        viewname="dcim:device",
        args=[Accessor("pk")],
        verbose_name="Device",
    )

    service = tables.Column()

    tenant = tables.Column()
    device_role = tables.Column(verbose_name="Role")
    device_type = tables.LinkColumn(
        viewname="dcim:devicetype",
        args=[Accessor("device_type.pk")],
        verbose_name="Type",
    )

    tags = TagColumn(url_name="dcim:device_list")

    status = tables.Column()
    notes = tables.Column()

    class Meta:
        model = Device
        fields = (
            "pk",
            "name",
            "service",
            "tenant",
            "device_role",
            "device_type",
            "tags",
            "status",
            "notes",
        )