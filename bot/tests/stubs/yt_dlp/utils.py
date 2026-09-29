class DownloadCancelled(Exception):
    pass


class DownloadError(Exception):
    pass


def download_range_func(chapters, ranges):
    """Same shape as the real one: (info_dict, ydl) -> [{start_time, end_time}]."""
    return lambda info, ydl: [{"start_time": a, "end_time": b} for a, b in ranges]
