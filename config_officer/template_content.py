"""Plugin template extensions - injects buttons into the Device detail page.

NetBox looks for this file as 'template_content.py' by default
(DEFAULT_RESOURCE_PATHS['template_extensions'] = 'template_content.template_extensions').
The list must be named 'template_extensions'.

IMPORTANT for NetBox 4.3+: use 'models' (plural list), NOT 'model' (singular string).
"""

from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from netbox.plugins import PluginTemplateExtension


class DeviceConfigButtons(PluginTemplateExtension):
    """Add config-officer action buttons to the Device detail page."""

    models = ["dcim.device"]

    def buttons(self):
        device = self.context["object"]
        name = device.name

        try:
            running_url = reverse(
                "plugins:config_officer:running_config",
                kwargs={"hostname": name},
            )
            collect_url = reverse(
                "plugins:config_officer:collect_device_config",
                kwargs={"slug": name},
            )
        except NoReverseMatch:
            return ""

        # Compliance button only if a Compliance record exists
        compliance_html = ""
        try:
            from .models import Compliance

            if Compliance.objects.filter(device=device).exists():
                compliance_url = reverse(
                    "plugins:config_officer:compliance",
                    kwargs={"device": device.pk},
                )
                compliance_html = format_html(
                    ' <a href="{}" class="btn btn-outline-secondary btn-sm">'
                    '<i class="mdi mdi-check-decagram"></i> Compliance'
                    "</a>",
                    compliance_url,
                )
        except Exception:
            pass

        return format_html(
            '<a href="{}" class="btn btn-outline-info btn-sm">'
            '<i class="mdi mdi-console"></i> Show running config'
            "</a> "
            '<a href="{}" class="btn btn-outline-primary btn-sm">'
            '<i class="mdi mdi-download-network"></i> Collect config'
            "</a>"
            "{}",
            running_url,
            collect_url,
            compliance_html,
        )


template_extensions = [DeviceConfigButtons]
