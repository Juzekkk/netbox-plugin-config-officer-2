"""REST API: NetBox's generic API tests for the plugin's models, and starting runs."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from users.constants import TOKEN_PREFIX
from users.models import Token
from utilities.testing import APIViewTestCases

from config_officer.models import Service, Template
from tests.integration.helpers import grant


class PluginAPI:
    view_namespace = "plugins-api:config_officer"


class TestTemplateAPI(
    PluginAPI,
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.CreateObjectViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
):
    model = Template
    brief_fields = ["description", "display", "id", "name", "url"]
    create_data = [{"name": f"New {i}", "configuration": "ntp server 1.1.1.1"} for i in range(3)]

    @classmethod
    def setUpTestData(cls):
        Template.objects.bulk_create([Template(name=f"Template {i}") for i in range(3)])


class TestServiceAPI(
    PluginAPI,
    APIViewTestCases.GetObjectViewTestCase,
    APIViewTestCases.ListObjectsViewTestCase,
    APIViewTestCases.CreateObjectViewTestCase,
    APIViewTestCases.UpdateObjectViewTestCase,
    APIViewTestCases.DeleteObjectViewTestCase,
):
    model = Service
    brief_fields = ["description", "display", "id", "name", "url"]
    create_data = [{"name": f"New {i}"} for i in range(3)]

    @classmethod
    def setUpTestData(cls):
        Service.objects.bulk_create([Service(name=f"Service {i}") for i in range(3)])


def api_client(user) -> APIClient:
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}"
    )  # NetBox v2 token
    return client


@pytest.mark.django_db
def test_start_run(make_device):
    make_device("sw1", ip="10.0.0.1/24")
    user = get_user_model().objects.create_user("automation")
    client = api_client(user)
    assert (
        client.post(
            "/api/plugins/config_officer/runs/", {"all_devices": True}, format="json"
        ).status_code
        == 403
    )

    grant(user, "dcim.device", ["collect_config"])
    response = client.post("/api/plugins/config_officer/runs/", {"tags": ["core"]}, format="json")
    assert response.status_code == 202
    assert response.json()["run_url"].endswith(
        f"/plugins/config_officer/runs/{response.json()['job_id']}/"
    )
    assert client.post("/api/plugins/config_officer/runs/", {}, format="json").status_code == 400
