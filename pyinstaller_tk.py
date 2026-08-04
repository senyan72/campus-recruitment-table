import os
import sys


def collect_tkinter():
    """Return tkinter modules, Tcl/Tk data files, and native binaries."""
    python_root = sys.base_prefix
    tcl_root = os.path.join(python_root, "tcl")
    dll_root = os.path.join(python_root, "DLLs")

    binaries = [
        (os.path.join(dll_root, filename), ".")
        for filename in ("_tkinter.pyd", "tk86t.dll", "tcl86t.dll")
        if os.path.isfile(os.path.join(dll_root, filename))
    ]

    datas = []
    for source, destination in (
        (os.path.join(tcl_root, "tcl8.6"), "_tcl_data"),
        (os.path.join(tcl_root, "tk8.6"), "_tk_data"),
        (os.path.join(tcl_root, "tcl8"), "tcl8"),
    ):
        if not os.path.isdir(source):
            continue
        for root, _, files in os.walk(source):
            relative = os.path.relpath(root, source)
            target = destination if relative == "." else os.path.join(destination, relative)
            datas.extend((os.path.join(root, filename), target) for filename in files)

    hiddenimports = [
        "tkinter",
        "tkinter.colorchooser",
        "tkinter.commondialog",
        "tkinter.constants",
        "tkinter.dialog",
        "tkinter.dnd",
        "tkinter.filedialog",
        "tkinter.font",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
        "tkinter.simpledialog",
        "tkinter.tix",
        "tkinter.ttk",
    ]
    return datas, binaries, hiddenimports
