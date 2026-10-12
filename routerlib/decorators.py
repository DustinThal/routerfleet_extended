from functools import wraps

from django.http import JsonResponse


def cron_endpoint(view):
    """Marks a view as one of the endpoints the scheduler calls.

    These endpoints carry no authentication on purpose: the cron container calls
    them from inside the network, on a loop, and there is no account behind that
    call. That makes them worth protecting by other means, because whoever
    reaches them makes the installation work - the command endpoints send
    payloads to the devices, one sends messages, one removes old backups.

    The shipped nginx does not pass /cron/ on, and this is the second lock
    behind it: nginx writes the address of the caller into X-Real-IP on the way
    in, while the cron container talks to the application directly, so a request
    that carries that header came from outside and is refused. It is not an
    authentication - a deployment that puts its own proxy in front, or publishes
    the application port, is not covered by it and has to keep those paths to
    itself.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.META.get('HTTP_X_REAL_IP'):
            # Answered the way a path that does not exist is answered, so the
            # reply says nothing about what is there
            return JsonResponse({'error': 'Not found'}, status=404)
        return view(request, *args, **kwargs)

    return wrapper
