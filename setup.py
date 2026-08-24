from setuptools import setup

APP_ID = "com.gamefixsps.WiiBackupManager"
ICON_SIZES = ["16", "32", "48", "64", "128", "256", "512"]

data_files = [
    ("share/applications", [f"data/{APP_ID}.desktop"]),
]
for size in ICON_SIZES:
    data_files.append(
        (
            f"share/icons/hicolor/{size}x{size}/apps",
            [f"data/icons/hicolor/{size}x{size}/apps/{APP_ID}.png"],
        )
    )

setup(data_files=data_files)
