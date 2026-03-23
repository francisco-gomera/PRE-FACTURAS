from django.utils import timezone

from .server_time import get_server_tzinfo


class ServerLocalTimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tzinfo = get_server_tzinfo()
        if tzinfo is None:
            return self.get_response(request)

        timezone.activate(tzinfo)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
