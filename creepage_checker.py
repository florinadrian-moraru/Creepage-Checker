import pcbnew
import math
import heapq
import time
import os
import wx

class TrueGeometricCreepageEngine(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "KiCad 10 True Surface Creepage Solver"
        self.category = "Verification"
        self.description = "Calculates true 3D surface creepage (Zone Extractor & MicroVias)."
        self.show_toolbar_button = True
        try:
            _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creepage_checker.png")
            if os.path.isfile(_icon_path):
                self.icon_file_name = _icon_path
                self.dark_icon_file_name = _icon_path
        except Exception:
            pass

    def Run(self):
        t0 = time.time()
        board = pcbnew.GetBoard()
        BUILD_VERSION = "v189.0"

        # =====================================================================
        # NET SELECTION + IEC 60664-1 PARAMETER DIALOG
        # =====================================================================
        net_names_all = sorted(set(
            nc.GetNetname() for nc in board.GetNetInfo().NetsByNetcode().values()
            if nc.GetNetname()
        ))
        if len(net_names_all) < 2:
            wx.MessageBox("Board has fewer than 2 named nets — nothing to check.",
                           BUILD_VERSION, wx.OK | wx.ICON_ERROR)
            return

        dlg_result = self.show_config_dialog(net_names_all)
        if dlg_result is None:
            return  # user cancelled
        NET_A_NAME = dlg_result["net_a"]
        NET_B_NAME = dlg_result["net_b"]
        input_mode = dlg_result["mode"]              # "voltage" or "distance"
        voltage_v = dlg_result["voltage_v"]
        direct_distance_mm = dlg_result["distance_mm"]
        pollution_degree = dlg_result["pollution_degree"]
        material_group = dlg_result["material_group"]
        use_pwb = dlg_result["use_pwb"]

        required_creepage_mm = None
        required_creepage_note = None
        if input_mode == "voltage":
            required_creepage_mm, required_creepage_note = iec_f5_creepage_mm(
                voltage_v, pollution_degree, material_group, use_pwb)
            if required_creepage_mm is None:
                wx.MessageBox(required_creepage_note or "Could not compute required creepage.",
                               BUILD_VERSION, wx.OK | wx.ICON_ERROR)
                return
        else:
            required_creepage_mm = direct_distance_mm

        # =====================================================================
        # PROGRESS DIALOG — KiCad plugins run synchronously on the main thread,
        # so true background progress isn't available; Pulse() at each stage
        # boundary (plus periodically inside the pathfinding loop, the usual
        # bottleneck on a large board) at least shows it's alive and roughly
        # where it is, rather than looking frozen for however long a big board
        # with hundreds of obstacles takes. Not a strict percentage — Pulse()
        # just animates and updates the message text.
        # =====================================================================
        progress_dlg = None
        try:
            progress_dlg = wx.ProgressDialog(
                f"{BUILD_VERSION} — Creepage Analysis",
                "Starting creepage analysis, this may take a while on a complex board...",
                maximum=100, parent=None,
                style=wx.PD_APP_MODAL | wx.PD_ELAPSED_TIME | wx.PD_SMOOTH)
        except Exception:
            progress_dlg = None

        _progress_last_call = [0.0]  # mutable single-element container for closure access

        def _progress(msg, force=False):
            # Time-throttled, not iteration-count-throttled: a fixed "every N
            # iterations" gate silently stops updating entirely whenever a
            # stage has fewer than N iterations total (exactly what caused the
            # elapsed-time display to freeze during a short via-conductor
            # search) — throttling by wall-clock time instead means the UI
            # stays live regardless of how work happens to be distributed.
            # Cheap enough to call on every iteration of any loop.
            if progress_dlg is None:
                return
            now = time.time()
            if not force and (now - _progress_last_call[0]) < 0.25:
                return
            _progress_last_call[0] = now
            try:
                progress_dlg.Pulse(msg)
            except Exception:
                pass

        try:
            settings = board.GetDesignSettings()
            settings.m_BlindBuriedViaAllowed = True
            settings.m_MicroViasAllowed = True
        except: pass

        comment_layer = board.GetLayerID("User.Comments")
        edge_cuts_layer_id = board.GetLayerID("Edge.Cuts")
        magic_width = pcbnew.FromMM(0.152)
        magic_via_width = pcbnew.FromMM(0.302)  # kept only for cleanup of vias from prior builds
        magic_marker_width = pcbnew.FromMM(0.06)  # contact-point circles from a previous run

        # ActionPlugin print() output goes to KiCad's own stdout, which isn't
        # visible unless KiCad was launched from a terminal. Diagnostics are
        # instead written to a log file next to the script, plus an optional
        # popup dialog (wx is the toolkit KiCad itself runs on, so it's always
        # available). Set DEBUG_POPUP True to also show the log in a dialog
        # after each run; by default it's written to creepage_debug_log.txt only.
        DEBUG_POPUP = False

        diag_lines = []
        def flush_diagnostics():
            text = "\n".join(diag_lines)
            log_path = None
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
            except Exception:
                base_dir = os.path.expanduser("~")
            try:
                log_path = os.path.join(base_dir, "creepage_debug_log.txt")
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                log_path = None
                diag_lines.append(f"Failed to write log file: {e}")
            if DEBUG_POPUP:
                try:
                    msg = f"[{BUILD_VERSION}] Diagnostic log written.\n\n"
                    msg += f"File: {log_path}\n\n" if log_path else "Could not write log file — see below.\n\n"
                    preview = text if len(text) < 3000 else text[:3000] + "\n...(truncated, see file)..."
                    msg += preview
                    dlg = wx.MessageDialog(None, msg, f"{BUILD_VERSION} Diagnostics", wx.OK | wx.ICON_INFORMATION | wx.STAY_ON_TOP)
                    dlg.ShowModal()
                    dlg.Destroy()
                except Exception:
                    pass

        diag_lines.append(f"===== NET SELECTION / IEC 60664-1 CONFIG =====")
        diag_lines.append(f"Net A = '{NET_A_NAME}'  |  Net B = '{NET_B_NAME}'")
        if input_mode == "voltage":
            diag_lines.append(f"Mode: voltage input. Working voltage = {voltage_v:.2f} V, "
                               f"Pollution degree = {pollution_degree}, "
                               f"Source = {'Printed wiring material' if use_pwb else f'Material group {material_group}'}")
            diag_lines.append(f"IEC 60664-1:2020 Table F.5 lookup (with linear interpolation where needed): "
                               f"required creepage = {required_creepage_mm:.4f} mm"
                               + (f" — {required_creepage_note}" if required_creepage_note else ""))
        else:
            diag_lines.append(f"Mode: direct creepage distance input. Required creepage = {required_creepage_mm:.4f} mm "
                               f"(entered directly, no IEC table lookup performed)")

        # =====================================================================
        # TARGETED ZONE REFILL (opt-in) — GetFilledPolysList() returns whatever
        # was last computed by KiCad, not a live recompute. If a zone on net A
        # or net B was edited (e.g. a trace moved) since its last fill, the
        # geometry this tool measures against would be stale. Refilling fixes
        # that, but forcibly filling zones is a real edit to the board with
        # its own side effects — on some HV designs a zone may be left
        # deliberately unfilled specifically to avoid triggering fill-time
        # conflicts unrelated to the clearance question being checked here.
        # So this only runs if explicitly requested in the dialog, and even
        # then touches ONLY zones on net A or net B, never any other net.
        # =====================================================================
        refill_ab_zones = dlg_result["refill_ab_zones"]
        _zones_on_ab = [z for z in board.Zones() if z.GetNetname() in (NET_A_NAME, NET_B_NAME)]
        if refill_ab_zones:
            try:
                if _zones_on_ab and hasattr(pcbnew, "ZONE_FILLER"):
                    _filler = pcbnew.ZONE_FILLER(board)
                    _filler.Fill(_zones_on_ab)
                    diag_lines.append(f"Refilled {len(_zones_on_ab)} zone(s) on net A/B before analysis "
                                       f"(requested in dialog). No other net's zones were touched.")
                elif _zones_on_ab:
                    diag_lines.append(f"WARNING: refill was requested, but pcbnew.ZONE_FILLER is unavailable "
                                       f"in this KiCad build — could not force a refill. Refill manually "
                                       f"(Edit > Fill All Zones) before running if unsure.")
            except Exception as _refill_e:
                diag_lines.append(f"WARNING: zone refill for net A/B failed ({type(_refill_e).__name__}: {_refill_e}) "
                                   f"— proceeding with whatever fill data is currently available, which may be stale.")
        elif _zones_on_ab:
            diag_lines.append(f"NOTE: {len(_zones_on_ab)} zone(s) on net A/B were NOT refilled (not requested). "
                               f"If either net's zone geometry was edited since its last manual fill in KiCad, "
                               f"this analysis is working from that stale fill data, not current geometry. "
                               f"Refill manually first, or re-run with the refill option checked, if unsure.")

        # =====================================================================
        # BOARD INVENTORY DUMP — logs every Edge.Cuts shape, NPTH pad, and any
        # pad/via/zone on a third net (neither net A nor net B). Useful for
        # troubleshooting when an obstacle isn't being routed around as
        # expected: this makes it easy to check whether the object in question
        # was actually detected and classified correctly.
        # =====================================================================
        try:
            def _shape_type_name(shape_val):
                names = {
                    getattr(pcbnew, 'SHAPE_T_SEGMENT', object()): "SEGMENT",
                    getattr(pcbnew, 'SHAPE_T_RECT', object()): "RECT",
                    getattr(pcbnew, 'SHAPE_T_ARC', object()): "ARC",
                    getattr(pcbnew, 'SHAPE_T_CIRCLE', object()): "CIRCLE",
                    getattr(pcbnew, 'SHAPE_T_POLY', object()): "POLY",
                    getattr(pcbnew, 'SHAPE_T_BEZIER', object()): "BEZIER",
                }
                return names.get(shape_val, f"UNKNOWN({shape_val})")

            diag_lines.append(f"===== BOARD INVENTORY =====")

            _dbg_edge_cuts = [d for d in board.GetDrawings() if d.GetLayer() == edge_cuts_layer_id]
            diag_lines.append(f"Edge.Cuts shapes: {len(_dbg_edge_cuts)}")
            for idx, dwg in enumerate(_dbg_edge_cuts):
                try:
                    shp = dwg.GetShape() if hasattr(dwg, "GetShape") else None
                    bbox = dwg.GetBoundingBox()
                    diag_lines.append(f"  EdgeCuts[{idx}] type={_shape_type_name(shp)} "
                          f"bbox=({pcbnew.ToMM(bbox.GetX()):.3f},{pcbnew.ToMM(bbox.GetY()):.3f}) to "
                          f"({pcbnew.ToMM(bbox.GetRight()):.3f},{pcbnew.ToMM(bbox.GetBottom()):.3f}) mm")
                except Exception as e:
                    diag_lines.append(f"  EdgeCuts[{idx}] ERROR: {e}")

            _dbg_npth = [p for p in board.GetPads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH]
            diag_lines.append(f"NPTH pads: {len(_dbg_npth)}")
            for idx, pad in enumerate(_dbg_npth):
                try:
                    pos = pad.GetPosition()
                    diag_lines.append(f"  NPTH[{idx}] pos=({pcbnew.ToMM(pos.x):.3f},{pcbnew.ToMM(pos.y):.3f}) "
                          f"size=({pcbnew.ToMM(pad.GetSizeX()):.3f}x{pcbnew.ToMM(pad.GetSizeY()):.3f})mm")
                except Exception as e:
                    diag_lines.append(f"  NPTH[{idx}] ERROR: {e}")

            diag_lines.append(f"Pads on neither net A nor net B and not NPTH (third-net obstacle candidates):")
            _other_count = 0
            for pad in board.GetPads():
                try:
                    if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH: continue
                    net_name = pad.GetNetname()
                    if net_name and (net_name == NET_A_NAME or net_name == NET_B_NAME): continue
                    pos = pad.GetPosition()
                    diag_lines.append(f"  Pad net='{net_name}' pos=({pcbnew.ToMM(pos.x):.3f},{pcbnew.ToMM(pos.y):.3f}) "
                          f"size=({pcbnew.ToMM(pad.GetSizeX()):.3f}x{pcbnew.ToMM(pad.GetSizeY()):.3f})mm "
                          f"shape={pad.GetShapeString() if hasattr(pad,'GetShapeString') else '?'}")
                    _other_count += 1
                except Exception as e:
                    diag_lines.append(f"  Pad ERROR: {e}")
            if _other_count == 0: diag_lines.append(f"  (none found)")

            diag_lines.append(f"Vias on neither net A nor net B (third-net obstacle candidates):")
            _via_count = 0
            for trk in board.GetTracks():
                if isinstance(trk, pcbnew.PCB_VIA):
                    try:
                        net_name = trk.GetNetname()
                        if net_name and (net_name == NET_A_NAME or net_name == NET_B_NAME): continue
                        if trk.GetWidth() == magic_via_width: continue  # skip our own leftover vias
                        pos = trk.GetPosition()
                        diag_lines.append(f"  Via net='{net_name}' pos=({pcbnew.ToMM(pos.x):.3f},{pcbnew.ToMM(pos.y):.3f}) "
                              f"width={pcbnew.ToMM(trk.GetWidth()):.3f}mm")
                        _via_count += 1
                    except Exception as e:
                        diag_lines.append(f"  Via ERROR: {e}")
            if _via_count == 0: diag_lines.append(f"  (none found)")

            diag_lines.append(f"Zones on neither net A nor net B (third-net obstacle candidates):")
            _zone_count = 0
            for zone in board.Zones():
                try:
                    net_name = zone.GetNetname()
                    if net_name and (net_name == NET_A_NAME or net_name == NET_B_NAME): continue
                    bbox = zone.GetBoundingBox()
                    zlayers = list(zone.GetLayerSet().CuStack()) if hasattr(zone, "GetLayerSet") else [zone.GetLayer()]
                    zlayer_names = [board.GetLayerName(l) for l in zlayers]
                    diag_lines.append(f"  Zone net='{net_name}' layers={zlayer_names} "
                          f"bbox=({pcbnew.ToMM(bbox.GetX()):.3f},{pcbnew.ToMM(bbox.GetY()):.3f}) to "
                          f"({pcbnew.ToMM(bbox.GetRight()):.3f},{pcbnew.ToMM(bbox.GetBottom()):.3f})mm")
                    _zone_count += 1
                except Exception as e:
                    diag_lines.append(f"  Zone ERROR: {e}")
            if _zone_count == 0: diag_lines.append(f"  (none found)")
        except Exception as _diag_e:
            diag_lines.append(f"Diagnostic block failed (non-fatal): {_diag_e}")

        for d in list(board.GetDrawings()):
            _d_width = d.GetWidth() if hasattr(d, 'GetWidth') else None
            _d_text = d.GetText() if hasattr(d, 'GetText') else ""
            if d.GetLayer() in [comment_layer, pcbnew.F_SilkS, pcbnew.B_SilkS, pcbnew.Dwgs_User, pcbnew.F_Adhes, pcbnew.B_Adhes] and (_d_width == magic_width or _d_width == magic_marker_width or ("CREEPAGE" in _d_text.upper()) or ("IEC REQUIRED" in _d_text.upper()) or ("NO PATH FOUND" in _d_text.upper()) or ("NO COPPER NODES" in _d_text.upper())):
                board.Remove(d)
        for t in list(board.GetTracks()):
            if isinstance(t, pcbnew.PCB_VIA) and t.GetWidth() == magic_via_width:
                board.Remove(t)

        # Search-grid resolution kept at full 25-micron precision intentionally — the
        # grid is only used for A* TOPOLOGY (which side of an obstacle to route
        # around), but coarsening it risks missing a genuinely narrow path between
        # close obstacles and silently OVERSTATING the minimum creepage distance,
        # which is the unsafe direction of error for a clearance tool. Performance
        # instead comes from lazy/memoized evaluation below (only compute what the
        # search actually visits) rather than from reducing resolution.
        res_iu = pcbnew.FromMM(0.025)
        poly_error_iu = int(pcbnew.FromMM(0.01))

        enabled_copper_layers = []
        for i in range(128):
            if board.IsLayerEnabled(i) and pcbnew.IsCopperLayer(i):
                enabled_copper_layers.append(i)
        enabled_copper_layers.sort()

        # ---------------------------------------------------------------------
        # STACKUP Z EXTRACTION
        # Three-tier approach, in priority order:
        #   1. MANUAL_STACKUP_MM dict below (explicit override, always wins if set)
        #   2. Parse the .kicad_pcb file directly — bypasses the SWIG binding gap
        #      that makes GetStackupDescriptor() return an untyped SwigPyObject on
        #      some KiCad 10 builds. The file contains the exact same stackup data
        #      in well-defined S-expression format and is always consistent with
        #      what KiCad loaded, so this is more reliable than the Python API.
        #   3. Python API via GetStackupDescriptor() — kept as secondary attempt
        #      in case the file can't be read (e.g. board not yet saved to disk)
        #   4. Proportional spacing fallback (least accurate, clearly flagged)
        #
        # MANUAL OVERRIDE: fill this dict if all auto-detection fails.
        # Key = layer name exactly as shown in Board Setup, value = Z of that
        # layer's top surface in mm measured from F.Cu = 0.
        # Example: F.Cu=0, 0.035mm copper + 0.1mm prepreg, In1.Cu=0.135,
        #          0.035mm + 1.24mm core, In2.Cu=1.41, 0.035mm + 0.1mm + B.Cu=1.545
        MANUAL_STACKUP_MM = {}

        layer_z_mm = {}
        copper_thickness_mm = {}  # per-layer copper thickness (mm) for dielectric-gap-only Z distance

        # Priority 1: manual override
        if MANUAL_STACKUP_MM:
            for l in enabled_copper_layers:
                lname = board.GetLayerName(l)
                if lname in MANUAL_STACKUP_MM:
                    layer_z_mm[l] = MANUAL_STACKUP_MM[lname]
            if layer_z_mm:
                diag_lines.append(f"Stackup Z: MANUAL_STACKUP_MM matched {len(layer_z_mm)}/{len(enabled_copper_layers)} layers.")

        # Priority 2: parse .kicad_pcb file directly (bypasses broken SWIG binding)
        if len(layer_z_mm) < len(enabled_copper_layers):
            try:
                import re as _re
                _fname = board.GetFileName()
                if _fname:
                    with open(_fname, 'r', encoding='utf-8', errors='replace') as _f:
                        _content = _f.read()
                    # Find and extract the (stackup ...) block
                    _si = _content.find('(stackup')
                    if _si >= 0:
                        _depth = 0; _se = -1
                        for _ci in range(_si, len(_content)):
                            if _content[_ci] == '(': _depth += 1
                            elif _content[_ci] == ')':
                                _depth -= 1
                                if _depth == 0: _se = _ci + 1; break
                    if _si >= 0 and _se > 0:
                        _stk = _content[_si:_se]
                        # Build name→layer_id map
                        _name_to_id = {board.GetLayerName(l): l for l in enabled_copper_layers}
                        # Walk through layer sub-blocks in order, accumulating Z
                        _cur_z = 0.0; _i = 0
                        while True:
                            _li = _stk.find('(layer ', _i)
                            if _li < 0: break
                            # Find the closing paren of this (layer ...) block
                            _d2 = 0; _le = -1
                            for _cj in range(_li, len(_stk)):
                                if _stk[_cj] == '(': _d2 += 1
                                elif _stk[_cj] == ')':
                                    _d2 -= 1
                                    if _d2 == 0: _le = _cj + 1; break
                            if _le < 0: break
                            _blk = _stk[_li:_le]
                            _i = _le
                            # Extract name, type, thickness from block
                            _nm = _re.search(r'\(layer\s+"([^"]+)"', _blk)
                            _ty = _re.search(r'\(type\s+"([^"]+)"', _blk)
                            _th = _re.search(r'\(thickness\s+([\d.]+)\)', _blk)
                            if not _nm or not _ty: continue
                            _lname = _nm.group(1); _ltype = _ty.group(1).lower()
                            _thick = float(_th.group(1)) if _th else 0.0
                            # Record Z for copper layers (only if not already set by manual)
                            if ('copper' in _ltype or _lname in _name_to_id):
                                if _lname in _name_to_id and _name_to_id[_lname] not in layer_z_mm:
                                    _lid2 = _name_to_id[_lname]
                                    layer_z_mm[_lid2] = _cur_z
                                    if _thick > 0: copper_thickness_mm[_lid2] = _thick
                            # Advance Z for copper and dielectric layers only
                            # (mask/paste/silkscreen don't contribute to stackup thickness)
                            if 'copper' in _ltype or 'preg' in _ltype or 'core' in _ltype or 'dielectric' in _ltype:
                                _cur_z += _thick
                        _parsed = [l for l in enabled_copper_layers if l in layer_z_mm]
                        diag_lines.append(f"Stackup Z: parsed {len(_parsed)}/{len(enabled_copper_layers)} layers from .kicad_pcb file.")
            except Exception as _fe:
                diag_lines.append(f"Stackup Z: file parse failed ({type(_fe).__name__}: {_fe})")

        # Priority 3: Python API via GetStackupDescriptor()
        if len(layer_z_mm) < len(enabled_copper_layers):
            try:
                _stackup = board.GetDesignSettings().GetStackupDescriptor()
                _z_iu = 0; _seen = 0
                for _item in _stackup.GetList():
                    _seen += 1
                    _is_cu = False
                    try: _is_cu = _item.IsCopper()
                    except Exception: pass
                    _is_di = False
                    if not _is_cu:
                        try: _is_di = 'DIELECTRIC' in str(_item.GetType()).upper()
                        except Exception: pass
                    if _is_cu:
                        try:
                            _lid = _item.GetBrdLayerId()
                            if _lid in enabled_copper_layers and _lid not in layer_z_mm:
                                layer_z_mm[_lid] = pcbnew.ToMM(_z_iu)
                        except Exception: pass
                    if _is_cu or _is_di:
                        _th2 = 0
                        try: _th2 = _item.GetThickness()
                        except TypeError:
                            try: _th2 = _item.GetThickness(0)
                            except Exception: _th2 = 0
                        except Exception: _th2 = 0
                        _z_iu += _th2
                if _seen:
                    diag_lines.append(f"Stackup Z: Python API read {_seen} items.")
            except Exception as _pe:
                diag_lines.append(f"Stackup Z: Python API failed ({type(_pe).__name__}: {_pe})")

        # Priority 4: proportional fallback (clearly flagged as inaccurate)
        missing_z = [l for l in enabled_copper_layers if l not in layer_z_mm]
        if missing_z:
            b_thick = pcbnew.ToMM(board.GetDesignSettings().GetBoardThickness())
            if not b_thick or b_thick <= 0: b_thick = 1.6
            for idx, l in enumerate(enabled_copper_layers):
                if l not in layer_z_mm:
                    layer_z_mm[l] = (b_thick / (len(enabled_copper_layers) - 1)) * idx if len(enabled_copper_layers) > 1 else 0.0
            diag_lines.append(f"Stackup Z: WARNING — {len(missing_z)} layer(s) missing, used proportional fallback "
                               f"(Z distances will be wrong for asymmetric stackups like yours). "
                               f"Check that the board file has been saved after setting up the stackup in Board Setup.")

        diag_lines.append(f"Stackup Z positions (mm): " + ", ".join(f"{board.GetLayerName(l)}={layer_z_mm.get(l,0):.4f}" for l in enabled_copper_layers))

        def layer_gap_mm(l1, l2):
            # Pure dielectric gap between the bottom of l1 copper and the top of
            # l2 copper (or vice versa if l2 is above l1). This matches the IEC
            # definition: the surface path travels along the exposed conductor
            # edge, then through the dielectric only (not through the copper on
            # either side). The copper thicknesses are subtracted from the layer
            # Z positions, leaving only the insulating gap between them.
            z1 = layer_z_mm.get(l1, 0.0); z2 = layer_z_mm.get(l2, 0.0)
            t1 = copper_thickness_mm.get(l1, 0.0); t2 = copper_thickness_mm.get(l2, 0.0)
            if z1 <= z2:
                # l1 is above (smaller z): gap = top_of_l2 - bottom_of_l1
                return max(0.0, z2 - (z1 + t1))
            else:
                # l2 is above (smaller z): gap = top_of_l1 - bottom_of_l2
                return max(0.0, z1 - (z2 + t2))

        t1 = time.time()
        diag_lines.append(f"Timing - Stackup extraction: {t1-t0:.2f}s")
        _progress("Extracting net A/B copper geometry...", force=True)

        # =====================================================================
        # GEOMETRY EXTRACTION - NET A / NET B COPPER (the two nets being measured)
        # =====================================================================
        hvp_nodes, hvm_nodes = {}, {}
        exact_hvp_edges, exact_hvm_edges = [], []
        # Layered versions — (x1,y1,x2,y2,layer) — used by global optimizer for Z-distance
        exact_hvp_edges_layered, exact_hvm_edges_layered = [], []
        conductor_polys_by_layer = {}  # {layer_id: [ring_IU, ...]} third-net conductors for via-conductor shortcut search

        def add_node(target_dict, layer, y, x):
            if layer not in enabled_copper_layers: return
            if layer not in target_dict: target_dict[layer] = set()
            target_dict[layer].add((y, x))

        def sample_chain(chain, net_name, layers):
            for k in range(chain.PointCount()):
                p1, p2 = chain.CPoint(k), chain.CPoint((k + 1) % chain.PointCount())
                if net_name == NET_A_NAME:
                    exact_hvp_edges.append((p1.x, p1.y, p2.x, p2.y))
                    for l in layers: exact_hvp_edges_layered.append((p1.x, p1.y, p2.x, p2.y, l))
                elif net_name == NET_B_NAME:
                    exact_hvm_edges.append((p1.x, p1.y, p2.x, p2.y))
                    for l in layers: exact_hvm_edges_layered.append((p1.x, p1.y, p2.x, p2.y, l))
                dist = math.hypot(p2.x - p1.x, p2.y - p1.y)
                if dist == 0: continue
                steps = max(1, int(dist / res_iu))
                for s in range(steps + 1):
                    t = s / float(steps)
                    gx = int(round((p1.x + t * (p2.x - p1.x)) / res_iu))
                    gy = int(round((p1.y + t * (p2.y - p1.y)) / res_iu))
                    for l in layers:
                        if net_name == NET_A_NAME: add_node(hvp_nodes, l, gy, gx)
                        elif net_name == NET_B_NAME: add_node(hvm_nodes, l, gy, gx)

        def sample_poly_set_fully(poly_set, net_name, layers):
            for p_idx in range(poly_set.OutlineCount()):
                sample_chain(poly_set.Outline(p_idx), net_name, layers)
                for h_idx in range(poly_set.HoleCount(p_idx)):
                    sample_chain(poly_set.Hole(p_idx, h_idx), net_name, layers)

        for zone in board.Zones():
            net_name = zone.GetNetname()
            if not net_name: continue
            layers = list(zone.GetLayerSet().CuStack()) if hasattr(zone, "GetLayerSet") else [zone.GetLayer()]
            for layer in layers:
                if hasattr(zone, "GetFilledPolysList"):
                    poly_set = zone.GetFilledPolysList(layer)
                    if poly_set.OutlineCount() > 0:
                        sample_poly_set_fully(poly_set, net_name, [layer])

        for pad in board.GetPads():
            if pad.GetNetname():
                poly = pcbnew.SHAPE_POLY_SET()
                if hasattr(pad, "GetEffectiveShape"):
                    pad.GetEffectiveShape(pad.GetLayer()).TransformToPolygon(poly, poly_error_iu, 0)
                    sample_poly_set_fully(poly, pad.GetNetname(), list(pad.GetLayerSet().CuStack()))

        for track in board.GetTracks():
            if isinstance(track, pcbnew.PCB_TRACK) and track.GetNetname():
                # Use KiCad's own effective shape rather than hand-building a flat
                # rectangle from the segment's start/end ± half-width: a real track
                # segment's copper has ROUNDED end caps (radius = width/2), the same
                # capsule/stadium shape KiCad itself uses for DRC and collision. A
                # flat-rectangle approximation is missing that cap's bulge, which
                # matters most exactly at a bend — the outer/convex side of a joint
                # between two angled segments is where the true copper extends
                # furthest toward nearby copper, via the cap, not the flat edge.
                if hasattr(track, "GetEffectiveShape"):
                    poly = pcbnew.SHAPE_POLY_SET()
                    track.GetEffectiveShape().TransformToPolygon(poly, poly_error_iu, 0)
                    sample_poly_set_fully(poly, track.GetNetname(), list(track.GetLayerSet().CuStack()))
                else:
                    # Fallback for older API without GetEffectiveShape(): flat
                    # rectangle only, no end caps — less accurate at bends/ends.
                    p1, p2, w = track.GetStart(), track.GetEnd(), track.GetWidth()
                    r = w // 2
                    dx, dy = p2.x - p1.x, p2.y - p1.y
                    dist = math.hypot(dx, dy)
                    if dist > 0:
                        nx, ny = int(-(dy/dist)*r), int((dx/dist)*r)
                        chain = pcbnew.SHAPE_LINE_CHAIN()
                        chain.Append(p1.x+nx, p1.y+ny); chain.Append(p2.x+nx, p2.y+ny)
                        chain.Append(p2.x-nx, p2.y-ny); chain.Append(p1.x-nx, p1.y-ny)
                        chain.SetClosed(True)
                        sample_chain(chain, track.GetNetname(), list(track.GetLayerSet().CuStack()))

        all_rows, all_cols = [], []
        for l_dict in [hvp_nodes, hvm_nodes]:
            for nodes in l_dict.values():
                for r, c in nodes:
                    all_rows.append(r); all_cols.append(c)

        if not all_rows:
            self.draw_text(board, comment_layer, f"NO COPPER NODES FOUND FOR '{NET_A_NAME}' TO '{NET_B_NAME}'", None)
            flush_diagnostics()
            if progress_dlg is not None:
                progress_dlg.Destroy()
            return

        min_r, max_r = min(all_rows), max(all_rows)
        min_c, max_c = min(all_cols), max(all_cols)
        pad_buffer = int(3.0 / pcbnew.ToMM(res_iu))
        grid_min_r, grid_max_r = min_r - pad_buffer, max_r + pad_buffer
        grid_min_c, grid_max_c = min_c - pad_buffer, max_c + pad_buffer
        rows, cols = grid_max_r - grid_min_r + 1, grid_max_c - grid_min_c + 1

        t2 = time.time()
        diag_lines.append(f"Timing - Copper extraction: {t2-t1:.2f}s")
        _progress("Extracting obstacles (pads, vias, zones, slots)...", force=True)

        # =====================================================================
        # OBSTACLE EXTRACTION - STITCHING & SOLIDIFYING
        # Arc/circle tessellation relaxed from 10um to 30um target arc-length
        # step: at any realistic radius this is sub-micron sagitta error (totally
        # negligible for creepage purposes) while cutting obstacle edge counts
        # roughly 3x, which directly speeds up every downstream point-in-polygon
        # and segment-intersection test.
        # =====================================================================
        obstacle_polygons = []
        obstacle_kind = []  # parallel to obstacle_polygons: 'npth' / 'slot_or_edge' / 'third_net' — used to tag
                             # each Z-transition in the path with what kind of exposed discontinuity it passes
                             # through, so pollution-degree judgment calls (coating exposure, slot walls, etc.)
                             # can be made with the facts in hand rather than the tool guessing at compliance.
        obstacle_holes = []  # parallel to obstacle_polygons: list of hole-ring-point-lists cut out of that
                              # obstacle's solid area (e.g. zone clearance around other-net copper). A point
                              # inside the outline but also inside one of these holes is NOT solid — see
                              # point_in_obstacle(). Empty list for obstacles with no holes (the normal case).
        exact_hole_edges = []

        npth_pads = [p for p in board.GetPads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH]
        edge_cuts = [d for d in board.GetDrawings() if d.GetLayer() == edge_cuts_layer_id]

        for pad in npth_pads:
            poly = pcbnew.SHAPE_POLY_SET()
            if hasattr(pad, "GetEffectiveShape"):
                pad.GetEffectiveShape(pad.GetLayer()).TransformToPolygon(poly, poly_error_iu, 0)
                for i in range(poly.OutlineCount()):
                    outl = poly.Outline(i)
                    ring = []
                    for k in range(outl.PointCount()):
                        p1h, p2h = outl.CPoint(k), outl.CPoint((k + 1) % outl.PointCount())
                        exact_hole_edges.append((p1h.x, p1h.y, p2h.x, p2h.y))
                        ring.append((p1h.x, p1h.y))
                    if len(ring) >= 3:
                        obstacle_polygons.append(ring)
                        obstacle_kind.append('npth')
                        obstacle_holes.append([])

        # Plated pads/vias belonging to neither net A nor net B (a third net, or
        # no net at all) have real copper sitting between the two nets being
        # measured, so the surface path must route around them too. This reuses
        # the exact same obstacle_polygons/exact_hole_edges lists used for NPTH
        # holes, so grid masking, exact snapping, and tangent-wrap correction
        # apply to these automatically with no separate code path.
        search_min_x, search_max_x = grid_min_c * res_iu, grid_max_c * res_iu
        search_min_y, search_max_y = grid_min_r * res_iu, grid_max_r * res_iu

        def _in_search_region(bbox):
            return not (bbox.GetRight() < search_min_x or bbox.GetX() > search_max_x or
                        bbox.GetBottom() < search_min_y or bbox.GetY() > search_max_y)

        for pad in board.GetPads():
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                continue
            net_name = pad.GetNetname()
            if net_name and (net_name == NET_A_NAME or net_name == NET_B_NAME):
                continue
            if not _in_search_region(pad.GetBoundingBox()):
                continue
            pad_layers = []
            try:
                pad_layers = list(pad.GetLayerSet().CuStack())
            except Exception:
                pad_layers = []
            if not pad_layers:
                pad_layers = [pad.GetLayer()]
            _pad_added = False
            _pad_errs = []
            for pad_layer in pad_layers:
                poly = pcbnew.SHAPE_POLY_SET()
                try:
                    pad.GetEffectiveShape(pad_layer).TransformToPolygon(poly, poly_error_iu, 0)
                    for i in range(poly.OutlineCount()):
                        outl = poly.Outline(i)
                        ring = []
                        for k in range(outl.PointCount()):
                            p1h, p2h = outl.CPoint(k), outl.CPoint((k + 1) % outl.PointCount())
                            exact_hole_edges.append((p1h.x, p1h.y, p2h.x, p2h.y))
                            ring.append((p1h.x, p1h.y))
                        if len(ring) >= 3:
                            obstacle_polygons.append(ring)
                            obstacle_kind.append('third_net')
                            obstacle_holes.append([])
                            conductor_polys_by_layer.setdefault(pad_layer, []).append(ring)
                            _pad_added = True
                except Exception as e:
                    _pad_errs.append(f"{board.GetLayerName(pad_layer)}: {type(e).__name__}: {e}")
            if not _pad_added:
                _pos = pad.GetPosition()
                diag_lines.append(f"WARNING: obstacle pad net='{pad.GetNetname()}' pos=({pcbnew.ToMM(_pos.x):.3f},{pcbnew.ToMM(_pos.y):.3f}) "
                                   f"produced NO usable polygon on any of {len(pad_layers)} layer(s) tried"
                                   f"{' — errors: ' + '; '.join(_pad_errs) if _pad_errs else ' (no exceptions, just empty outlines)'}")

        for trk in board.GetTracks():
            if isinstance(trk, pcbnew.PCB_VIA):
                net_name = trk.GetNetname()
                if net_name and (net_name == NET_A_NAME or net_name == NET_B_NAME):
                    continue
                if trk.GetWidth() == magic_via_width:
                    continue  # our own leftover via from a previous run, already cleaned up above in theory
                if not _in_search_region(trk.GetBoundingBox()):
                    continue
                pos = trk.GetPosition()
                r = trk.GetWidth() // 2
                if r > 0:
                    steps = max(32, int(2 * math.pi * r / pcbnew.FromMM(0.03)))
                    ring = []
                    for i in range(steps):
                        a = (i / float(steps)) * 2 * math.pi
                        ring.append((int(round(pos.x + r*math.cos(a))), int(round(pos.y + r*math.sin(a)))))
                    for i in range(len(ring)):
                        x1, y1 = ring[i]; x2, y2 = ring[(i + 1) % len(ring)]
                        exact_hole_edges.append((x1, y1, x2, y2))
                    obstacle_polygons.append(ring)
                    obstacle_kind.append('third_net')
                    obstacle_holes.append([])
        # Zones belonging to neither net A nor net B (e.g. a GND pour, possibly
        # on an inner layer) are real filled copper, so the surface path must
        # route around them too. A zone's fill routinely has holes cut out of
        # it — clearance around every other-net pad/trace it clears, including
        # net A/B's own copper — and those holes are NOT copper, so they must
        # be subtracted from the outline's solid area, not ignored. Ignoring
        # them would treat the zone's entire outer boundary as solid, which
        # can easily engulf net A/B's own copper on a real board with a large
        # ground pour, making a real path look nonexistent.
        _other_zone_count = 0
        for zone in board.Zones():
            net_name = zone.GetNetname()
            if net_name and (net_name == NET_A_NAME or net_name == NET_B_NAME):
                continue
            if not _in_search_region(zone.GetBoundingBox()):
                continue
            if not hasattr(zone, "GetFilledPolysList"):
                continue
            zone_layers = list(zone.GetLayerSet().CuStack()) if hasattr(zone, "GetLayerSet") else [zone.GetLayer()]
            _zone_added = False
            _zone_errs = []
            _zone_outline_counts = []
            for layer in zone_layers:
                try:
                    poly_set = zone.GetFilledPolysList(layer)
                except Exception as e:
                    _zone_errs.append(f"{board.GetLayerName(layer)}: {type(e).__name__}: {e}")
                    continue
                _zone_outline_counts.append(f"{board.GetLayerName(layer)}={poly_set.OutlineCount()}")
                for p_idx in range(poly_set.OutlineCount()):
                    outline = poly_set.Outline(p_idx)
                    n_pts = outline.PointCount()
                    ring = []
                    for k in range(n_pts):
                        p1z, p2z = outline.CPoint(k), outline.CPoint((k + 1) % n_pts)
                        exact_hole_edges.append((p1z.x, p1z.y, p2z.x, p2z.y))
                        ring.append((p1z.x, p1z.y))
                    if len(ring) >= 3:
                        holes_for_ring = []
                        for h_idx in range(poly_set.HoleCount(p_idx)):
                            hole_outline = poly_set.Hole(p_idx, h_idx)
                            n_hpts = hole_outline.PointCount()
                            hole_ring = []
                            for hk in range(n_hpts):
                                hp1, hp2 = hole_outline.CPoint(hk), hole_outline.CPoint((hk + 1) % n_hpts)
                                exact_hole_edges.append((hp1.x, hp1.y, hp2.x, hp2.y))
                                hole_ring.append((hp1.x, hp1.y))
                            if len(hole_ring) >= 3:
                                holes_for_ring.append(hole_ring)
                        obstacle_polygons.append(ring)
                        obstacle_kind.append('third_net')
                        obstacle_holes.append(holes_for_ring)
                        conductor_polys_by_layer.setdefault(layer, []).append(ring)
                        _other_zone_count += 1
                        _zone_added = True
            if not _zone_added:
                _zbb = zone.GetBoundingBox()
                diag_lines.append(f"NOTE: obstacle zone net='{net_name}' bbox=({pcbnew.ToMM(_zbb.GetX()):.3f},{pcbnew.ToMM(_zbb.GetY()):.3f}) to "
                                   f"({pcbnew.ToMM(_zbb.GetRight()):.3f},{pcbnew.ToMM(_zbb.GetBottom()):.3f}) "
                                   f"produced NO obstacle geometry on any of {len(zone_layers)} layer(s) tried "
                                   f"— outline counts: {_zone_outline_counts if _zone_outline_counts else '(none read)'}"
                                   f"{'; errors: ' + '; '.join(_zone_errs) if _zone_errs else ''}. "
                                   f"If this is unexpected, GetFilledPolysList() reflects the LAST fill computed "
                                   f"in KiCad, not a live recompute — an unfilled or stale-filled zone has no "
                                   f"realized copper and is correctly excluded here. If the zone is intentionally "
                                   f"left unfilled (e.g. reserved/placeholder copper not yet poured), this is "
                                   f"expected and requires no action.")
        if _other_zone_count:
            diag_lines.append(f"Added {_other_zone_count} third-net zone outline(s) as obstacles (net != A/B)")

        # Identify the board outline shape itself BEFORE processing any
        # Edge.Cuts geometry, rather than trying to recognize it after the fact
        # from the resulting polygons — a rectangular outline's
        # GetEffectiveShape() can decompose into four separate thin
        # edge-stroke slivers (one per side) rather than one large ring, so
        # there's no reliable "biggest ring wins" heuristic to apply
        # afterward. Instead, each shape's own bounding box is checked against
        # the combined extent of every Edge.Cuts shape before any
        # decomposition happens, which is robust regardless of how many
        # pieces a given shape later breaks into.
        _ec_min_x = _ec_min_y = _ec_max_x = _ec_max_y = None
        for _dwg in edge_cuts:
            try:
                _bbox = _dwg.GetBoundingBox()
                x1, y1, x2, y2 = _bbox.GetX(), _bbox.GetY(), _bbox.GetRight(), _bbox.GetBottom()
                if _ec_min_x is None:
                    _ec_min_x, _ec_min_y, _ec_max_x, _ec_max_y = x1, y1, x2, y2
                else:
                    _ec_min_x = min(_ec_min_x, x1); _ec_min_y = min(_ec_min_y, y1)
                    _ec_max_x = max(_ec_max_x, x2); _ec_max_y = max(_ec_max_y, y2)
            except Exception:
                pass

        def _is_likely_outline_shape(dwg):
            if _ec_min_x is None:
                return False
            try:
                bbox = dwg.GetBoundingBox()
                tol = pcbnew.FromMM(0.5)
                return (abs(bbox.GetX() - _ec_min_x) < tol and abs(bbox.GetY() - _ec_min_y) < tol and
                        abs(bbox.GetRight() - _ec_max_x) < tol and abs(bbox.GetBottom() - _ec_max_y) < tol)
            except Exception:
                return False

        raw_paths = []
        arc_metadata = []  # parallel to raw_paths: None for lines, (cx,cy,r,sa,ea) for arcs
        _outline_shapes_skipped = 0
        for dwg in edge_cuts:
            if not hasattr(dwg, "GetShape"): continue
            if _is_likely_outline_shape(dwg):
                _outline_shapes_skipped += 1
                continue
            shape = dwg.GetShape()
            pts = []
            arc_meta = None
            if shape == pcbnew.SHAPE_T_SEGMENT: pts = [dwg.GetStart(), dwg.GetEnd()]
            elif shape == pcbnew.SHAPE_T_ARC:
                cx, cy = dwg.GetCenter().x, dwg.GetCenter().y
                sx, sy = dwg.GetStart().x, dwg.GetStart().y
                ex, ey = dwg.GetEnd().x, dwg.GetEnd().y
                r = math.hypot(sx - cx, sy - cy)
                if r > 0:
                    sa, ea = math.atan2(sy - cy, sx - cx), math.atan2(ey - cy, ex - cx)
                    if hasattr(dwg, "GetMid"):
                        mx, my = dwg.GetMid().x, dwg.GetMid().y
                        ma = math.atan2(my - cy, mx - cx)
                        if ea < sa: ea += 2*math.pi
                        if ma < sa: ma += 2*math.pi
                        if ma > ea: sa, ea = ea, sa + 2*math.pi
                    else:
                        if ea < sa: ea += 2*math.pi
                    arc_meta = (cx, cy, r, sa, ea)
                    steps = max(32, int((r * abs(ea - sa)) / pcbnew.FromMM(0.03)))
                    pts.append(pcbnew.VECTOR2I(sx, sy))
                    for i in range(1, steps):
                        a = sa + (ea - sa) * (i / float(steps))
                        pts.append(pcbnew.VECTOR2I(int(round(cx + r*math.cos(a))), int(round(cy + r*math.sin(a)))))
                    pts.append(pcbnew.VECTOR2I(ex, ey))
            elif shape == pcbnew.SHAPE_T_CIRCLE:
                cx, cy = dwg.GetCenter().x, dwg.GetCenter().y
                r = dwg.GetRadius() if hasattr(dwg, "GetRadius") else math.hypot(dwg.GetStart().x - cx, dwg.GetStart().y - cy)
                if r > 0:
                    steps = max(48, int(2 * math.pi * r / pcbnew.FromMM(0.03)))
                    for i in range(steps):
                        a = (i / float(steps)) * 2 * math.pi
                        pts.append(pcbnew.VECTOR2I(int(round(cx + r*math.cos(a))), int(round(cy + r*math.sin(a)))))
                    pts.append(pts[0])
            else:
                # Catch-all for any shape type not explicitly parsed above — most
                # importantly SHAPE_T_POLY, which is what KiCad uses whenever a
                # cutout is drawn as a single closed polygon (e.g. via the polygon
                # tool, or imported from mechanical CAD) rather than as separate
                # segment/arc pieces.
                try:
                    eff_shape = dwg.GetEffectiveShape()
                    fallback_poly = pcbnew.SHAPE_POLY_SET()
                    eff_shape.TransformToPolygon(fallback_poly, poly_error_iu, 0)
                    for oi in range(fallback_poly.OutlineCount()):
                        outline = fallback_poly.Outline(oi)
                        n_pts = outline.PointCount()
                        ring = []
                        for k in range(n_pts):
                            p = outline.CPoint(k)
                            ring.append((p.x, p.y))
                        if len(ring) >= 2:
                            for i in range(len(ring)):
                                x1, y1 = ring[i]
                                x2, y2 = ring[(i + 1) % len(ring)]
                                exact_hole_edges.append((x1, y1, x2, y2))
                            if len(ring) >= 3:
                                obstacle_polygons.append(ring)
                                obstacle_kind.append('slot_or_edge')
                                obstacle_holes.append([])
                except Exception:
                    pass

            if len(pts) >= 2:
                for i in range(len(pts) - 1): exact_hole_edges.append((pts[i].x, pts[i].y, pts[i+1].x, pts[i+1].y))
                raw_paths.append(pts)
                arc_metadata.append(arc_meta)

        stitch_tol = pcbnew.FromMM(0.05)
        while raw_paths:
            current_ring = raw_paths.pop(0)
            changed = True
            while changed:
                changed = False
                i = 0
                while i < len(raw_paths):
                    p = raw_paths[i]
                    d1 = math.hypot(current_ring[-1].x - p[0].x, current_ring[-1].y - p[0].y)
                    d2 = math.hypot(current_ring[-1].x - p[-1].x, current_ring[-1].y - p[-1].y)
                    d3 = math.hypot(current_ring[0].x - p[-1].x, current_ring[0].y - p[-1].y)
                    d4 = math.hypot(current_ring[0].x - p[0].x, current_ring[0].y - p[0].y)
                    min_d = min(d1, d2, d3, d4)
                    if min_d < stitch_tol:
                        if min_d == d1: current_ring.extend(p[1:])
                        elif min_d == d2: current_ring.extend(reversed(p[:-1]))
                        elif min_d == d3: current_ring = p[:-1] + current_ring
                        elif min_d == d4: current_ring = list(reversed(p[1:])) + current_ring
                        raw_paths.pop(i)
                        changed = True
                    else: i += 1
            if math.hypot(current_ring[0].x - current_ring[-1].x, current_ring[0].y - current_ring[-1].y) < stitch_tol:
                _ring_xs = [pt.x for pt in current_ring]
                _ring_ys = [pt.y for pt in current_ring]
                _ring_is_outline = False
                if _ec_min_x is not None:
                    _tol = pcbnew.FromMM(0.5)
                    _ring_is_outline = (abs(min(_ring_xs) - _ec_min_x) < _tol and
                                         abs(min(_ring_ys) - _ec_min_y) < _tol and
                                         abs(max(_ring_xs) - _ec_max_x) < _tol and
                                         abs(max(_ring_ys) - _ec_max_y) < _tol)
                if _ring_is_outline:
                    _outline_shapes_skipped += 1
                    diag_lines.append(f"Skipped a stitched ring ({len(current_ring)} points) whose bounding box "
                                       f"matches the full board extent — this is the board outline assembled from "
                                       f"multiple segments/arcs (no single piece individually matched the full "
                                       f"extent, so the per-shape check alone couldn't catch it before stitching). "
                                       f"Not treated as an obstacle.")
                else:
                    obstacle_polygons.append([(pt.x, pt.y) for pt in current_ring])
                    obstacle_kind.append('slot_or_edge')
                    obstacle_holes.append([])

        if _outline_shapes_skipped:
            diag_lines.append(f"Skipped {_outline_shapes_skipped} Edge.Cuts shape(s) identified as the board outline "
                               f"(bbox matches the combined extent of all Edge.Cuts shapes) — not treated as obstacles.")

        # Bounding box per obstacle polygon, computed once, so the per-cell grid
        # check below can cheaply reject polygons that can't possibly contain the
        # point before paying for a full point-in-polygon walk over its edges.
        obstacle_bboxes = []
        for poly in obstacle_polygons:
            xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
            obstacle_bboxes.append((min(xs), min(ys), max(xs), max(ys)))

        # =====================================================================
        # SPATIAL INDEX — on a simple board (a couple hundred obstacles) the
        # per-cell linear scan below over obstacle_bboxes was cheap. On a real
        # complex board (thousands of pads/zones/holes -> thousands of
        # obstacles), that same scan run against every one of the millions of
        # cells pathfinding actually visits becomes billions of comparisons —
        # confirmed as the dominant cost via profiling (2.9M cells x thousands
        # of obstacles). This buckets every obstacle into a coarse grid ONCE;
        # a cell lookup then only needs to check the handful of obstacles
        # whose bounding box actually overlaps that bucket, not all of them.
        # =====================================================================
        SPATIAL_BUCKET_IU = pcbnew.FromMM(2.0)

        def _bucket_of(x, y):
            return (int(x // SPATIAL_BUCKET_IU), int(y // SPATIAL_BUCKET_IU))

        def _build_bucket_index(bboxes):
            index = {}
            for idx, (bx1, by1, bx2, by2) in enumerate(bboxes):
                bxlo, bylo = _bucket_of(bx1, by1)
                bxhi, byhi = _bucket_of(bx2, by2)
                for bxi in range(bxlo, bxhi + 1):
                    for byi in range(bylo, byhi + 1):
                        index.setdefault((bxi, byi), []).append(idx)
            return index

        obstacle_spatial_index = _build_bucket_index(obstacle_bboxes)

        def _obstacle_candidates_near(x, y):
            return obstacle_spatial_index.get(_bucket_of(x, y), ())

        def _obstacle_candidates_in_region(x, y, margin):
            # 3x3 neighborhood is always sufficient as long as margin < bucket
            # size (2mm here) — every margin used anywhere in this file is a
            # small fraction of a mm, so this has plenty of headroom.
            bx0, by0 = _bucket_of(x, y)
            seen = set()
            out = []
            for dbx in (-1, 0, 1):
                for dby in (-1, 0, 1):
                    for idx in obstacle_spatial_index.get((bx0 + dbx, by0 + dby), ()):
                        if idx not in seen:
                            seen.add(idx)
                            out.append(idx)
            return out

        # exact_hole_edges gets scanned on every LOS check (segments_proper_intersect
        # against every edge) — same problem as obstacle_polygons above, and this list
        # is often even larger (every obstacle contributes multiple edges). LOS checks
        # fire heavily inside the via-conductor search's inner loop, so this indexing
        # matters just as much there as get_grid_mask's did for pathfinding.
        hole_edge_bboxes = []
        for (ex1, ey1, ex2, ey2) in exact_hole_edges:
            hole_edge_bboxes.append((min(ex1, ex2), min(ey1, ey2), max(ex1, ex2), max(ey1, ey2)))
        hole_edge_spatial_index = _build_bucket_index(hole_edge_bboxes)

        def _hole_edge_candidates_for_segment(x1, y1, x2, y2):
            # Candidates from every bucket the query segment's own bounding box
            # spans — correct for segments of any length, from a short local
            # tangent-correction hop to a long global-direct-search line.
            bxlo, bylo = _bucket_of(min(x1, x2), min(y1, y2))
            bxhi, byhi = _bucket_of(max(x1, x2), max(y1, y2))
            seen = set()
            out = []
            for bxi in range(bxlo, bxhi + 1):
                for byi in range(bylo, byhi + 1):
                    for idx in hole_edge_spatial_index.get((bxi, byi), ()):
                        if idx not in seen:
                            seen.add(idx)
                            out.append(idx)
            return out

        # Build lookup of all analytic arc definitions (in IU) — used during
        # visualization to draw the hugged portion as a true arc instead of
        # connecting coarse polygon-ring points with straight segments.
        # The polygon ring drives A* search topology (correct, cheap), but
        # the visual output is derived from the arc's own parametric equation
        # for a smooth curve instead of a faceted polyline.
        arc_primitives = []  # list of (cx,cy,r,sa,ea) in IU
        for meta in arc_metadata:
            if meta is not None:
                arc_primitives.append(meta)

        # Per-layer edge lookups for fast access inside the global optimizer
        # (avoids filtering the full edge list on every inner-loop call)
        hvp_edges_by_layer = {}
        for (x1,y1,x2,y2,l) in exact_hvp_edges_layered:
            hvp_edges_by_layer.setdefault(l, []).append((x1,y1,x2,y2))
        hvm_edges_by_layer = {}
        for (x1,y1,x2,y2,l) in exact_hvm_edges_layered:
            hvm_edges_by_layer.setdefault(l, []).append((x1,y1,x2,y2))

        def find_arc_for_point(px, py, tol_iu):
            # returns (cx,cy,r,sa,ea) if the point lies on a known analytic arc
            for cx, cy, r, sa, ea in arc_primitives:
                dist = math.hypot(px-cx, py-cy)
                if abs(dist - r) < tol_iu:
                    ang = math.atan2(py-cy, px-cx)
                    # normalise to [sa, sa+2pi)
                    while ang < sa - 1e-9: ang += 2*math.pi
                    if ang <= ea + 1e-9:
                        return (cx, cy, r, sa, ea)
            return None

        diag_lines.append(f"obstacle_polygons detail ({len(obstacle_polygons)} total):")
        for _idx, (poly, bb) in enumerate(zip(obstacle_polygons, obstacle_bboxes)):
            _area_mm2 = pcbnew.ToMM(bb[2]-bb[0]) * pcbnew.ToMM(bb[3]-bb[1])
            _kind = obstacle_kind[_idx] if _idx < len(obstacle_kind) else "?"
            diag_lines.append(f"  [{_idx}] kind={_kind} pts={len(poly)} bbox=({pcbnew.ToMM(bb[0]):.3f},{pcbnew.ToMM(bb[1]):.3f}) to "
                               f"({pcbnew.ToMM(bb[2]):.3f},{pcbnew.ToMM(bb[3]):.3f}) area={_area_mm2:.3f}mm^2")

        def point_in_polygon(x, y, poly):
            n = len(poly)
            inside = False
            p1x, p1y = poly[0]
            for i in range(n + 1):
                p2x, p2y = poly[i % n]
                if y > min(p1y, p2y):
                    if y <= max(p1y, p2y) and x <= max(p1x, p2x):
                        if p1y != p2y:
                            xints = (y - p1y) * (p2x - p1x) / float(p2y - p1y) + p1x
                        if p1x == p2x or x <= xints: inside = not inside
                p1x, p1y = p2x, p2y
            return inside

        def point_in_obstacle(x, y, idx):
            # A point counts as inside obstacle_polygons[idx] only if it's inside
            # that outline AND not inside any of ITS holes. Without this, a zone's
            # clearance cutouts (around every other-net pad/trace it clears —
            # including net A/B's own copper) would be silently ignored, and the
            # zone's entire outer boundary would be treated as solid copper even
            # where it demonstrably isn't. This is what a real ground pour with
            # real clearance holes needs; the earlier simple test boards never
            # had a zone with holes, so this gap never surfaced until now.
            if not point_in_polygon(x, y, obstacle_polygons[idx]):
                return False
            for hole in obstacle_holes[idx]:
                if point_in_polygon(x, y, hole):
                    return False
            return True

        # Pad/edge bounding boxes computed once up front, reused across every grid
        # cell query instead of recomputing them on every call.
        npth_pad_bboxes = [(p, p.GetBoundingBox()) for p in npth_pads]
        edge_cuts_bboxes = [(d, d.GetBoundingBox()) for d in edge_cuts if hasattr(d, "HitTest")]

        t3 = time.time()
        diag_lines.append(f"Timing - Obstacle extraction + stitching: {t3-t2:.2f}s")
        _progress("Pathfinding (often the slowest stage)...", force=True)

        # =====================================================================
        # LAZY / MEMOIZED GRID: a cell's blocked/free status (and is_portal) is
        # computed once on first access and cached, rather than eagerly
        # rasterizing the full padded bounding box up front. A* only ever pays
        # for cells it actually visits, which for a typical localized creepage
        # search is a small fraction of the padded box — and that gap widens
        # further on larger/more complex boards.
        # =====================================================================
        grid_mask_cache = {}

        def get_grid_mask(r, c):
            if r < 0 or r >= rows or c < 0 or c >= cols:
                return 0
            key = (r, c)
            cached = grid_mask_cache.get(key)
            if cached is not None:
                return cached
            real_x = int(round((grid_min_c + c) * res_iu))
            real_y = int(round((grid_min_r + r) * res_iu))
            pt = pcbnew.VECTOR2I(real_x, real_y)
            inside = False

            for pad, bbox in npth_pad_bboxes:
                if bbox.Contains(pt) and pad.HitTest(pt): inside = True; break

            if not inside:
                for idx in _obstacle_candidates_near(real_x, real_y):
                    bx1, by1, bx2, by2 = obstacle_bboxes[idx]
                    if bx1 <= real_x <= bx2 and by1 <= real_y <= by2:
                        if point_in_obstacle(real_x, real_y, idx): inside = True; break

            if not inside:
                for dwg, bbox in edge_cuts_bboxes:
                    if bbox.Contains(pt) and dwg.HitTest(pt, int(res_iu)): inside = True; break

            val = -1 if inside else 0
            grid_mask_cache[key] = val
            return val

        def get_is_portal(r, c):
            if get_grid_mask(r, c) == -1: return False
            return (get_grid_mask(r-1, c) == -1 or get_grid_mask(r+1, c) == -1 or
                    get_grid_mask(r, c-1) == -1 or get_grid_mask(r, c+1) == -1)

        def near_hole(r, c):
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if get_grid_mask(r+dr, c+dc) == -1: return True
            return False

        # Dijkstra pathfinding with sparse dict-based storage, so memory/time
        # scale with cells actually explored rather than with the padded
        # bounding box times layer count.
        start_cache, target_cache = {}, {}
        for layer, nodes in hvp_nodes.items():
            start_cache[layer] = [(r - grid_min_r, c - grid_min_c) for r, c in nodes
                                   if 0 <= r - grid_min_r < rows and 0 <= c - grid_min_c < cols
                                   and get_grid_mask(r - grid_min_r, c - grid_min_c) != -1]
        for layer, nodes in hvm_nodes.items():
            target_cache[layer] = set((r - grid_min_r, c - grid_min_c) for r, c in nodes
                                       if 0 <= r - grid_min_r < rows and 0 <= c - grid_min_c < cols)

        neighbor_offsets = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0), (-1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)), (1, 1, math.sqrt(2))]
        global_min_dist = float('inf')
        term_node, term_layer = None, None
        dist_matrix = {}
        parent_matrix = {}
        pq = []

        for layer, starts in start_cache.items():
            for r, c in starts:
                dist_matrix[(layer, r, c)] = 0.0
                heapq.heappush(pq, (0.0, layer, r, c))

        res_mm = pcbnew.ToMM(res_iu)
        _pf_iter = 0
        while pq:
            _pf_iter += 1
            if _pf_iter % 200 == 0:
                _progress(f"Pathfinding: {len(grid_mask_cache)} cells evaluated")
            d, layer, r, c = heapq.heappop(pq)
            if d > dist_matrix.get((layer, r, c), float('inf')): continue
            if d >= global_min_dist: break

            if (r, c) in target_cache.get(layer, set()):
                global_min_dist = d
                term_node, term_layer = (r, c), layer
                break

            for dr, dc, weight in neighbor_offsets:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and get_grid_mask(nr, nc) != -1:
                    nd = d + (weight * res_mm)
                    key = (layer, nr, nc)
                    if nd < dist_matrix.get(key, float('inf')):
                        dist_matrix[key] = nd
                        parent_matrix[key] = (layer, r, c)
                        heapq.heappush(pq, (nd, layer, nr, nc))

            if get_is_portal(r, c):
                for next_layer in enabled_copper_layers:
                    if next_layer != layer:
                        dz = layer_gap_mm(layer, next_layer)
                        nd_z = d + dz
                        key = (next_layer, r, c)
                        if nd_z < dist_matrix.get(key, float('inf')):
                            dist_matrix[key] = nd_z
                            parent_matrix[key] = (layer, r, c)
                            heapq.heappush(pq, (nd_z, next_layer, r, c))

        t4 = time.time()
        diag_lines.append(f"Timing - Pathfinding: {t4-t3:.2f}s (cells evaluated: {len(grid_mask_cache)})")
        _progress("Smoothing path, correcting boundaries...", force=True)

        if term_node:
            raw_path = []
            curr = (term_layer, term_node[0], term_node[1])
            while curr:
                raw_path.append(curr)
                curr = parent_matrix.get(curr)
            raw_path.reverse()

            def closest_on_segment(px, py, x1, y1, x2, y2):
                dx, dy = x2 - x1, y2 - y1
                lensq = dx*dx + dy*dy
                if lensq == 0: return x1, y1, math.hypot(px-x1, py-y1)
                t = max(0.0, min(1.0, ((px-x1)*dx + (py-y1)*dy) / lensq))
                cx, cy = x1 + t*dx, y1 + t*dy
                return cx, cy, math.hypot(px-cx, py-cy)

            def closest_on_edges(px, py, edges):
                b_pt, b_dist = None, float('inf')
                for (x1, y1, x2, y2) in edges:
                    cx, cy, dist = closest_on_segment(px, py, x1, y1, x2, y2)
                    if dist < b_dist: b_dist, b_pt = dist, (cx, cy)
                return b_pt, b_dist

            # ---- TRUE TANGENT-WRAP GEOMETRY ----
            def polygon_tangent_indices(px, py, poly_pts):
                n = len(poly_pts)
                if n < 3: return None
                signs = []
                for i in range(n):
                    ax, ay = poly_pts[i]
                    bx, by = poly_pts[(i + 1) % n]
                    signs.append((ax - px) * (by - py) - (ay - py) * (bx - px))
                tangents = [i for i in range(n) if (signs[i-1] >= 0) != (signs[i] >= 0)]
                return tangents if len(tangents) == 2 else None

            def walk_polygon_arc(poly_pts, i1, i2, forward):
                n = len(poly_pts)
                if forward:
                    idxs = list(range(i1, i2+1)) if i1 <= i2 else list(range(i1, n)) + list(range(0, i2+1))
                else:
                    idxs = list(range(i1, i2-1, -1)) if i1 >= i2 else list(range(i1, -1, -1)) + list(range(n-1, i2-1, -1))
                return [poly_pts[i] for i in idxs]

            def polyline_length(points):
                return sum(math.hypot(points[i+1][0]-points[i][0], points[i+1][1]-points[i][1]) for i in range(len(points)-1))

            def tangent_wrap(prev_pt, next_pt, poly_pts):
                tA = polygon_tangent_indices(prev_pt[0], prev_pt[1], poly_pts)
                tB = polygon_tangent_indices(next_pt[0], next_pt[1], poly_pts)
                if not tA or not tB: return None
                best_path, best_len = None, float('inf')
                for ia in tA:
                    for ib in tB:
                        for forward in (True, False):
                            arc_pts = walk_polygon_arc(poly_pts, ia, ib, forward)
                            candidate = [prev_pt] + arc_pts + [next_pt]
                            length = polyline_length(candidate)
                            if length < best_len:
                                best_len, best_path = length, candidate
                return best_path

            def find_obstacle_index(px, py, tol):
                best_idx, best_d = None, float('inf')
                for idx in _obstacle_candidates_in_region(px, py, tol):
                    poly = obstacle_polygons[idx]
                    bx1, by1, bx2, by2 = obstacle_bboxes[idx]
                    margin = tol
                    if not (bx1-margin <= px <= bx2+margin and by1-margin <= py <= by2+margin):
                        continue
                    n = len(poly)
                    for i in range(n):
                        x1, y1 = poly[i]
                        x2, y2 = poly[(i+1) % n]
                        _, _, d = closest_on_segment(px, py, x1, y1, x2, y2)
                        if d < best_d: best_d, best_idx = d, idx
                return best_idx if best_d <= tol else None

            # ---- BRUTE-FORCE CROSS-CHECK ----
            # tangent_wrap computes the analytical tangent points directly, which
            # should be optimal, but this provides an independent, exhaustive
            # verification (try every point on the obstacle directly, keep the
            # true minimum) rather than trusting the analytical shortcut blindly.
            # Sorted-distance pruning keeps it fast; a real visibility check (not
            # just raw distance) keeps it honest — a straight line to a "hidden"
            # point on the far side of the obstacle is invalid even if its raw
            # distance looks shorter.
            def _tuple_los(p1, p2):
                x1, y1 = p1; x2, y2 = p2
                dist = math.hypot(x2 - x1, y2 - y1)
                if dist < 1: return True
                ux, uy = (x2-x1)/dist, (y2-y1)/dist
                trim = min(trim_iu, dist/3.0)
                tx1, ty1 = x1+ux*trim, y1+uy*trim
                tx2, ty2 = x2-ux*trim, y2-uy*trim
                seg_min_x, seg_max_x = (tx1, tx2) if tx1 <= tx2 else (tx2, tx1)
                seg_min_y, seg_max_y = (ty1, ty2) if ty1 <= ty2 else (ty2, ty1)
                for _hidx in _hole_edge_candidates_for_segment(tx1, ty1, tx2, ty2):
                    ex1, ey1, ex2, ey2 = exact_hole_edges[_hidx]
                    if (ex1 < seg_min_x and ex2 < seg_min_x) or (ex1 > seg_max_x and ex2 > seg_max_x): continue
                    if (ey1 < seg_min_y and ey2 < seg_min_y) or (ey1 > seg_max_y and ey2 > seg_max_y): continue
                    if segments_proper_intersect(tx1, ty1, tx2, ty2, ex1, ey1, ex2, ey2):
                        return False
                mx, my = (tx1+tx2)/2.0, (ty1+ty2)/2.0
                for idx2 in _obstacle_candidates_near(mx, my):
                    bx1, by1, bx2, by2 = obstacle_bboxes[idx2]
                    if not (bx1 <= mx <= bx2 and by1 <= my <= by2): continue
                    if point_in_obstacle(mx, my, idx2):
                        return False
                return True

            def brute_force_verify_wrap(prev_pt, next_pt, poly_pts):
                n = len(poly_pts)
                if n < 3: return None, None
                seg_lens = [math.hypot(poly_pts[(i+1)%n][0]-poly_pts[i][0], poly_pts[(i+1)%n][1]-poly_pts[i][1]) for i in range(n)]
                cum = [0.0]
                for L in seg_lens: cum.append(cum[-1] + L)
                total_perim = cum[-1]
                def arc_dist(i, j):
                    d1 = abs(cum[j] - cum[i])
                    return min(d1, total_perim - d1)
                entry_cands = sorted((math.hypot(poly_pts[i][0]-prev_pt[0], poly_pts[i][1]-prev_pt[1]), i) for i in range(n))
                exit_cands = sorted((math.hypot(poly_pts[j][0]-next_pt[0], poly_pts[j][1]-next_pt[1]), j) for j in range(n))
                best_len, best_pair = float('inf'), None
                for entry_d, i in entry_cands:
                    if entry_d >= best_len: break
                    for exit_d, j in exit_cands:
                        if entry_d + exit_d >= best_len: break
                        if i == j: continue
                        total = entry_d + arc_dist(i, j) + exit_d
                        if total < best_len:
                            if _tuple_los(prev_pt, poly_pts[i]) and _tuple_los(poly_pts[j], next_pt):
                                best_len, best_pair = total, (i, j)
                return best_len, best_pair

            class PathPoint:
                def __init__(self, layer, x, y, is_obstacle=False, is_conductor_transit=False):
                    self.layer, self.x, self.y = layer, int(round(x)), int(round(y))
                    self.is_obstacle = is_obstacle
                    self.is_conductor_transit = is_conductor_transit  # inside a third-net conductor: 0mm creepage

            snapped_path = []
            for i, (layer, r, c) in enumerate(raw_path):
                px, py = (grid_min_c + c) * res_iu, (grid_min_r + r) * res_iu
                if i == 0 and exact_hvp_edges:
                    b_pt, _ = closest_on_edges(px, py, exact_hvp_edges)
                    snapped_path.append(PathPoint(layer, b_pt[0], b_pt[1], False))
                elif i == len(raw_path)-1 and exact_hvm_edges:
                    b_pt, _ = closest_on_edges(px, py, exact_hvm_edges)
                    snapped_path.append(PathPoint(layer, b_pt[0], b_pt[1], False))
                else:
                    if (get_is_portal(r, c) or near_hole(r, c)) and exact_hole_edges:
                        b_pt, bd = closest_on_edges(px, py, exact_hole_edges)
                        if bd < pcbnew.FromMM(0.1):
                            snapped_path.append(PathPoint(layer, b_pt[0], b_pt[1], True))
                            continue
                    snapped_path.append(PathPoint(layer, px, py, False))

            _n_obstacle_pts = sum(1 for p in snapped_path if p.is_obstacle)
            diag_lines.append(f"===== PATHFINDING DIAGNOSTIC =====")
            diag_lines.append(f"raw_path points: {len(raw_path)} | obstacle_polygons: {len(obstacle_polygons)} | "
                               f"exact_hole_edges: {len(exact_hole_edges)} | exact_hvp_edges: {len(exact_hvp_edges)} | "
                               f"exact_hvm_edges: {len(exact_hvm_edges)}")
            diag_lines.append(f"snapped_path points tagged is_obstacle: {_n_obstacle_pts} / {len(snapped_path)}")
            if _n_obstacle_pts == 0 and obstacle_polygons:
                diag_lines.append("  NOTE: obstacle geometry exists on the board but the path never snapped to "
                                   "any of it (is_portal/near_hole never triggered near this path, or every "
                                   "candidate point was >0.1mm from the nearest obstacle edge) — points to the "
                                   "GRID/SNAPPING stage, not the tangent-wrap stage.")

            # STRING PULLING EXACT — true geometry test (segment intersection +
            # point-in-polygon), with a cheap bounding-box reject per edge before
            # paying for the full intersection math.
            trim_iu = max(1, int(pcbnew.FromMM(0.001)))

            def segments_proper_intersect(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
                d1 = (bx2-bx1)*(ay1-by1) - (by2-by1)*(ax1-bx1)
                d2 = (bx2-bx1)*(ay2-by1) - (by2-by1)*(ax2-bx1)
                d3 = (ax2-ax1)*(by1-ay1) - (ay2-ay1)*(bx1-ax1)
                d4 = (ax2-ax1)*(by2-ay1) - (ay2-ay1)*(bx2-ax1)
                if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
                    return True
                return False

            def exact_los(p1, p2):
                x1, y1, x2, y2 = p1.x, p1.y, p2.x, p2.y
                dist = math.hypot(x2 - x1, y2 - y1)
                if dist < 1: return True
                ux, uy = (x2-x1)/dist, (y2-y1)/dist
                trim = min(trim_iu, dist/3.0)
                tx1, ty1 = x1+ux*trim, y1+uy*trim
                tx2, ty2 = x2-ux*trim, y2-uy*trim
                seg_min_x, seg_max_x = (tx1, tx2) if tx1 <= tx2 else (tx2, tx1)
                seg_min_y, seg_max_y = (ty1, ty2) if ty1 <= ty2 else (ty2, ty1)
                for _hidx in _hole_edge_candidates_for_segment(tx1, ty1, tx2, ty2):
                    ex1, ey1, ex2, ey2 = exact_hole_edges[_hidx]
                    if (ex1 < seg_min_x and ex2 < seg_min_x) or (ex1 > seg_max_x and ex2 > seg_max_x): continue
                    if (ey1 < seg_min_y and ey2 < seg_min_y) or (ey1 > seg_max_y and ey2 > seg_max_y): continue
                    if segments_proper_intersect(tx1, ty1, tx2, ty2, ex1, ey1, ex2, ey2):
                        return False
                mx, my = (tx1+tx2)/2.0, (ty1+ty2)/2.0
                for idx in _obstacle_candidates_near(mx, my):
                    bx1, by1, bx2, by2 = obstacle_bboxes[idx]
                    if not (bx1 <= mx <= bx2 and by1 <= my <= by2): continue
                    if point_in_obstacle(mx, my, idx):
                        return False
                return True

            def smooth_segment(seg):
                if len(seg) <= 1: return seg
                smoothed = [seg[0]]
                curr = 0
                while curr < len(seg) - 1:
                    nb = curr + 1
                    for look in range(len(seg) - 1, curr, -1):
                        if exact_los(seg[curr], seg[look]):
                            nb = look
                            break
                    smoothed.append(seg[nb])
                    curr = nb
                return smoothed

            def apply_tangent_correction(seg):
                # Operates on the FULL smoothed path (already concatenated across
                # any layer/via transitions, see below) so an obstacle-hugging run
                # that happens to straddle a via still gets corrected as one
                # continuous curve. Whatever layer-change the global pathfinder
                # decided on is preserved — just relocated onto the exact
                # tangent-corrected curve instead of left at whatever raw grid
                # point the run got split at, which previously degraded to a
                # straight line near the via.
                if len(seg) < 3 or not obstacle_polygons:
                    return seg
                result = [seg[0]]
                i = 1
                n = len(seg)
                while i < n - 1:
                    if seg[i].is_obstacle:
                        run_start = i
                        run_end = i
                        while run_end + 1 < n - 1 and seg[run_end + 1].is_obstacle:
                            run_end += 1
                        run_pts = seg[run_start:run_end+1]
                        anchor_before = result[-1]
                        anchor_after = seg[run_end + 1]
                        prev_pt = (anchor_before.x, anchor_before.y)
                        next_pt = (anchor_after.x, anchor_after.y)
                        mid = run_pts[len(run_pts)//2]
                        obs_idx = find_obstacle_index(mid.x, mid.y, pcbnew.FromMM(0.1))
                        wrapped = tangent_wrap(prev_pt, next_pt, obstacle_polygons[obs_idx]) if obs_idx is not None else None
                        _path_is_geometric = False  # set True only when path comes from real tangent geometry, not raw grid points

                        # When tangent_wrap fails (both anchors in overlapping visibility zone),
                        # run brute_force directly as a fallback instead of keeping raw grid points.
                        if (not wrapped or len(wrapped) <= 2) and obs_idx is not None:
                            _bf_fb_iu, _bf_fb_pair = brute_force_verify_wrap(prev_pt, next_pt, obstacle_polygons[obs_idx])
                            if _bf_fb_iu is not None and _bf_fb_iu != float('inf') and _bf_fb_pair is not None:
                                _poly = obstacle_polygons[obs_idx]
                                _bi, _bj = _bf_fb_pair
                                _arc_f = walk_polygon_arc(_poly, _bi, _bj, True)
                                _arc_b = walk_polygon_arc(_poly, _bi, _bj, False)
                                _cand_f = [prev_pt] + _arc_f + [next_pt]
                                _cand_b = [prev_pt] + _arc_b + [next_pt]
                                wrapped = _cand_f if polyline_length(_cand_f) <= polyline_length(_cand_b) else _cand_b
                                _path_is_geometric = True
                                diag_lines.append(f"tangent-hug run @ ({pcbnew.ToMM(mid.x):.3f},{pcbnew.ToMM(mid.y):.3f})mm: "
                                                   f"tangent_wrap failed, recovered via brute-force fallback "
                                                   f"({pcbnew.ToMM(int(round(_bf_fb_iu))):.3f}mm, {len(wrapped)-2} interior pts)")

                        if wrapped and len(wrapped) > 2:
                            if not _path_is_geometric:
                                _path_is_geometric = True  # tangent_wrap succeeded
                            _tw_len_mm = sum(pcbnew.ToMM(int(round(math.hypot(wrapped[k+1][0]-wrapped[k][0], wrapped[k+1][1]-wrapped[k][1])))) for k in range(len(wrapped)-1))
                            _bf_len_iu, _bf_pair = brute_force_verify_wrap(prev_pt, next_pt, obstacle_polygons[obs_idx])
                            _used_bf = False
                            if _bf_len_iu is not None and _bf_len_iu != float('inf'):
                                _bf_len_mm = pcbnew.ToMM(int(round(_bf_len_iu)))
                                _delta_um = (_bf_len_mm - _tw_len_mm) * 1000.0
                                if _delta_um < -1.0 and _bf_pair is not None:
                                    # brute-force genuinely found a shorter valid path: tangent_wrap's
                                    # "each anchor's own independent tangent points" model is incomplete
                                    # when both anchors' visibility regions overlap near the obstacle —
                                    # rebuild the actual path from brute-force's proven-correct indices.
                                    _poly = obstacle_polygons[obs_idx]
                                    _bi, _bj = _bf_pair
                                    _arc_f = walk_polygon_arc(_poly, _bi, _bj, True)
                                    _arc_b = walk_polygon_arc(_poly, _bi, _bj, False)
                                    _cand_f = [prev_pt] + _arc_f + [next_pt]
                                    _cand_b = [prev_pt] + _arc_b + [next_pt]
                                    wrapped = _cand_f if polyline_length(_cand_f) <= polyline_length(_cand_b) else _cand_b
                                    _used_bf = True

                            interior = wrapped[1:-1]
                            _wrap_arc_len_mm = sum(pcbnew.ToMM(int(round(math.hypot(wrapped[k+1][0]-wrapped[k][0], wrapped[k+1][1]-wrapped[k][1])))) for k in range(len(wrapped)-1))
                            _chord_span_mm = pcbnew.ToMM(int(round(math.hypot(interior[-1][0]-interior[0][0], interior[-1][1]-interior[0][1])))) if len(interior) > 1 else 0.0
                            _seg_lens_mm = [round(pcbnew.ToMM(int(round(math.hypot(wrapped[k+1][0]-wrapped[k][0], wrapped[k+1][1]-wrapped[k][1])))), 4) for k in range(len(wrapped)-1)]
                            diag_lines.append(f"tangent-hug run @ ({pcbnew.ToMM(mid.x):.3f},{pcbnew.ToMM(mid.y):.3f})mm: "
                                               f"SUCCESS via obstacle#{obs_idx}, {len(run_pts)} raw pts -> {len(interior)} pts "
                                               f"(wrap arc length {_wrap_arc_len_mm:.3f}mm, interior chord span {_chord_span_mm:.3f}mm)"
                                               f"{' [corrected via brute-force]' if _used_bf else ''}")
                            diag_lines.append(f"  segment lengths (mm): {_seg_lens_mm}")
                            if _bf_len_iu is not None and _bf_len_iu != float('inf'):
                                _verdict = "MATCH" if not _used_bf and abs(_delta_um) < 1.0 else \
                                           f"CORRECTED: tangent_wrap's {_tw_len_mm:.4f}mm replaced with brute-force's {_bf_len_mm:.4f}mm" if _used_bf else \
                                           f"brute-force longer by {_delta_um:.2f}um (tessellation noise, tangent_wrap kept)"
                                diag_lines.append(f"  brute-force cross-check: {_verdict}")

                            if interior:
                                chain_layers = [anchor_before.layer] + [p.layer for p in run_pts] + [anchor_after.layer]
                                chain_len = len(chain_layers) - 1
                                transitions = []
                                for k in range(1, len(chain_layers)):
                                    if chain_layers[k] != chain_layers[k-1]:
                                        transitions.append((k / float(chain_len), chain_layers[k]))
                                current_layer = anchor_before.layer
                                trans_idx = 0
                                n_interior = len(interior)
                                for k, (ix, iy) in enumerate(interior):
                                    frac = (k + 1) / float(n_interior + 1)
                                    while trans_idx < len(transitions) and frac >= transitions[trans_idx][0]:
                                        current_layer = transitions[trans_idx][1]
                                        trans_idx += 1
                                    result.append(PathPoint(current_layer, ix, iy, _path_is_geometric))
                        else:
                            diag_lines.append(f"tangent-hug run @ ({pcbnew.ToMM(mid.x):.3f},{pcbnew.ToMM(mid.y):.3f})mm: "
                                               f"ALL METHODS FAILED (obs_idx={obs_idx}, polygons={len(obstacle_polygons)}), "
                                               f"kept {len(run_pts)} raw pts as-is (is_obstacle cleared to prevent arc-draw misfire)")
                            for p in run_pts:
                                # Clear is_obstacle flag: these are raw grid-resolution points, not
                                # true arc-contact points. Leaving is_obstacle=True here would cause
                                # draw_segments_arc_aware to fire and produce malformed arc segments
                                # between incoherent grid-quantized positions, creating visible kinks.
                                result.append(PathPoint(p.layer, p.x, p.y, False))
                        i = run_end + 1
                    else:
                        result.append(seg[i])
                        i += 1
                result.append(seg[-1])
                return result

            # Smooth per-layer-chunk first (safe: a chunk boundary can never
            # silently skip past a real layer transition, since each chunk by
            # construction contains only one layer's points), THEN run the
            # tangent correction on the full concatenated result so it can see
            # across what used to be chunk boundaries.
            def split_by_obstacle_status(seg):
                # Splits a same-layer chunk into alternating free-space and
                # obstacle-hugging sub-runs, preserving order. This matters
                # because smooth_segment's greedy skip-ahead, given a whole
                # chunk at once, could test visibility directly between a
                # point before an obstacle run and a point after it — and if
                # that single long chord happens to read as geometrically
                # clear, it would eliminate the entire run of is_obstacle
                # points before apply_tangent_correction ever sees them.
                # Smoothing must never be allowed to see both sides of an
                # obstacle run in the same call.
                if not seg: return []
                runs = []
                curr_status = seg[0].is_obstacle
                curr_run = [seg[0]]
                for p in seg[1:]:
                    if p.is_obstacle == curr_status:
                        curr_run.append(p)
                    else:
                        runs.append((curr_status, curr_run))
                        curr_status = p.is_obstacle
                        curr_run = [p]
                runs.append((curr_status, curr_run))
                return runs

            def smooth_chunk(chunk):
                out = []
                for is_obs, run_pts in split_by_obstacle_status(chunk):
                    if is_obs:
                        # Never smoothed here — apply_tangent_correction replaces
                        # the whole run with its own computed tangent-wrap points
                        # regardless of how many raw points it originally had, so
                        # smoothing it first would only discard information
                        # apply_tangent_correction needs.
                        out.extend(run_pts)
                    else:
                        out.extend(smooth_segment(run_pts))
                return out

            smoothed_chunks = []
            curr_segment = []
            for p in snapped_path:
                if not curr_segment: curr_segment.append(p)
                elif p.layer == curr_segment[-1].layer: curr_segment.append(p)
                else:
                    smoothed_chunks.extend(smooth_chunk(curr_segment))
                    curr_segment = [p]
            if curr_segment:
                smoothed_chunks.extend(smooth_chunk(curr_segment))

            smoothed_path = apply_tangent_correction(smoothed_chunks)

            # GLOBAL ARC OPTIMIZER
            # The A* grid path gives anchor points that are 25µm-quantized intermediate
            # waypoints, not actual copper boundary points. apply_tangent_correction then
            # correctly minimises the local sub-path for those specific anchors — but if
            # the anchors themselves are suboptimal (e.g. landing too close to the rightmost
            # cap tip), the resulting path will be globally longer even though locally correct.
            # This step re-runs the search from the ACTUAL copper boundaries for each
            # obstacle encountered, finding the true mathematical minimum regardless of where
            # the A* grid path happened to pass — an exhaustive search combined with exact
            # arc geometry and layer awareness.
            def global_arc_minimum_for_obstacle(obs_idx, current_layer):
                poly = obstacle_polygons[obs_idx]
                n = len(poly)
                bb = obstacle_bboxes[obs_idx]

                seg_lens = [math.hypot(poly[(k+1)%n][0]-poly[k][0], poly[(k+1)%n][1]-poly[k][1]) for k in range(n)]
                cum = [0.0]
                for L in seg_lens: cum.append(cum[-1]+L)
                perim = cum[-1]
                def arc_dist(i, j):
                    d1 = abs(cum[j]-cum[i]); return min(d1, perim-d1)

                def closest_pt_on_edges_layered(px, py, edges_with_layer):
                    # Returns (closest_point, distance, layer_id)
                    best_d, best_pt, best_layer = float('inf'), None, None
                    for x1,y1,x2,y2,layer in edges_with_layer:
                        dx,dy = x2-x1, y2-y1; l2 = dx*dx+dy*dy
                        if l2 == 0: t = 0.0
                        else: t = max(0.0, min(1.0, ((px-x1)*dx+(py-y1)*dy)/l2))
                        cx,cy = x1+t*dx, y1+t*dy; d = math.hypot(px-cx,py-cy)
                        if d < best_d: best_d, best_pt, best_layer = d, (cx, cy), layer
                    return best_pt, best_d, best_layer

                best_total_iu = float('inf')
                best_hvp = best_hvm = best_ei = best_ej = None
                best_hvp_layer = best_hvm_layer = current_layer

                hvp_dists = []
                hvm_dists = []
                for k in range(n):
                    px, py = poly[k]
                    hvp_pt, d_hvp, hvp_l = closest_pt_on_edges_layered(px, py, exact_hvp_edges_layered)
                    hvm_pt, d_hvm, hvm_l = closest_pt_on_edges_layered(px, py, exact_hvm_edges_layered)
                    hvp_dists.append((d_hvp, hvp_pt, hvp_l))
                    hvm_dists.append((d_hvm, hvm_pt, hvm_l))

                entry_cands = sorted(((hvp_dists[k][0], k) for k in range(n)), key=lambda x: x[0])
                exit_cands  = sorted(((hvm_dists[k][0], k) for k in range(n)), key=lambda x: x[0])

                for entry_d, ei in entry_cands:
                    if entry_d >= best_total_iu: break
                    hvp_pt = hvp_dists[ei][1]; hvp_l = hvp_dists[ei][2]
                    if hvp_pt is None or not _tuple_los(hvp_pt, poly[ei]): continue
                    for exit_d, ej in exit_cands:
                        # Same-layer: must traverse arc around obstacle, so ei==ej invalid.
                        # Cross-layer: path drops straight through the slot wall at a single
                        # XY point — zero 2D arc traversal — so ei==ej is geometrically correct.
                        if hvp_l == hvm_dists[ej][2] and ei == ej: continue
                        if entry_d + exit_d >= best_total_iu: break
                        ad = arc_dist(ei, ej)  # = 0 when ei == ej (as intended for cross-layer)
                        hvm_pt = hvm_dists[ej][1]; hvm_l = hvm_dists[ej][2]
                        if hvm_pt is None: continue
                        z_iu = pcbnew.FromMM(layer_gap_mm(hvp_l, hvm_l)) if hvp_l != hvm_l else 0
                        total = entry_d + ad + z_iu + exit_d
                        if total < best_total_iu:
                            if _tuple_los(poly[ej], hvm_pt):
                                best_total_iu = total
                                best_hvp = hvp_pt; best_hvm = hvm_pt
                                best_ei = ei; best_ej = ej
                                best_hvp_layer = hvp_l; best_hvm_layer = hvm_l

                if best_ei is None: return None, None

                # Golden-section arc refinement for the cross-layer ei==ej case.
                # A discrete vertex search (stepped at the tessellation's arc-length
                # resolution) can only land on one of the polygon's existing sample
                # points, which generally overstates the true minimum — the actual
                # continuous-arc minimum lies somewhere between two vertices. For a
                # circle, finding it is a 1D optimisation over arc angle θ, which
                # golden-section search solves in ~50 iterations to nanoradian precision.
                if best_ei == best_ej and best_hvp_layer != best_hvm_layer:
                    # Find which analytic arc contains poly[best_ei]
                    snap_tol = pcbnew.FromMM(0.01)
                    px0, py0 = poly[best_ei]
                    arc_info = None
                    for (acx, acy, ar, asa, aea) in arc_primitives:
                        if abs(math.hypot(px0-acx, py0-acy) - ar) < snap_tol:
                            th0 = math.atan2(py0-acy, px0-acx)
                            while th0 < asa - 1e-9: th0 += 2*math.pi
                            if th0 <= aea + 1e-9:
                                arc_info = (acx, acy, ar, asa, aea); break

                    if arc_info is not None:
                        acx, acy, ar, asa, aea = arc_info
                        hvp_edges_l = hvp_edges_by_layer.get(best_hvp_layer, [])
                        hvm_edges_l = hvm_edges_by_layer.get(best_hvm_layer, [])

                        def closest_dist_to_edges(px, py, edges):
                            best_d = float('inf')
                            for x1,y1,x2,y2 in edges:
                                dx,dy=x2-x1,y2-y1; l2=dx*dx+dy*dy
                                if l2==0: d=math.hypot(px-x1,py-y1)
                                else:
                                    t=max(0.0,min(1.0,((px-x1)*dx+(py-y1)*dy)/l2))
                                    d=math.hypot(px-(x1+t*dx),py-(y1+t*dy))
                                if d<best_d: best_d=d
                            return best_d

                        z_iu_ref = pcbnew.FromMM(layer_gap_mm(best_hvp_layer, best_hvm_layer))

                        def arc_cost(theta):
                            px_t = acx + ar*math.cos(theta)
                            py_t = acy + ar*math.sin(theta)
                            d_hvp = closest_dist_to_edges(px_t, py_t, hvp_edges_l)
                            d_hvm = closest_dist_to_edges(px_t, py_t, hvm_edges_l)
                            return d_hvp + z_iu_ref + d_hvm  # IU

                        # Golden-section search over [asa, aea]
                        phi_gs = (1 + 5**0.5) / 2
                        a_gs, b_gs = asa, aea
                        c_gs = b_gs - (b_gs-a_gs)/phi_gs
                        d_gs = a_gs + (b_gs-a_gs)/phi_gs
                        fc, fd = arc_cost(c_gs), arc_cost(d_gs)
                        for _ in range(50):
                            if fc < fd:
                                b_gs = d_gs; d_gs = c_gs; fd = fc
                                c_gs = b_gs - (b_gs-a_gs)/phi_gs; fc = arc_cost(c_gs)
                            else:
                                a_gs = c_gs; c_gs = d_gs; fc = fd
                                d_gs = a_gs + (b_gs-a_gs)/phi_gs; fd = arc_cost(d_gs)
                        theta_opt = (a_gs+b_gs)/2
                        refined_total = arc_cost(theta_opt)

                        if refined_total < best_total_iu:
                            # Verify LOS for the refined contact point
                            px_opt = acx + ar*math.cos(theta_opt)
                            py_opt = acy + ar*math.sin(theta_opt)
                            pt_opt = (px_opt, py_opt)

                            def best_pt_on_edges(px, py, edges):
                                b_d,b_pt=float('inf'),None
                                for x1,y1,x2,y2 in edges:
                                    dx,dy=x2-x1,y2-y1; l2=dx*dx+dy*dy
                                    if l2==0: cx2,cy2=x1,y1
                                    else: t=max(0,min(1,((px-x1)*dx+(py-y1)*dy)/l2)); cx2,cy2=x1+t*dx,y1+t*dy
                                    d=math.hypot(px-cx2,py-cy2)
                                    if d<b_d: b_d,b_pt=d,(cx2,cy2)
                                return b_pt

                            hvp_ref = best_pt_on_edges(px_opt, py_opt, hvp_edges_l)
                            hvm_ref = best_pt_on_edges(px_opt, py_opt, hvm_edges_l)
                            if hvp_ref and hvm_ref:
                                if _tuple_los(hvp_ref, pt_opt) and _tuple_los(pt_opt, hvm_ref):
                                    best_total_iu = refined_total
                                    best_hvp = hvp_ref; best_hvm = hvm_ref
                                    # Replace best_ei/best_ej with the refined contact
                                    # (kept as a tuple, not a polygon index)
                                    poly_refined = (int(round(px_opt)), int(round(py_opt)))
                                    diag_lines.append(f"  Arc refinement: discrete best was "
                                                       f"{pcbnew.ToMM(int(round(best_total_iu))):.4f}mm before refinement, "
                                                       f"refined to {pcbnew.ToMM(int(round(refined_total))):.4f}mm "
                                                       f"at θ_opt={math.degrees(theta_opt):.2f}°")
                                    best_arc = [best_hvp, poly_refined, poly_refined, best_hvm]
                                    return best_total_iu, (best_arc, best_hvp_layer, best_hvm_layer, best_ei, best_ej)

                if best_ei == best_ej:
                    # Cross-layer zero-arc case: duplicate the contact point so the
                    # reconstruction assigns one copy to hvp_layer and one to hvm_layer.
                    p_arc = poly[best_ei]
                    best_arc = [best_hvp, p_arc, p_arc, best_hvm]
                else:
                    arc_f = walk_polygon_arc(poly, best_ei, best_ej, True)
                    arc_b = walk_polygon_arc(poly, best_ei, best_ej, False)
                    cand_f = [best_hvp] + arc_f + [best_hvm]
                    cand_b = [best_hvp] + arc_b + [best_hvm]
                    best_arc = cand_f if polyline_length(cand_f) <= polyline_length(cand_b) else cand_b
                return best_total_iu, (best_arc, best_hvp_layer, best_hvm_layer, best_ei, best_ej)

            # Find which obstacles appear in the smoothed path
            obs_in_path = {}
            for pt in smoothed_path:
                if pt.is_obstacle:
                    oidx = find_obstacle_index(pt.x, pt.y, pcbnew.FromMM(0.1))
                    if oidx is not None and oidx not in obs_in_path:
                        obs_in_path[oidx] = pt.layer

            for obs_idx, obs_layer in obs_in_path.items():
                glob_len_iu, glob_result = global_arc_minimum_for_obstacle(obs_idx, obs_layer)
                if glob_result is None: continue
                glob_arc, glob_hvp_layer, glob_hvm_layer, glob_ei, glob_ej = glob_result
                glob_len_mm = pcbnew.ToMM(int(round(glob_len_iu)))

                curr_len = 0.0
                for k in range(len(smoothed_path)-1):
                    p1, p2 = smoothed_path[k], smoothed_path[k+1]
                    if p1.layer == p2.layer:
                        curr_len += math.hypot(pcbnew.ToMM(p2.x-p1.x), pcbnew.ToMM(p2.y-p1.y))
                    else:
                        curr_len += layer_gap_mm(p1.layer, p2.layer)

                if glob_len_mm < curr_len - 0.001:
                    z_mm = layer_gap_mm(glob_hvp_layer, glob_hvm_layer)
                    diag_lines.append(f"Global optimizer: replaced A*+tangent path ({curr_len:.4f}mm) with "
                                       f"direct copper-to-copper search ({glob_len_mm:.4f}mm) for obstacle#{obs_idx} "
                                       f"(layers: {board.GetLayerName(glob_hvp_layer)}->{board.GetLayerName(glob_hvm_layer)}, "
                                       f"Z-transit={z_mm:.3f}mm)")
                    replace = True
                elif glob_hvp_layer != glob_hvm_layer:
                    # Cross-layer: A* may use phantom multi-hop transitions
                    # (e.g. In2.Cu→In1.Cu→F.Cu = 1.34mm instead of correct In2.Cu→F.Cu = 1.375mm),
                    # producing a shorter-but-physically-wrong path. The global optimizer
                    # uses only direct Z-gaps and is always authoritative for cross-layer.
                    z_mm = layer_gap_mm(glob_hvp_layer, glob_hvm_layer)
                    diag_lines.append(f"Global optimizer: cross-layer override ({curr_len:.4f}mm → {glob_len_mm:.4f}mm) "
                                       f"for obstacle#{obs_idx} "
                                       f"(layers: {board.GetLayerName(glob_hvp_layer)}->{board.GetLayerName(glob_hvm_layer)}, "
                                       f"Z-transit={z_mm:.3f}mm; A* may have used phantom hops)")
                    replace = True
                else:
                    diag_lines.append(f"Global optimizer: current path ({curr_len:.4f}mm) already optimal "
                                       f"vs direct search ({glob_len_mm:.4f}mm) for obstacle#{obs_idx}")
                    replace = False

                if replace:
                    hvp_x, hvp_y = int(round(glob_arc[0][0])), int(round(glob_arc[0][1]))
                    hvm_x, hvm_y = int(round(glob_arc[-1][0])), int(round(glob_arc[-1][1]))
                    arc_interior = glob_arc[1:-1]
                    mid_split = len(arc_interior) // 2
                    new_path = [PathPoint(glob_hvp_layer, hvp_x, hvp_y, False)]
                    for (ax, ay) in arc_interior[:mid_split]:
                        new_path.append(PathPoint(glob_hvp_layer, int(round(ax)), int(round(ay)), True))
                    for (ax, ay) in arc_interior[mid_split:]:
                        new_path.append(PathPoint(glob_hvm_layer, int(round(ax)), int(round(ay)), True))
                    new_path.append(PathPoint(glob_hvm_layer, hvm_x, hvm_y, False))
                    smoothed_path = new_path

            t5 = time.time()
            diag_lines.append(f"Timing - Smoothing + tangent correction + global opt: {t5-t4:.2f}s")
            _progress("Running exact global direct-distance search...", force=True)

            def _seg_seg_closest(p1, p2, p3, p4):
                # Exact closest points between two finite line segments (p1-p2)
                # and (p3-p4) — the true analytic minimum anywhere along both
                # edges, not just at their tessellated vertices. Shared by the
                # via-conductor search and the global direct minimum search.
                d1x, d1y = p2[0]-p1[0], p2[1]-p1[1]
                d2x, d2y = p4[0]-p3[0], p4[1]-p3[1]
                rx, ry = p1[0]-p3[0], p1[1]-p3[1]
                a = d1x*d1x + d1y*d1y
                e = d2x*d2x + d2y*d2y
                f = d2x*rx + d2y*ry
                if a <= 1e-9 and e <= 1e-9:
                    s = t = 0.0
                elif a <= 1e-9:
                    s = 0.0
                    t = max(0.0, min(1.0, f/e))
                else:
                    c = d1x*rx + d1y*ry
                    if e <= 1e-9:
                        t = 0.0
                        s = max(0.0, min(1.0, -c/a))
                    else:
                        b = d1x*d2x + d1y*d2y
                        denom = a*e - b*b
                        s = max(0.0, min(1.0, (b*f - c*e)/denom)) if denom != 0 else 0.0
                        t = (b*s + f) / e
                        if t < 0.0:
                            t = 0.0; s = max(0.0, min(1.0, -c/a))
                        elif t > 1.0:
                            t = 1.0; s = max(0.0, min(1.0, (b - c)/a))
                pt1 = (p1[0] + d1x*s, p1[1] + d1y*s)
                pt2 = (p3[0] + d2x*t, p3[1] + d2y*t)
                return math.hypot(pt1[0]-pt2[0], pt1[1]-pt2[1]), pt1, pt2

            # =====================================================================
            # GLOBAL DIRECT MINIMUM SEARCH — exact, grid-independent closest-point
            # search between HV+ and HV- boundaries on each layer, for the case
            # where no obstacle wrapping is needed at all. The main A*+smoothing
            # pipeline is grid-quantized (res_iu resolution) even though its
            # endpoints get snapped onto the true boundary afterward — the CHOICE
            # of which boundary region to snap from is still limited by that
            # grid, so a true global minimum sitting between two grid steps can
            # be missed by a few microns. This checks every HV+/HV- edge pair on
            # the same layer directly (already-tessellated exact edges, so this
            # is exact down to the tessellation tolerance, not the grid
            # resolution) and takes it if it's shorter and has a clear line of
            # sight (no obstacle actually requires routing around).
            # =====================================================================
            direct_best_mm = None
            direct_best_pts = None  # (layer, pt_a, pt_b)
            for layer in enabled_copper_layers:
                hvp_edges_l = hvp_edges_by_layer.get(layer, [])
                hvm_edges_l = hvm_edges_by_layer.get(layer, [])
                if not hvp_edges_l or not hvm_edges_l:
                    continue
                best_d_l, best_pts_l, best_raw_edges_l = float('inf'), None, None
                for (ax1, ay1, ax2, ay2) in hvp_edges_l:
                    for (bx1, by1, bx2, by2) in hvm_edges_l:
                        d, pa, pb = _seg_seg_closest((ax1, ay1), (ax2, ay2), (bx1, by1), (bx2, by2))
                        if d < best_d_l:
                            best_d_l, best_pts_l = d, (pa, pb)
                            best_raw_edges_l = ((ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2))
                if best_pts_l is not None:
                    pa, pb = best_pts_l
                    if _tuple_los(pa, pb):
                        d_mm = pcbnew.ToMM(int(round(best_d_l)))
                        if direct_best_mm is None or d_mm < direct_best_mm:
                            direct_best_mm = d_mm
                            direct_best_pts = (layer, pa, pb)
                            (rax1, ray1, rax2, ray2), (rbx1, rby1, rbx2, rby2) = best_raw_edges_l
                            diag_lines.append(f"Global direct search candidate: {d_mm:.4f}mm on "
                                               f"{board.GetLayerName(layer)} (clear line of sight, no obstacle)")
                            diag_lines.append(f"  winning HV+ edge (raw, mm): "
                                               f"({pcbnew.ToMM(rax1):.6f},{pcbnew.ToMM(ray1):.6f}) -> "
                                               f"({pcbnew.ToMM(rax2):.6f},{pcbnew.ToMM(ray2):.6f})")
                            diag_lines.append(f"  winning HV- edge (raw, mm): "
                                               f"({pcbnew.ToMM(rbx1):.6f},{pcbnew.ToMM(rby1):.6f}) -> "
                                               f"({pcbnew.ToMM(rbx2):.6f},{pcbnew.ToMM(rby2):.6f})")
                            diag_lines.append(f"  closest point HV+ side (mm): "
                                               f"({pcbnew.ToMM(int(round(pa[0]))):.6f},{pcbnew.ToMM(int(round(pa[1]))):.6f}) "
                                               f"| HV- side (mm): "
                                               f"({pcbnew.ToMM(int(round(pb[0]))):.6f},{pcbnew.ToMM(int(round(pb[1]))):.6f})")

            t5b = time.time()
            diag_lines.append(f"Timing - Global direct search: {t5b-t5:.2f}s")
            _progress("Finalizing result and drawing output...", force=True)

            # The current best distance found so far (main A*+tangent+global-arc
            # path), computed HERE (before via-conductor search) so it can be used
            # as a pruning bound below — a conductor whose bounding box can't
            # possibly beat this distance doesn't need its edges examined at all.
            curr_direct_mm = 0.0
            for k in range(len(smoothed_path)-1):
                p1t, p2t = smoothed_path[k], smoothed_path[k+1]
                if p1t.layer == p2t.layer:
                    curr_direct_mm += math.hypot(pcbnew.ToMM(p2t.x-p1t.x), pcbnew.ToMM(p2t.y-p1t.y))
                else:
                    curr_direct_mm += layer_gap_mm(p1t.layer, p2t.layer)

            def _bbox_of_points(pts):
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                return (min(xs), min(ys), max(xs), max(ys))

            def _bbox_of_edges(edges):
                xs, ys = [], []
                for (x1, y1, x2, y2) in edges:
                    xs.append(x1); xs.append(x2); ys.append(y1); ys.append(y2)
                return (min(xs), min(ys), max(xs), max(ys)) if xs else None

            def _bbox_gap(bb1, bb2):
                # Lower-bound distance between two axis-aligned boxes (0 if they
                # overlap/touch). Always <= the true minimum distance between any
                # geometry inside them, so it's safe to use as a pruning bound.
                dx = max(bb1[0] - bb2[2], bb2[0] - bb1[2], 0)
                dy = max(bb1[1] - bb2[3], bb2[1] - bb1[3], 0)
                return math.hypot(dx, dy)

            # VIA-CONDUCTOR SHORTCUT SEARCH
            # IEC creepage rule: when a conductive part (third net, e.g. GND) sits between
            # two HV conductors, the surface path goes TO the conductor edge (normal surface
            # distance), skips ACROSS the conductor (0mm — it's copper), then FROM the
            # other/same conductor edge to the second HV conductor. Total = d_hvm + 0 + d_hvp.
            # This can be dramatically shorter than routing around the conductor as an obstacle.
            via_cond_best_mm = None
            via_cond_path_pts = None  # (vc_layer, hvm_boundary, cond_entry, cond_exit, hvp_boundary)

            if conductor_polys_by_layer:
                _pruned_conductors = 0
                _examined_conductors = 0
                for vc_layer in enabled_copper_layers:
                    if vc_layer not in conductor_polys_by_layer: continue
                    hvp_l_vc = hvp_edges_by_layer.get(vc_layer, [])
                    hvm_l_vc = hvm_edges_by_layer.get(vc_layer, [])
                    if not hvp_l_vc or not hvm_l_vc: continue

                    hvp_bbox_vc = _bbox_of_edges(hvp_l_vc)
                    hvm_bbox_vc = _bbox_of_edges(hvm_l_vc)
                    # Per-edge bboxes precomputed ONCE per layer, reused across every
                    # conductor on that layer — this is what lets the inner loop reject
                    # a pair with a single cheap comparison instead of running the full
                    # exact segment math (and, worse, the O(exact_hole_edges) visibility
                    # check) on every single pair. Critical for a large ground pour with
                    # thousands of edges from correctly-processed clearance holes.
                    def _edge_bbox(e):
                        x1, y1, x2, y2 = e
                        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                    hvm_edges_bb_vc = [_edge_bbox(e) for e in hvm_l_vc]
                    hvp_edges_bb_vc = [_edge_bbox(e) for e in hvp_l_vc]
                    hvm_spatial_vc = _build_bucket_index(hvm_edges_bb_vc)
                    hvp_spatial_vc = _build_bucket_index(hvp_edges_bb_vc)

                    def _hv_candidates_for(spatial_idx, g_bbox, margin_iu):
                        bxlo, bylo = _bucket_of(g_bbox[0] - margin_iu, g_bbox[1] - margin_iu)
                        bxhi, byhi = _bucket_of(g_bbox[2] + margin_iu, g_bbox[3] + margin_iu)
                        seen = set()
                        out = []
                        for bxi in range(bxlo, bxhi + 1):
                            for byi in range(bylo, byhi + 1):
                                for idx in spatial_idx.get((bxi, byi), ()):
                                    if idx not in seen:
                                        seen.add(idx)
                                        out.append(idx)
                        return out

                    for vc_poly in conductor_polys_by_layer[vc_layer]:
                        # Best possible via-conductor total is bounded below by the
                        # bbox-to-bbox gaps on each side — if that lower bound
                        # can't beat the best distance found so far (main path OR
                        # any via-conductor candidate already found), this
                        # conductor's actual edges never need to be examined.
                        best_so_far_mm = curr_direct_mm if via_cond_best_mm is None else min(curr_direct_mm, via_cond_best_mm)
                        best_so_far_iu = pcbnew.FromMM(best_so_far_mm)
                        vc_bbox = _bbox_of_points(vc_poly)
                        lower_bound = _bbox_gap(vc_bbox, hvm_bbox_vc) + _bbox_gap(vc_bbox, hvp_bbox_vc)
                        if lower_bound >= best_so_far_iu:
                            _pruned_conductors += 1
                            continue
                        _examined_conductors += 1
                        _progress(f"Via search: {_examined_conductors} examined, {_pruned_conductors} pruned")
                        # INDEPENDENT optimisation: find best HV- contact and best HV+ contact
                        # separately — the conductor lets them be at different points (0mm transit).
                        # Searched EDGE-to-EDGE (both the GND boundary and the HV boundary are
                        # polylines, not point sets), so the result is the true geometric
                        # minimum distance between the two boundaries, not a vertex-sampled
                        # approximation of it.
                        best_d_hvm_vc, best_hvm_pt_vc, best_hvm_cpt = float('inf'), None, None
                        best_d_hvp_vc, best_hvp_pt_vc, best_hvp_cpt = float('inf'), None, None

                        n_vc = len(vc_poly)
                        vc_edges_raw = [(vc_poly[i], vc_poly[(i+1) % n_vc]) for i in range(n_vc)]

                        def _vc_edge_bbox(e):
                            ea, eb = e
                            return (min(ea[0], eb[0]), min(ea[1], eb[1]), max(ea[0], eb[0]), max(ea[1], eb[1]))

                        # Sort by proximity to the combined HV+/HV- region so the
                        # tightest best-so-far bound gets established almost
                        # immediately, letting the per-pair pruning below reject
                        # the (likely large) majority of a big conductor's edges
                        # outright instead of scanning them with a still-loose
                        # bound. Without this, edges are examined in raw polygon
                        # order, and if the genuinely close ones happen to appear
                        # late, most of a multi-thousand-edge zone gets fully
                        # scanned before pruning helps at all — this is what made
                        # a whole-board ground pour with correctly-processed
                        # clearance holes take minutes instead of seconds.
                        vc_edges = [(e, _vc_edge_bbox(e)) for e in vc_edges_raw]
                        vc_edges.sort(key=lambda eb: min(_bbox_gap(eb[1], hvm_bbox_vc), _bbox_gap(eb[1], hvp_bbox_vc)))

                        for _ei, ((g1, g2), g_bbox) in enumerate(vc_edges):
                            if _ei % 50 == 0:
                                _progress(f"Via search: conductor {_examined_conductors}, edge {_ei}/{len(vc_edges)}")
                            for hi in _hv_candidates_for(hvm_spatial_vc, g_bbox, best_so_far_iu):
                                hx1, hy1, hx2, hy2 = hvm_l_vc[hi]
                                if _bbox_gap(g_bbox, hvm_edges_bb_vc[hi]) >= best_d_hvm_vc: continue
                                dist, gpt, hpt = _seg_seg_closest(g1, g2, (hx1, hy1), (hx2, hy2))
                                if dist < best_d_hvm_vc and _tuple_los(hpt, gpt):
                                    best_d_hvm_vc, best_hvm_pt_vc, best_hvm_cpt = dist, hpt, gpt
                            for hi in _hv_candidates_for(hvp_spatial_vc, g_bbox, best_so_far_iu):
                                hx1, hy1, hx2, hy2 = hvp_l_vc[hi]
                                if _bbox_gap(g_bbox, hvp_edges_bb_vc[hi]) >= best_d_hvp_vc: continue
                                dist, gpt, hpt = _seg_seg_closest(g1, g2, (hx1, hy1), (hx2, hy2))
                                if dist < best_d_hvp_vc and _tuple_los(gpt, hpt):
                                    best_d_hvp_vc, best_hvp_pt_vc, best_hvp_cpt = dist, hpt, gpt

                        if best_hvm_pt_vc and best_hvp_pt_vc:
                            via_total_mm = pcbnew.ToMM(int(round(best_d_hvm_vc + best_d_hvp_vc)))
                            if via_cond_best_mm is None or via_total_mm < via_cond_best_mm:
                                via_cond_best_mm = via_total_mm
                                via_cond_path_pts = (vc_layer, best_hvm_pt_vc, best_hvm_cpt,
                                                     best_hvp_cpt, best_hvp_pt_vc)
                                diag_lines.append(f"Via-conductor candidate: {via_total_mm:.4f}mm on "
                                                   f"{board.GetLayerName(vc_layer)} "
                                                   f"(d_HVM={pcbnew.ToMM(int(round(best_d_hvm_vc))):.4f}mm, "
                                                   f"d_HVP={pcbnew.ToMM(int(round(best_d_hvp_vc))):.4f}mm)")

                diag_lines.append(f"Via-conductor search: {_examined_conductors} conductor(s) examined, "
                                   f"{_pruned_conductors} pruned by bounding box before touching their edges "
                                   f"(out of {_examined_conductors + _pruned_conductors} total candidates)")

            t5c = time.time()
            diag_lines.append(f"Timing - Via-conductor search: {t5c-t5b:.2f}s")
            _progress("Drawing final path and markers...", force=True)

            # Pick the shortest of: the main A*+tangent+global-arc-optimizer path,
            # the via-conductor shortcut, and the new exact direct search — each
            # targets a different scenario (detour required, third-net conductor
            # shortcut, clear line of sight) and only one will actually apply for
            # any given board, but whichever is genuinely shortest wins.
            # (curr_direct_mm was already computed above, before the via-conductor
            # search, where it's needed as a pruning bound — smoothed_path hasn't
            # changed since, so recomputing it here would just be redundant work.)
            used_via_conductor = False
            if via_cond_best_mm is not None and via_cond_best_mm < curr_direct_mm - 0.001:
                diag_lines.append(f"Via-conductor wins: {via_cond_best_mm:.4f}mm < direct {curr_direct_mm:.4f}mm — using conductor shortcut path")
                vc_l, hvm_pt_vc, hvm_cpt_vc, hvp_cpt_vc, hvp_pt_vc = via_cond_path_pts
                smoothed_path = [
                    PathPoint(vc_l, hvm_pt_vc[0], hvm_pt_vc[1], False, False),         # HV- boundary
                    PathPoint(vc_l, hvm_cpt_vc[0], hvm_cpt_vc[1], False, True),         # conductor entry (0mm transit starts)
                    PathPoint(vc_l, hvp_cpt_vc[0], hvp_cpt_vc[1], False, True),         # conductor exit  (0mm transit ends)
                    PathPoint(vc_l, hvp_pt_vc[0], hvp_pt_vc[1], False, False),           # HV+ boundary
                ]
                used_via_conductor = True
                curr_direct_mm = via_cond_best_mm
            elif via_cond_best_mm is not None:
                diag_lines.append(f"Via-conductor ({via_cond_best_mm:.4f}mm) not shorter than direct path ({curr_direct_mm:.4f}mm)")

            if direct_best_mm is not None and direct_best_mm < curr_direct_mm - 0.001:
                diag_lines.append(f"Global direct search wins: {direct_best_mm:.4f}mm < {curr_direct_mm:.4f}mm — "
                                   f"replacing with the exact closest-point path (this bypasses grid-resolution "
                                   f"limits the main A* search is subject to)")
                d_layer, d_pa, d_pb = direct_best_pts
                smoothed_path = [
                    PathPoint(d_layer, d_pa[0], d_pa[1], False),
                    PathPoint(d_layer, d_pb[0], d_pb[1], False),
                ]
                used_via_conductor = False
            elif direct_best_mm is not None:
                diag_lines.append(f"Global direct search ({direct_best_mm:.4f}mm) not shorter than current best ({curr_direct_mm:.4f}mm)")

            final_distance_mm = 0.0
            z_transition_notes = []
            _kind_labels = {
                'npth': 'NPTH hole wall',
                'slot_or_edge': 'slot / board-edge cutout wall',
                'third_net': 'third-net conductor surface (via-conductor shortcut)',
            }
            for i in range(len(smoothed_path) - 1):
                pt1, pt2 = smoothed_path[i], smoothed_path[i+1]
                # Conductor-transit segments: 0mm creepage (copper is conductive)
                if pt1.is_conductor_transit and pt2.is_conductor_transit:
                    continue
                if pt1.layer == pt2.layer:
                    dx, dy = pcbnew.ToMM(pt2.x - pt1.x), pcbnew.ToMM(pt2.y - pt1.y)
                    final_distance_mm += math.hypot(dx, dy)
                else:
                    gap_mm = layer_gap_mm(pt1.layer, pt2.layer)
                    final_distance_mm += gap_mm
                    # Tag this Z-transition with what physical discontinuity it passes
                    # through — every layer change in this tool's model happens at an
                    # exposed boundary of some kind (there's no way to change layers
                    # along a surface path through unbroken solid stackup), so this is
                    # always resolvable to one of: an NPTH hole wall, a slot/board-edge
                    # cutout wall, or (for the via-conductor shortcut specifically) a
                    # third-net conductor's own copper surface.
                    if used_via_conductor:
                        kind = 'third_net'
                    else:
                        mx, my = (pt1.x + pt2.x) / 2.0, (pt1.y + pt2.y) / 2.0
                        oidx = find_obstacle_index(mx, my, pcbnew.FromMM(0.15))
                        kind = obstacle_kind[oidx] if (oidx is not None and oidx < len(obstacle_kind)) else None
                    label = _kind_labels.get(kind, "unrecognized discontinuity (not matched within 0.15mm — verify manually)")
                    z_transition_notes.append(
                        f"  Z-transition {board.GetLayerName(pt1.layer)} -> {board.GetLayerName(pt2.layer)} "
                        f"at ({pcbnew.ToMM(pt1.x):.3f}, {pcbnew.ToMM(pt1.y):.3f}) mm: through {label}, "
                        f"distance = {gap_mm:.4f} mm")

            if z_transition_notes:
                diag_lines.append("===== Z-TRANSITION EXPOSURE TAGGING (for your own pollution-degree judgment) =====")
                diag_lines.append("NOTE: an NPTH/slot/board-edge wall is an exposed, uncoatable discontinuity — "
                                   "the standard's 'worst micro-environment governs the whole path' principle "
                                   "(see prior discussion) means encountering ANY of these along a path is a "
                                   "reason to use the higher/uncoated pollution degree for that path's entire "
                                   "requirement, not just the transition itself.")
                diag_lines.extend(z_transition_notes)

            def get_visual_layer(l):
                return pcbnew.F_Adhes if l == pcbnew.F_Cu else (pcbnew.B_Adhes if l == pcbnew.B_Cu else pcbnew.Dwgs_User)

            def draw_marker_circle(x, y, layer, radius_mm=0.10):
                # Small unfilled circle at an exact contact point, so the point
                # can be located and cross-checked (e.g. with KiCad's own
                # measure tool) without having to guess where the path
                # geometry actually starts/ends/transits.
                circ = pcbnew.PCB_SHAPE(board)
                circ.SetShape(pcbnew.SHAPE_T_CIRCLE)
                circ.SetCenter(pcbnew.VECTOR2I(int(round(x)), int(round(y))))
                circ.SetEnd(pcbnew.VECTOR2I(int(round(x)) + pcbnew.FromMM(radius_mm), int(round(y))))
                circ.SetLayer(get_visual_layer(layer))
                circ.SetWidth(magic_marker_width)
                board.Add(circ)

            if smoothed_path:
                # Always mark where the measured path actually touches each net's
                # copper — the two ends of the path.
                draw_marker_circle(smoothed_path[0].x, smoothed_path[0].y, smoothed_path[0].layer)
                draw_marker_circle(smoothed_path[-1].x, smoothed_path[-1].y, smoothed_path[-1].layer)
                if used_via_conductor:
                    # Also mark the two conductor entry/exit contact points
                    # (smoothed_path[1] and [2] in the via-conductor 4-point path).
                    draw_marker_circle(smoothed_path[1].x, smoothed_path[1].y, smoothed_path[1].layer)
                    draw_marker_circle(smoothed_path[2].x, smoothed_path[2].y, smoothed_path[2].layer)

            arc_snap_tol = pcbnew.FromMM(0.005)  # 5 micron — well within arc tessellation error

            def draw_segments_arc_aware(px1, py1, px2, py2, layer, is_obstacle_seg):
                vis_layer = get_visual_layer(layer)
                if is_obstacle_seg:
                    arc1 = find_arc_for_point(px1, py1, arc_snap_tol)
                    arc2 = find_arc_for_point(px2, py2, arc_snap_tol)
                    if arc1 is not None and arc2 is not None and arc1 == arc2:
                        # Both endpoints on the same arc: resample at high density
                        # along the true circumference instead of drawing a chord
                        # between coarse polygon ring points.
                        cx, cy, r, sa, ea = arc1
                        a1 = math.atan2(py1-cy, px1-cx)
                        a2 = math.atan2(py2-cy, px2-cx)
                        while a1 < sa - 1e-9: a1 += 2*math.pi
                        while a2 < sa - 1e-9: a2 += 2*math.pi
                        arc_span = abs(a2 - a1)
                        if arc_span > 1e-9:
                            n_steps = max(2, int(r * arc_span / pcbnew.FromMM(0.01)))
                            a_start, a_end = (a1, a2) if a1 < a2 else (a2, a1)
                            rev = a1 > a2
                            prev_x = int(round(cx + r * math.cos(a_start)))
                            prev_y = int(round(cy + r * math.sin(a_start)))
                            for step in range(1, n_steps + 1):
                                t = step / n_steps
                                a = a_start + (a_end - a_start) * t
                                nx = int(round(cx + r * math.cos(a)))
                                ny = int(round(cy + r * math.sin(a)))
                                s = pcbnew.PCB_SHAPE(board)
                                s.SetShape(pcbnew.SHAPE_T_SEGMENT)
                                s.SetStart(pcbnew.VECTOR2I(prev_x, prev_y) if not rev else pcbnew.VECTOR2I(nx, ny))
                                s.SetEnd(pcbnew.VECTOR2I(nx, ny) if not rev else pcbnew.VECTOR2I(prev_x, prev_y))
                                s.SetLayer(vis_layer)
                                s.SetWidth(magic_width)
                                board.Add(s)
                                prev_x, prev_y = nx, ny
                            return
                seg = pcbnew.PCB_SHAPE(board)
                seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
                seg.SetStart(pcbnew.VECTOR2I(px1, py1))
                seg.SetEnd(pcbnew.VECTOR2I(px2, py2))
                seg.SetLayer(vis_layer)
                seg.SetWidth(magic_width)
                board.Add(seg)

            for i in range(len(smoothed_path) - 1):
                pt1, pt2 = smoothed_path[i], smoothed_path[i+1]
                # Conductor-transit interior: the path is inside a conductor (0mm creepage).
                # Don't draw this segment — the user sees only the two approach lines.
                if pt1.is_conductor_transit and pt2.is_conductor_transit:
                    continue
                if pt1.layer == pt2.layer:
                    is_obs = pt1.is_obstacle and pt2.is_obstacle
                    draw_segments_arc_aware(pt1.x, pt1.y, pt2.x, pt2.y, pt1.layer, is_obs)
                # Cross-layer transitions are intentionally not drawn — the Z-distance
                # (pure dielectric gap between copper layers) is included in
                # final_distance_mm via layer_gap_mm(), but no visual element is
                # placed for it since no via representation is wanted.

            mid_pt = smoothed_path[len(smoothed_path)//2] if smoothed_path else None
            pos = pcbnew.VECTOR2I(mid_pt.x, mid_pt.y) if mid_pt else None

            line1 = f"'{NET_A_NAME}' to '{NET_B_NAME}'"
            line2 = f"Creepage: {final_distance_mm:.3f} mm"
            lines = [line1, line2]
            if required_creepage_mm is not None:
                margin_mm = final_distance_mm - required_creepage_mm
                verdict_word = "PASS" if margin_mm >= 0 else "FAIL"
                lines.append(f"IEC REQUIRED: {required_creepage_mm:.3f} mm")
                lines.append(f"{verdict_word} (margin {margin_mm:+.3f} mm)")
            self.draw_text(board, comment_layer, "\n".join(lines), pos)
            diag_lines.append(f"Final measured distance: {final_distance_mm:.3f} mm")
            if required_creepage_mm is not None:
                diag_lines.append(f"Required creepage (IEC 60664-1): {required_creepage_mm:.3f} mm | "
                                   f"Margin: {final_distance_mm - required_creepage_mm:+.3f} mm | "
                                   f"Verdict: {'PASS' if final_distance_mm >= required_creepage_mm else 'FAIL'}")
            diag_lines.append(f"Timing - Drawing/tagging/markers: {time.time()-t5c:.2f}s")
            diag_lines.append(f"Total time: {time.time()-t0:.2f}s")
            flush_diagnostics()
        else:
            diag_lines.append("NO PATH FOUND")
            self.draw_text(board, comment_layer, f"NO PATH FOUND between '{NET_A_NAME}' and '{NET_B_NAME}'", None)
            flush_diagnostics()

        if progress_dlg is not None:
            progress_dlg.Destroy()

    def show_config_dialog(self, net_names):
        """
        Modal dialog collecting: net A, net B, voltage-or-distance input mode,
        pollution degree, and material group. Returns a dict of
        results, or None if the user cancelled.
        """
        dlg = wx.Dialog(None, title="Creepage Solver — Net & IEC 60664-1 Setup",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(dlg)
        vbox = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Net A:"), 0, wx.ALIGN_CENTER_VERTICAL)
        choice_a = wx.Choice(panel, choices=net_names)
        default_a = next((n for n in net_names if "HV+" in n or n.upper() == "HV+"), net_names[0])
        choice_a.SetStringSelection(default_a)
        grid.Add(choice_a, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Net B:"), 0, wx.ALIGN_CENTER_VERTICAL)
        choice_b = wx.Choice(panel, choices=net_names)
        default_b = next((n for n in net_names if "HV-" in n or n.upper() == "HV-"), net_names[-1])
        choice_b.SetStringSelection(default_b)
        grid.Add(choice_b, 1, wx.EXPAND)

        vbox.Add(grid, 0, wx.ALL | wx.EXPAND, 12)

        refill_check = wx.CheckBox(panel, label=(
            "Refill net A/B zones before measuring (only these two nets — "
            "never touches any other net's zones)"))
        refill_check.SetValue(False)
        vbox.Add(refill_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        refill_note = wx.StaticText(panel, label=(
            "Off by default: forcing a zone fill is a real edit to the board and can\n"
            "surface unrelated fill-time conflicts, which some HV designs deliberately\n"
            "avoid mid-layout. Leave unchecked and refill manually first if that matters\n"
            "to you; check this only if you want current geometry guaranteed."))
        refill_note.Wrap(420)
        vbox.Add(refill_note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        vbox.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        source_label = wx.StaticText(panel, label=(
            "Required creepage source — fill in EITHER field below.\n"
            "If both are filled, the direct distance takes priority."))
        vbox.Add(source_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

        grid2 = wx.FlexGridSizer(rows=0, cols=2, vgap=8, hgap=10)
        grid2.AddGrowableCol(1, 1)

        grid2.Add(wx.StaticText(panel, label="Required creepage (mm), direct entry:"), 0, wx.ALIGN_CENTER_VERTICAL)
        distance_ctrl = wx.TextCtrl(panel, value="")
        grid2.Add(distance_ctrl, 1, wx.EXPAND)

        grid2.Add(wx.StaticText(panel, label="OR working voltage (V, RMS or DC):"), 0, wx.ALIGN_CENTER_VERTICAL)
        voltage_ctrl = wx.TextCtrl(panel, value="")
        grid2.Add(voltage_ctrl, 1, wx.EXPAND)

        grid2.Add(wx.StaticText(panel, label="Pollution degree:"), 0, wx.ALIGN_CENTER_VERTICAL)
        pd_choice = wx.Choice(panel, choices=["1", "2", "3"])
        pd_choice.SetStringSelection("2")
        grid2.Add(pd_choice, 1, wx.EXPAND)

        grid2.Add(wx.StaticText(panel, label="Material group (FR4 = III):"), 0, wx.ALIGN_CENTER_VERTICAL)
        mg_choice = wx.Choice(panel, choices=["I", "II", "III"])
        mg_choice.SetStringSelection("III")
        grid2.Add(mg_choice, 1, wx.EXPAND)

        vbox.Add(grid2, 0, wx.ALL | wx.EXPAND, 12)

        pwb_check = wx.CheckBox(panel, label=(
            "Use printed wiring material (PCB conductor) values instead of "
            "material group — only tabulated for pollution degree 1 or 2"))
        vbox.Add(pwb_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        def _on_pwb_toggle(evt):
            mg_choice.Enable(not pwb_check.GetValue())
        pwb_check.Bind(wx.EVT_CHECKBOX, _on_pwb_toggle)

        note = wx.StaticText(panel, label=(
            "Note: FR4 is normally material group IIIa/IIIb, both covered by the\n"
            "'III' column here (IEC 60664-1 doesn't split them except for a\n"
            "restriction on IIIb above 630 V at pollution degree 3, flagged in\n"
            "the result if it applies). Values from IEC 60664-1:2020 Table F.5."))
        note.Wrap(420)
        vbox.Add(note, 0, wx.ALL, 12)

        btn_ok = wx.Button(panel, wx.ID_OK, "OK")
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_ok.SetDefault()
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.Add(btn_cancel, 0, wx.RIGHT, 8)
        btn_sizer.Add(btn_ok, 0)
        vbox.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_RIGHT, 12)

        panel.SetSizer(vbox)
        vbox.SetMinSize((460, -1))
        dlg_outer = wx.BoxSizer(wx.VERTICAL)
        dlg_outer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizer(dlg_outer)
        dlg.Fit()
        dlg.Layout()
        dlg.CenterOnScreen()

        result = None
        while True:
            if dlg.ShowModal() != wx.ID_OK:
                dlg.Destroy()
                return None

            net_a = choice_a.GetStringSelection()
            net_b = choice_b.GetStringSelection()
            if not net_a or not net_b or net_a == net_b:
                wx.MessageBox("Please select two different nets for Net A and Net B.",
                               "Invalid selection", wx.OK | wx.ICON_ERROR)
                continue

            distance_raw = distance_ctrl.GetValue().strip()
            voltage_raw = voltage_ctrl.GetValue().strip()
            try:
                distance_mm = float(distance_raw) if distance_raw else None
            except ValueError:
                distance_mm = None
            try:
                voltage_v = float(voltage_raw) if voltage_raw else None
            except ValueError:
                voltage_v = None

            if distance_raw and distance_mm is None:
                wx.MessageBox("Creepage distance must be a valid number.",
                               "Invalid input", wx.OK | wx.ICON_ERROR)
                continue
            if voltage_raw and voltage_v is None:
                wx.MessageBox("Working voltage must be a valid number.",
                               "Invalid input", wx.OK | wx.ICON_ERROR)
                continue

            # Direct distance entry takes priority whenever it's filled in —
            # voltage/table lookup is only used as a fallback when distance
            # is left blank.
            if distance_mm is not None and distance_mm >= 0:
                mode = "distance"
            elif voltage_v is not None and voltage_v > 0:
                mode = "voltage"
            else:
                wx.MessageBox("Enter either a required creepage distance (mm) or a working voltage (V).",
                               "Invalid input", wx.OK | wx.ICON_ERROR)
                continue

            pollution_degree = int(pd_choice.GetStringSelection())
            use_pwb = pwb_check.GetValue()
            if mode == "voltage" and use_pwb and pollution_degree == 3:
                wx.MessageBox("Printed wiring material values are only tabulated for "
                               "pollution degree 1 or 2. Choose one of those, or uncheck "
                               "the printed-wiring-material option.",
                               "Invalid input", wx.OK | wx.ICON_ERROR)
                continue

            result = {
                "net_a": net_a,
                "net_b": net_b,
                "mode": mode,
                "voltage_v": voltage_v,
                "distance_mm": distance_mm,
                "pollution_degree": pollution_degree,
                "material_group": mg_choice.GetStringSelection(),
                "use_pwb": use_pwb,
                "refill_ab_zones": refill_check.GetValue(),
            }
            break

        dlg.Destroy()
        return result

    def draw_text(self, board, layer, text, pos):
        txt = pcbnew.PCB_TEXT(board)
        txt.SetText(text)
        if not pos: pos = board.GetBoardEdgesBoundingBox().GetCenter()
        txt.SetPosition(pos)
        txt.SetLayer(layer)
        txt.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.6), pcbnew.FromMM(0.6)))
        board.Add(txt)
        pcbnew.UpdateUserInterface()
        pcbnew.Refresh()


# =============================================================================
# IEC 60664-1:2020 TABLE F.5 — "Creepage distances to avoid failure due to
# tracking", as a function of working voltage (RMS a.c. or d.c.), pollution
# degree, and material group.
#
# Transcribed directly from the standard's own Table F.5 (pages 72-73) as
# supplied by the user — NOT reconstructed from secondary sources. Covers
# 10 V to 10,000 V; the table continues to 63,000 V but those rows are
# outside any realistic PCB working voltage and are omitted here to avoid
# transcribing figures from a photographed page without a second source to
# cross-check them against.
#
# Two families of columns, per the standard:
#   - "Printed wiring material" (PWB_*): reduced values that apply
#     specifically to the conductors/traces of the printed board itself.
#     Only tabulated for pollution degrees 1 and 2 (not 3) and does not
#     distinguish material group — that's what "all material groups,
#     except IIIb" in the PD2 column heading means.
#   - General "material group" columns (GENERAL_*): apply to equipment/
#     component creepage generally. At pollution degree 1 the value doesn't
#     depend on material group. At PD2 and PD3 the standard's own column
#     headings only split out groups I, II, and III (not IIIa vs IIIb
#     separately) — group III covers both, EXCEPT that per footnote (b),
#     material group IIIb is specifically flagged as "not recommended for
#     application in pollution degree 3 above 630 V". This lookup still
#     returns the tabulated PD3 group-III value above 630 V, but flags that
#     footnote in the returned note so it surfaces to the user rather than
#     being silently ignored.
# =============================================================================

IEC_F5_PWB_VOLTAGES = [10, 12.5, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125,
                        160, 200, 250, 320, 400, 500, 630, 800, 1000]

IEC_F5_PWB_TABLE = {
    1: [0.025, 0.025, 0.025, 0.025, 0.025, 0.025, 0.025, 0.025, 0.040, 0.063,
        0.100, 0.160, 0.250, 0.400, 0.560, 0.75, 1.0, 1.3, 1.8, 2.4, 3.2],
    2: [0.040, 0.040, 0.040, 0.040, 0.040, 0.040, 0.040, 0.040, 0.063, 0.100,
        0.160, 0.250, 0.400, 0.630, 1.000, 1.60, 2.0, 2.5, 3.2, 4.0, 5.0],
}

IEC_F5_GENERAL_VOLTAGES = [10, 12.5, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125,
                            160, 200, 250, 320, 400, 500, 630, 800, 1000, 1250,
                            1600, 2000, 2500, 3200, 4000, 5000, 6300, 8000, 10000]

IEC_F5_GENERAL_TABLE = {
    1: {  # pollution degree 1 — same value regardless of material group
        'I':   [0.080, 0.090, 0.100, 0.110, 0.125, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25, 0.28, 0.32, 0.42, 0.56, 0.75, 1.0, 1.3, 1.8, 2.4, 3.2, 4.2, 5.6, 7.5, 10.0, 12.5, 16.0, 20.0, 25.0, 32.0, 40.0],
        'II':  [0.080, 0.090, 0.100, 0.110, 0.125, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25, 0.28, 0.32, 0.42, 0.56, 0.75, 1.0, 1.3, 1.8, 2.4, 3.2, 4.2, 5.6, 7.5, 10.0, 12.5, 16.0, 20.0, 25.0, 32.0, 40.0],
        'III': [0.080, 0.090, 0.100, 0.110, 0.125, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25, 0.28, 0.32, 0.42, 0.56, 0.75, 1.0, 1.3, 1.8, 2.4, 3.2, 4.2, 5.6, 7.5, 10.0, 12.5, 16.0, 20.0, 25.0, 32.0, 40.0],
    },
    2: {
        'I':   [0.400, 0.420, 0.450, 0.480, 0.500, 0.53, 0.56, 0.60, 0.63, 0.67, 0.71, 0.75, 0.80, 1.00, 1.25, 1.60, 2.0, 2.5, 3.2, 4.0, 5.0, 6.3, 8.0, 10.0, 12.5, 16.0, 20.0, 25.0, 32.0, 40.0, 50.0],
        'II':  [0.400, 0.420, 0.450, 0.480, 0.500, 0.53, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.40, 1.80, 2.20, 2.8, 3.6, 4.5, 5.6, 7.1, 9.0, 11.0, 14.0, 18.0, 22.0, 28.0, 36.0, 45.0, 56.0, 71.0],
        'III': [0.400, 0.420, 0.450, 0.480, 0.500, 0.53, 1.10, 1.20, 1.25, 1.30, 1.40, 1.50, 1.60, 2.00, 2.50, 3.20, 4.0, 5.0, 6.3, 8.0, 10.0, 12.5, 16.0, 20.0, 25.0, 32.0, 40.0, 50.0, 63.0, 80.0, 100.0],
    },
    3: {
        'I':   [1.000, 1.050, 1.100, 1.200, 1.250, 1.30, 1.40, 1.50, 1.60, 1.70, 1.80, 1.90, 2.00, 2.50, 3.20, 4.00, 5.0, 6.3, 8.0, 10.0, 12.5, 16.0, 20.0, 25.0, 32.0, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0],
        'II':  [1.000, 1.050, 1.100, 1.200, 1.250, 1.30, 1.60, 1.70, 1.80, 2.00, 2.00, 2.10, 2.20, 2.80, 3.60, 4.50, 5.6, 7.1, 9.0, 11.0, 14.0, 18.0, 22.0, 28.0, 36.0, 45.0, 56.0, 71.0, 90.0, 110.0, 140.0],
        'III': [1.000, 1.050, 1.100, 1.200, 1.250, 1.30, 1.80, 1.90, 2.00, 2.10, 2.20, 2.40, 2.50, 3.20, 4.00, 5.00, 6.3, 8.0, 10.0, 12.5, 16.0, 20.0, 25.0, 32.0, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0],
    },
}


def _interp(voltage_v, volts, vals):
    if voltage_v <= volts[0]:
        return vals[0], None
    if voltage_v >= volts[-1]:
        return vals[-1], (f"voltage {voltage_v:g} V exceeds the tabulated range "
                           f"(max {volts[-1]:g} V) — value shown is the table's maximum "
                           f"entry, NOT extrapolated. Consult the standard directly.")
    for i in range(len(volts) - 1):
        v0, v1 = volts[i], volts[i + 1]
        if v0 <= voltage_v <= v1:
            c0, c1 = vals[i], vals[i + 1]
            frac = (voltage_v - v0) / (v1 - v0)
            return c0 + frac * (c1 - c0), None
    return vals[-1], None


def iec_f5_creepage_mm(voltage_v, pollution_degree, material_group, use_pwb):
    """
    Linear interpolation of IEC 60664-1:2020 Table F.5. Returns
    (creepage_mm, note_or_None).
      voltage_v         working voltage, RMS a.c. or d.c.
      pollution_degree  1, 2, or 3
      material_group    'I', 'II', or 'III' (ignored if use_pwb)
      use_pwb           True to use the "printed wiring material" columns
                         instead of the general material-group columns
    """
    if use_pwb:
        if pollution_degree not in (1, 2):
            return None, ("Printed wiring material values are only tabulated for "
                           "pollution degree 1 and 2 — select one of those, or "
                           "switch off the PWB option to use the general table.")
        val, note = _interp(voltage_v, IEC_F5_PWB_VOLTAGES, IEC_F5_PWB_TABLE[pollution_degree])
        return val, note

    val, note = _interp(voltage_v, IEC_F5_GENERAL_VOLTAGES,
                         IEC_F5_GENERAL_TABLE[pollution_degree][material_group])
    if pollution_degree == 3 and material_group == 'III' and voltage_v > 630:
        iiib_note = ("IEC 60664-1 footnote (b): material group IIIb is not recommended "
                     "for pollution degree 3 above 630 V. If your board is genuinely "
                     "group IIIa rather than IIIb, this tabulated value is more "
                     "conservative than necessary — the standard doesn't separately "
                     "tabulate IIIa for PD3, so verify manually.")
        note = f"{note} {iiib_note}" if note else iiib_note
    return val, note


try:
    for active_plugin in list(pcbnew.ActionPlugin._plugins):
        if "KiCad 10 True Surface Creepage Solver" in active_plugin.name:
            pcbnew.ActionPlugin._plugins.remove(active_plugin)
except Exception: pass

TrueGeometricCreepageEngine().register()