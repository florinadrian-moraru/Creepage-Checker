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
*The Creepage Checker toolbar button in KiCad's PCB Editor*

![Configuration window before plugin execution](images/popup_window_before_run.png)
*The Creepage Checker configuration window*

![Zone to Pad creepage around PCB slot](images/Example1.png)
*Zone to Pad creepage around PCB slot*

![Pad to Pad creepage around PCB slot](images/Example2.png)
*Pad to Pad creepage around PCB slot*

![PCB stackup](images/Example3_stackup.png)
*PCB stackup*

![Top copper to slot edge distance](images/Example3_top.png)
*Top copper to slot edge distance*

![Slot edge to In.2 copper distance](images/Example3_In2.png)
*Slot edge to In.2 copper distance*

![Creepage by a third copper geometry](images/Example4.png)
*Creepage by a third copper geometry*

![trace to zone creepage distance](images/Example5.png)
*Trace to zone creepage distance*

## Notes
Creepage distance requirements are based on Table F.5 of IEC 60664-1:2020.

## Changelog

### v188.1
- Added detailed raw-edge diagnostic logging for the winning HV+/HV- edge pair in the global direct search

### v188.0
- Added optional "Refill net A/B zones before measuring" checkbox to avoid stale zone-fill geometry affecting results

### v187.8
- Added Global Direct Minimum Search — exact segment-to-segment closest-point calculation independent of the 25µm pathfinding grid

### v187.7
- Fixed track geometry modeling to use KiCad's actual tessellated shape (round end caps), correcting a measurement discrepancy against KiCad's ruler tool

### v187.4
- Initial GitHub release

## License
GPL-3.0
