from .server_time import server_now, server_today, server_timezone_label


def server_clock(request):
    now = server_now()
    return {
        "server_now": now,
        "server_now_iso": now.isoformat(),
        "server_today": server_today(),
        "server_today_iso": now.date().isoformat(),
        "server_timezone_label": server_timezone_label(),
    }
