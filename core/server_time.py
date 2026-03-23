from datetime import datetime


def get_server_tzinfo():
    return datetime.now().astimezone().tzinfo


def server_now():
    return datetime.now().astimezone()


def server_today():
    return server_now().date()


def server_timezone_label():
    now = server_now()
    tzinfo = now.tzinfo
    if tzinfo is None:
        return ""
    label = getattr(tzinfo, "key", "") or getattr(tzinfo, "zone", "")
    if label:
        return str(label)
    try:
        return str(tzinfo.tzname(now) or "")
    except Exception:
        return str(tzinfo)
