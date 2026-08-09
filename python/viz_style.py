"""shared matplotlib styling, light surface only since these are static
PNGs for the README"""

import matplotlib as mpl

# palette roles
SURFACE = "#fcfcfb"     # chart surface
PLANE = "#f9f9f7"       # page plane
INK = "#0b0b0b"         # primary ink
INK_2 = "#52514e"       # secondary ink
MUTED = "#898781"       # axis / tick labels
GRID = "#e1e0d9"        # hairline gridline
AXIS = "#c3c2b7"        # baseline / axis

SERIES_1 = "#2a78d6"    # categorical slot 1, blue
SERIES_2 = "#eb6834"    # categorical slot 2, orange


def panel_title(ax, title, sub=None, y=1.085, y_sub=1.018):
    """title + optional subtitle above an axes, avoids ax.set_title so the
    subtitle doesn't overlap it"""
    ax.text(0, y, title, transform=ax.transAxes, fontsize=11,
            weight="600", color=INK, ha="left", va="bottom")
    if sub:
        ax.text(0, y_sub, sub, transform=ax.transAxes, fontsize=8.6,
                color=MUTED, ha="left", va="bottom")


def use_style():
    mpl.rcParams.update({
        "figure.facecolor": PLANE,
        "savefig.facecolor": PLANE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.8,
        "axes.labelcolor": INK_2,
        "axes.titlecolor": INK,
        "axes.titlesize": 11,
        "axes.titleweight": "600",
        "axes.titlelocation": "left",
        "axes.titlepad": 10,
        "axes.labelsize": 9.5,
        "axes.grid": True,
        "axes.axisbelow": True,          # grid never sits on top of the data
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": GRID,
        "grid.linewidth": 0.7,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelsize": 8.5,
        "ytick.labelsize": 8.5,
        "xtick.direction": "out",
        "ytick.direction": "out",
        "legend.frameon": False,
        "legend.fontsize": 9,
        "legend.labelcolor": INK_2,
        "lines.linewidth": 2.0,          # thin marks
        "font.family": "sans-serif",
        "font.sans-serif": ["Segoe UI", "system-ui", "DejaVu Sans"],
        "figure.dpi": 130,
        "savefig.bbox": "tight",
    })
