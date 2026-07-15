from .creepage_checker import CreepageCheckerPlugin

# Safely instantiate and register the action plugin into the menu tree structure
try:
    plugin = CreepageCheckerPlugin()
    plugin.register()
except Exception as e:
    print(f"Creepage Checker UI initialization failed: {e}")