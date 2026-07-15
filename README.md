# Creepage Checker

KiCad 10 ActionPlugin for automated creepage distance verification per IEC 60664-1:2020.
The credit goes to Claude AI since my programming skills are limited!

## Features
- Exact segment-to-segment closest-point creepage measurement (Ericson's method)
- IEC 60664-1:2020 Table F.5 working-voltage lookup with linear interpolation (10V–1000V)
- Cross-layer measurement via pure dielectric gap (copper thickness excluded)
- Obstacle-aware routing around third-net pads/zones
- File-based .kicad_pcb stackup parsing
- wxPython configuration dialog for net pair selection, pollution degree, and material group

## Installation
1. Copy `creepage_checker.py`, `__init__.py`, and `creepage_checker.png` into your KiCad scripting/plugins folder
2. Restart KiCad or refresh plugins

## Usage
Select the plugin from the KiCad PCB Editor toolbar, choose your net pair, and either enter a required creepage distance directly or specify a working voltage for automatic IEC 60664-1 lookup.

![Creepage Checker button](images/creepage_checker_button.png)

![Configuration window before plugin execution](images/popup_window_before_run.png)

![Zone to Pad creepage around PCB slot](images/Example1.png)

![Pad to Pad creepage around PCB slot](images/Example2.png)

![PCB stackup](images/Example3_stackup.png)

![Top copper to slot edge distance](images/Example3_top.png)

![Slot edge to In.2 copper distance](images/Example3_In2.png)

![Creepage by a third copper geometry](images/Example4.png)

## Notes
Creepage distance requirements are based on Table F.5 of IEC 60664-1:2020.

## License
GPL-3.0
