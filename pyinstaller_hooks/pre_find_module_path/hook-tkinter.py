import os
import sysconfig


def pre_find_module_path(hook_api):
    # The base Python install has tkinter, but its Tcl probe fails on this
    # machine. Keep the standard-library package discoverable so the frozen
    # application can use the explicitly collected Tcl/Tk runtime.
    stdlib = sysconfig.get_paths().get("stdlib")
    if stdlib:
        hook_api.search_dirs = [stdlib]
