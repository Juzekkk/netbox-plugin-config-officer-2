from rest_framework import serializers
from netbox.api.serializers import NetBoxModelSerializer
from config_officer.models import Collection, CollectSchedule


class CollectionSerializer(serializers.ModelSerializer):
    """Serializer for the Collection model."""

    class Meta:
        """Meta class."""

        model = Collection
        fields = [
            "device",
            "status",
            "message",
        ]

class CollectScheduleSerializer(NetBoxModelSerializer):
    devices = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = CollectSchedule
        fields = ["id", "name", "devices", "interval_hours", "next_run", "enabled"]