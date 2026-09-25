class JobCancelled(Exception):
    """Raised by a handler when it notices mid-download that the user
    cancelled. Distinct from a plain tool failure: the dispatcher must
    stop outright here, not interpret it as "this tool doesn't work, try
    the next one"."""
