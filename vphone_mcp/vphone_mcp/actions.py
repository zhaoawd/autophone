"""Pre-mapped iOS actions in preview coordinates (430×932).

All pixel values are for the preview coordinate space. The client handles
the 3× scaling to native (1290×2796) before sending to vphone.sock.

Calibrated from pluginslab/vphone-mcp native coords ÷ 3.
"""

# Preview screen dimensions
SCREEN_W = 430
SCREEN_H = 932

# ---------------------------------------------------------------------------
# Home screen app grid (preview coords)
# ---------------------------------------------------------------------------
# Column centers: 60, 167, 273, 373
# Row 1: y ≈ 350, Row 2: y ≈ 437, Row 3: y ≈ 523, Row 4: y ≈ 610
# Dock: y ≈ 833

GRID_COLS = [60, 167, 273, 373]
GRID_ROWS = [350, 437, 523, 610]
DOCK_Y = 833
DOCK_COLS = [60, 153, 273, 373]

HOME_SCREEN_APPS: dict[str, tuple[int, int]] = {
    "facetime": (0, 0),
    "calendar": (1, 0),
    "photos": (2, 0),
    "mail": (3, 0),
    "notes": (0, 1),
    "reminders": (1, 1),
    "clock": (2, 1),
    "tv": (3, 1),
    "games": (0, 2),
    "app store": (1, 2),
    "maps": (2, 2),
    "health": (3, 2),
    "wallet": (0, 3),
    "settings": (1, 3),
}

DOCK_APPS: dict[str, int] = {
    "phone": 0,
    "safari": 1,
    "messages": 2,
    "music": 3,
}

# ---------------------------------------------------------------------------
# Navigation chrome (preview coords)
# ---------------------------------------------------------------------------

BACK_BUTTON = (50, 100)
SEARCH_BAR = (215, 700)

# ---------------------------------------------------------------------------
# Swipe gestures (preview coords)
# ---------------------------------------------------------------------------

SCROLL_DOWN = {"x1": 215, "y1": 600, "x2": 215, "y2": 267, "ms": 300}
SCROLL_UP = {"x1": 215, "y1": 267, "x2": 215, "y2": 600, "ms": 300}
NOTIFICATION_CENTER = {"x1": 67, "y1": 17, "x2": 67, "y2": 467, "ms": 250}
CONTROL_CENTER = {"x1": 367, "y1": 17, "x2": 367, "y2": 467, "ms": 250}
APP_SWITCHER = {"x1": 215, "y1": 930, "x2": 215, "y2": 467, "ms": 500}
NEXT_PAGE = {"x1": 400, "y1": 467, "x2": 33, "y2": 467, "ms": 250}
PREV_PAGE = {"x1": 33, "y1": 467, "x2": 400, "y2": 467, "ms": 250}


def app_position(name: str) -> tuple[int, int] | None:
    """Return preview (x, y) for an app on the home screen or dock."""
    key = name.lower().strip()

    if key in DOCK_APPS:
        col = DOCK_APPS[key]
        return (DOCK_COLS[col], DOCK_Y)

    if key in HOME_SCREEN_APPS:
        col, row = HOME_SCREEN_APPS[key]
        return (GRID_COLS[col], GRID_ROWS[row])

    return None
