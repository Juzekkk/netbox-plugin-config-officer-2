"""Web UI: NetBox's generic view tests for the plugin's models, every page, and permissions."""

import pytest
from core.models import Job
from django.contrib.auth import get_user_model
from utilities.testing import ViewTestCases

from config_officer.models import (
    Collection,
    CollectSchedule,
    Service,
    ServiceMapping,
    ServiceRule,
    Template,
)
from tests.integration.helpers import grant, run_job

P = "/plugins/config_officer"


class PluginURLs:
    """Point NetBox's generic view tests at the plugin's URL namespace."""

    def _get_base_url(self):
        return f"plugins:config_officer:{self.model._meta.model_name}_{{}}"


# Create / view / edit / delete / list, each with and without (constrained) permissions.
# A tuple, not a base class: pytest would collect a TestCase base class as a test itself.
CRUD_VIEW_TESTS = (
    PluginURLs,
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
)


class TestTemplateViews(*CRUD_VIEW_TESTS):
    model = Template

    @classmethod
    def setUpTestData(cls):
        Template.objects.bulk_create(
            [Template(name=f"Template {i}", configuration="ntp server 1.1.1.1") for i in range(3)]
        )
        cls.form_data = {
            "name": "New",
            "description": "d",
            "configuration": "ntp server 2.2.2.2",
            "tags": [],
        }


class TestServiceViews(*CRUD_VIEW_TESTS):
    model = Service

    @classmethod
    def setUpTestData(cls):
        Service.objects.bulk_create([Service(name=f"Service {i}") for i in range(3)])
        cls.form_data = {"name": "New", "description": "d", "tags": []}


class TestServiceRuleViews(*CRUD_VIEW_TESTS):
    model = ServiceRule

    @classmethod
    def setUpTestData(cls):
        service = Service.objects.create(name="base")
        template = Template.objects.create(name="ntp", configuration="ntp server 1.1.1.1")
        ServiceRule.objects.bulk_create(
            [ServiceRule(service=service, template=template, description=f"r{i}") for i in range(3)]
        )
        cls.form_data = {
            "service": service.pk,
            "template": template.pk,
            "description": "new",
            "device_roles": [],
            "device_types": [],
            "tags": [],
        }


class TestCollectScheduleViews(*CRUD_VIEW_TESTS):
    model = CollectSchedule
    validation_excluded_fields = ["next_run"]

    @classmethod
    def setUpTestData(cls):
        for i in range(3):
            CollectSchedule.objects.create(name=f"Schedule {i}", all_devices=True)
        cls.form_data = {
            "name": "New",
            "enabled": True,
            "all_devices": True,
            "interval_hours": 24,
            "next_run": "2030-01-01 02:00:00",
            "tags": [],
        }

    def setUp(self):
        super().setUp()
        grant(self.user, "dcim.device", ["collect_config"])  # needed to create / edit schedules


# ---------------------------------------------------------------------------
# Every page renders
# ---------------------------------------------------------------------------


@pytest.fixture
def objects(admin_user, make_device, fake_network):
    device = make_device("sw1", ip="10.0.0.1/24")
    template = Template.objects.create(name="ntp", configuration="ntp server 1.1.1.1")
    service = Service.objects.create(name="base")
    ServiceRule.objects.create(service=service, template=template)
    ServiceMapping.objects.create(device=device, service=service)
    schedule = CollectSchedule.objects.create(name="nightly", all_devices=True)
    job = run_job(admin_user, device_ids=(device.pk,))
    return {
        "device": device.pk,
        "template": template.pk,
        "service": service.pk,
        "schedule": schedule.pk,
        "rule": ServiceRule.objects.get().pk,
        "result": Collection.objects.get().pk,
        "job": job.pk,
    }


