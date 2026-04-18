"""Models for config_officer plugin."""

from django.db import models
from django.urls import reverse
from django.db.models import Q
from django.contrib.postgres.fields import ArrayField

from .choices import (
    ServiceComplianceChoices,
    CollectFailChoices,
    CollectStatusChoices,
)
from .config_manager import generate_templates_config_for_device


# ----------------------------
# Collection
# ----------------------------
class Collection(models.Model):
    device = models.ForeignKey(
        to="dcim.Device",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )

    status = models.CharField(
        max_length=255,
        choices=CollectStatusChoices,
        default=CollectStatusChoices.STATUS_PENDING,
        null=True,
    )

    message = models.CharField(max_length=512, blank=True, null=True)

    timestamp = models.DateTimeField(auto_now_add=True)

    failed_reason = models.CharField(
        max_length=255,
        choices=CollectFailChoices,
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return str(self.device) if self.device else "n/a"


# ----------------------------
# Template
# ----------------------------
class Template(models.Model):
    name = models.CharField(max_length=512)
    description = models.CharField(max_length=512, blank=True, null=True)
    configuration = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name or ""

    def get_absolute_url(self):
        return reverse("plugins:config_officer:template", args=[self.pk])

    def get_services_list(self):
        return Service.objects.filter(service_rules__template=self).distinct()


# ----------------------------
# Service
# ----------------------------
class Service(models.Model):
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:config_officer:service", args=[self.pk])

    def get_service_rules(self):
        return self.service_rules.all()

    def get_devices(self):
        return Device.objects.filter(servicemapping__service=self).distinct()

    def get_device_templates(self, device):
        if not ServiceMapping.objects.filter(service=self, device=device).exists():
            return []

        rules = self.service_rules.all()

        device_rules = rules.filter(
            Q(device_role=device.device_role) |
            Q(device_role__isnull=True)
        ).filter(
            Q(device_type=device.device_type) |
            Q(device_type__isnull=True)
        )

        return [r.template for r in device_rules if r.template]


# ----------------------------
# ServiceRule (FIXED M2M LOGIC)
# ----------------------------
class ServiceRule(models.Model):
    service = models.ForeignKey(
        to="config_officer.Service",
        on_delete=models.CASCADE,
        related_name="service_rules",
    )

    description = models.CharField(max_length=512, blank=True, null=True)

    # Keep M2M but FIX usage everywhere
    device_role = models.ManyToManyField("dcim.DeviceRole", blank=True)

    device_type = models.ManyToManyField("dcim.DeviceType", blank=True)

    template = models.ForeignKey(
        to="config_officer.Template",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )

    def matches_device(self, device):
        """Central matching logic (IMPORTANT FIX)."""

        role_match = (
            not self.device_role.exists()
            or device.device_role in self.device_role.all()
        )

        type_match = (
            not self.device_type.exists()
            or device.device_type in self.device_type.all()
        )

        return role_match and type_match


# ----------------------------
# ServiceMapping
# ----------------------------
class ServiceMapping(models.Model):
    device = models.ForeignKey("dcim.Device", on_delete=models.CASCADE)
    service = models.ForeignKey(Service, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("device", "service")

    def __str__(self):
        return f"{self.device}:{self.service}"


# ----------------------------
# Compliance
# ----------------------------
class Compliance(models.Model):
    device = models.OneToOneField(
        "dcim.Device",
        on_delete=models.CASCADE,
        related_name="compliance",
    )

    status = models.CharField(
        max_length=50,
        choices=ServiceComplianceChoices,
        default=ServiceComplianceChoices.STATUS_NON_COMPLIANCE,
    )

    notes = models.CharField(max_length=512, blank=True, null=True)

    generated_config = models.TextField(blank=True, null=True)

    diff = models.TextField(blank=True, null=True)

    services = ArrayField(
        models.CharField(max_length=512),
        default=list,
        blank=True,
    )

    def __str__(self):
        return f"{self.device}:{self.status}"

    def get_services(self):
        return Service.objects.filter(servicemapping__device=self.device).distinct()

    def get_device_templates(self):
        templates = []

        for service in self.get_services():
            for rule in service.get_service_rules():
                if rule.matches_device(self.device):
                    if rule.template:
                        templates.append(rule.template)

        return list(set(templates))

    def get_generated_config(self):
        self.generated_config = generate_templates_config_for_device(
            self.get_device_templates()
        )
        return self.generated_config

    def get_absolute_url(self):
        return reverse("plugins:config_officer:compliance", args=[self.pk])