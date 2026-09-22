"""Who is making this change, and from where.

Django calls a signal receiver with the instance that was saved, never with the
request that caused the save. Somewhere to look is therefore needed, or the audit
trail could say what changed but not who changed it - so the middleware leaves the
request here for the length of one request and the receivers read it back out.

One request per thread: the project is served by gunicorn's sync worker, which
handles one request at a time per thread, and nothing in the project starts a
thread of its own.
"""
import threading

_state = threading.local()

# Who - or what - made a change
ACTOR_USER = 'user'            # somebody signed in
ACTOR_CRON = 'cron'            # a request without a user, the housekeeping itself
ACTOR_ANONYMOUS = 'anonymous'  # a sign-in that was refused
ACTOR_COMMAND = 'command'      # manage.py: not a person, and not the cron either

# A header is written by whoever sends it. An address that does not fit is cut,
# and it must never be able to raise inside a login.
IP_LENGTH = 45
FORWARDED_LENGTH = 255
AGENT_LENGTH = 300
NAME_LENGTH = 150


def set_request(request):
    _state.request = request


def get_request():
    return getattr(_state, 'request', None)


def clear_request():
    _state.request = None


def text(value, length):
    """A header or a name, cut to what the column holds and never anything else."""
    return str(value if value is not None else '')[:length]


def client_address(request):
    """The address the request came from, and the chain it arrived through.

    X-Real-IP first: the nginx of this project sets it to its own $remote_addr, so
    it is nginx's view of the connection and a client cannot write it.

    X-Forwarded-For otherwise, and there the last entry - that is the one the proxy
    in front appended. Every entry before it was written by whoever was before
    that, so the first one is what the client felt like claiming. The whole chain
    is kept beside the address, so what was claimed can be read instead of having
    to be believed.

    REMOTE_ADDR last: without a proxy in front that is the connection itself, and
    behind one it is the proxy - which is why it is the last resort and not the
    first.
    """
    if request is None:
        return '', ''
    meta = getattr(request, 'META', None) or {}
    forwarded = text(meta.get('HTTP_X_FORWARDED_FOR'), FORWARDED_LENGTH).strip()
    address = text(meta.get('HTTP_X_REAL_IP'), IP_LENGTH).strip()
    if not address and forwarded:
        address = forwarded.split(',')[-1].strip()
    if not address:
        address = text(meta.get('REMOTE_ADDR'), IP_LENGTH).strip()
    return address, forwarded


def user_agent(request):
    if request is None:
        return ''
    meta = getattr(request, 'META', None) or {}
    return text(meta.get('HTTP_USER_AGENT'), AGENT_LENGTH).strip()


def identity(request=None):
    """Where a request came from, as both records write it down."""
    if request is None:
        request = get_request()
    address, forwarded = client_address(request)
    return {'ip': address, 'forwarded': forwarded, 'user_agent': user_agent(request)}


def current_user(request):
    """The account that is signed in, if one is."""
    user = getattr(request, 'user', None) if request is not None else None
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return user


def actor(request=None):
    """(the account, what kind of actor it was).

    No request at all is not the same as a request without a user: the first is a
    management command or a shell, the second is the cron container asking one of
    the cron URLs. Both are recorded, and neither is mistaken for a person.
    """
    if request is None:
        request = get_request()
    if request is None:
        return None, ACTOR_COMMAND
    user = current_user(request)
    if user is not None:
        return user, ACTOR_USER
    return None, ACTOR_CRON


def user_name(user):
    """The account as it is written down, so that deleting it later cannot blank
    out what it did."""
    if user is None:
        return ''
    return text(user.get_username(), NAME_LENGTH)


def user_level(user):
    """The level the account holds now - the record is what keeps it for later,
    when it has been changed."""
    if user is None:
        return None
    from user_manager.models import UserAcl

    acl = UserAcl.objects.filter(user=user).first()
    return acl.user_level if acl is not None else None
