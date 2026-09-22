from . import context


class AuditContextMiddleware:
    """Leaves the request where the audit receivers can find it.

    Placed after AuthenticationMiddleware, because what is wanted is the user, and
    the finally is not decoration: a worker thread serves the next request as well,
    and a request left behind would be named as the author of somebody else's
    change.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        context.set_request(request)
        try:
            return self.get_response(request)
        finally:
            context.clear_request()