PAGES = [
    "{P}/collection/",
    "{P}/runs/{job}/",
    "{P}/results/",
    "{P}/results/{result}/",
    "{P}/schedules/{schedule}/",
    "{P}/schedules/{schedule}/jobs/",
    "{P}/templates/{template}/",
    "{P}/services/{service}/",
    "{P}/service-rules/{rule}/",
    "{P}/compliance/",
    "{P}/compliance/?compliance_status=compliance&has_compliance_service=true",
    "/dcim/devices/{device}/",
    "/dcim/devices/{device}/config-officer/configuration/",
    "/dcim/devices/{device}/config-officer/compliance/",
]


@pytest.mark.django_db
@pytest.mark.parametrize("page", PAGES)
def test_page_renders(admin_client, objects, page):
    response = admin_client.get(page.format(P=P, **objects))
    assert response.status_code == 200


@pytest.mark.django_db
def test_device_page_shows_plugin_elements(admin_client, objects):
    html = admin_client.get(f"/dcim/devices/{objects['device']}/").content.decode()
    assert "Collect config" in html and "Config Officer" in html
    config_tab = admin_client.get(
        f"/dcim/devices/{objects['device']}/config-officer/configuration/"
    ).content.decode()
    assert "ntp server 1.1.1.1" in config_tab  # rendered from the stored config


@pytest.mark.django_db
def test_legacy_urls_redirect(admin_client, objects):
    assert admin_client.get(f"{P}/running_config/sw1/")["Location"].endswith(
        "/config-officer/configuration/"
    )
    assert admin_client.get(f"{P}/compliance/{objects['device']}/")["Location"].endswith(
        "/config-officer/compliance/"
    )


# ---------------------------------------------------------------------------
# Starting runs and permissions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_run_form_queues_a_job(admin_client, make_device):
    device = make_device("sw1", ip="10.0.0.1/24")
    response = admin_client.post(f"{P}/collection/", {"target": "devices", "devices": [device.pk]})
    job = Job.objects.get(name="Config Officer - Collection")
    assert response["Location"] == f"{P}/runs/{job.pk}/"

    invalid = admin_client.post(f"{P}/collection/", {"target": "tags"})
    assert invalid.status_code == 200 and "Select at least one tag" in invalid.content.decode()


@pytest.mark.django_db
def test_view_only_user_cannot_collect_or_read_configs(client, make_device):
    device = make_device("sw1", ip="10.0.0.1/24")
    user = get_user_model().objects.create_user("viewer")
    grant(user, "dcim.device", ["view"])
    grant(user, "config_officer.collection", ["view"])
    client.force_login(user)

    assert client.get(f"{P}/collection/").status_code == 200
    assert client.post(f"{P}/collection/", {"target": "all"}).status_code == 403
    assert client.post(f"{P}/devices/{device.pk}/collect/").status_code == 403
    assert client.get(f"/dcim/devices/{device.pk}/config-officer/configuration/").status_code == 404
    assert client.get(f"{P}/templates/").status_code == 403


@pytest.mark.django_db
def test_constrained_operator(client, make_device, fake_network):
    allowed, other = (
        make_device("allowed", ip="10.0.0.1/24"),
        make_device("other", ip="10.0.0.2/24"),
    )
    user = get_user_model().objects.create_user("operator")
    grant(
        user,
        "dcim.device",
        ["view", "collect_config", "view_config"],
        constraints={"name": "allowed"},
    )
    client.force_login(user)

    assert client.post(f"{P}/devices/{allowed.pk}/collect/").status_code == 302
    assert client.post(f"{P}/devices/{other.pk}/collect/").status_code == 404
    assert client.get(f"/dcim/devices/{other.pk}/config-officer/configuration/").status_code == 404
    job = run_job(user, all_devices=True)  # "all devices" means all devices this user may collect
    assert list(Collection.objects.filter(job=job).values_list("device__name", flat=True)) == [
        "allowed"
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url", ["/collection/", "/results/", "/templates/", "/compliance/", "/running_config/sw1/"]
)
def test_anonymous_users_are_sent_to_login(client, url):
    response = client.get(P + url)
    assert response.status_code == 302 and response["Location"].startswith("/login/")
