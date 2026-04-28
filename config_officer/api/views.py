from django.http import HttpResponse
from rest_framework.viewsets import ModelViewSet

from config_officer.models import Collection
from config_officer.views import global_collection

from .serializers import CollectionSerializer


class GlobalDataCollectionView(ModelViewSet):
    queryset = Collection.objects.all()
    serializer_class = CollectionSerializer

    def create(self, request):
        """POST request."""
        task = request.POST.get("task")
        message = global_collection() if task == "global_collection" else "wrong task"
        return HttpResponse(message)

    def list(self, request):
        """GET request."""
        return HttpResponse("not allowed")
