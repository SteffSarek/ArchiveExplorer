import os, json, logging, webbrowser, traceback, re, math, subprocess, hashlib, pathlib
import numpy as np
from PIL import Image
from astropy.io import fits
from astropy.wcs import WCS
from utils import generate_pretty_name, get_fits_stats


class AtlasExporter:
    def __init__(self, logic_instance):
        self.logic = logic_instance
        self.wcs_cache_file = os.path.join(self.logic.app_path, "atlas_wcs_cache.json")
        self.wcs_cache = self._load_wcs_cache()

    def _load_wcs_cache(self):
        if os.path.exists(self.wcs_cache_file):
            try:
                with open(self.wcs_cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_wcs_cache(self):
        try:
            with open(self.wcs_cache_file, "w", encoding="utf-8") as f:
                json.dump(self.wcs_cache, f, indent=4)
        except Exception as e:
            logging.warning(f"Cache Save Error: {e}")

    def _extract_geometry_from_wcs_header(self, header, width_px, height_px, is_flipped_y=True):
        try:
            hdr = header.copy()
            hdr["NAXIS"] = 2
            hdr["NAXIS1"] = int(width_px)
            hdr["NAXIS2"] = int(height_px)
            # SIP-Verzerrungspolynome bei extremen Widefields auf lineares TAN vereinfachen:
            if "CTYPE1" in hdr and "-SIP" in str(hdr["CTYPE1"]):
                hdr["CTYPE1"] = str(hdr["CTYPE1"]).replace("-SIP", "")
            if "CTYPE2" in hdr and "-SIP" in str(hdr["CTYPE2"]):
                hdr["CTYPE2"] = str(hdr["CTYPE2"]).replace("-SIP", "")

            w = WCS(hdr, naxis=2, relax=True)
            if not w.has_celestial:
                return None

            c_center = w.pixel_to_world(width_px * 0.5, height_px * 0.5)
            # Messung bei 25% und 75% der Bildbreite (* 2), um Weitwinkel-Randverzerrungen zu vermeiden
            c_left = w.pixel_to_world(width_px * 0.25, height_px * 0.5)
            c_right = w.pixel_to_world(width_px * 0.75, height_px * 0.5)
            y_top = (height_px * 0.75) if is_flipped_y else (height_px * 0.25)
            c_top = w.pixel_to_world(width_px * 0.5, y_top)

            ra_c, dec_c = float(c_center.ra.deg), float(c_center.dec.deg)
            fov_w = float(c_left.separation(c_right).deg) * 2.0

            # Erlaubt auch extreme Widefield-Aufnahmen bis 150°!
            if math.isnan(ra_c) or math.isnan(dec_c) or math.isnan(fov_w) or not (0.05 <= fov_w <= 150.0):
                return None

            pa_top = float(c_center.position_angle(c_top).deg)
            pa_right = float(c_center.position_angle(c_right).deg)
            flipped = (((pa_right - pa_top + 360.0) % 360.0) < 180.0)
            rot_deg = ((-pa_top + 180.0) % 360.0) - 180.0

            return {
                "ra": round(ra_c, 5), "dec": round(dec_c, 5),
                "fov": round(fov_w, 4), "rot": round(rot_deg, 2),
                "flipped": bool(flipped), "solved": True, "astap_solved": True
            }
        except Exception as e:
            logging.warning(f"WCS Geometrie-Fehler: {e}")
            return None

    def _read_solver_header(self, wcs_path, ini_path):
        # 1. .wcs-Datei prüfen (erst als FITS-Header wie bei SASpro/Astrometry, dann als ASTAP-Text)
        if wcs_path and os.path.exists(wcs_path):
            clean_head = fits.Header()
            try:
                with fits.open(wcs_path, ignore_missing_end=True) as h:
                    for k, v in h[0].header.items():
                        if k not in ['COMMENT', 'HISTORY', 'SIMPLE', 'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'NAXIS3'] and not k.startswith('WARN'):
                            clean_head[k] = v
            except Exception:
                pass
            if len(clean_head) == 0:
                with open(wcs_path, "r", encoding="ascii", errors="ignore") as f_wcs:
                    for line in f_wcs:
                        l = line.strip("\r\n")
                        if "=" not in l or l.startswith(("COMMENT", "WARNING", "HISTORY", "SIMPLE", "BITPIX", "NAXIS")):
                            continue
                        try:
                            clean_head.append(fits.Card.fromstring(l.ljust(80)[:80]))
                        except Exception:
                            pass
            if len(clean_head) > 0:
                return clean_head

        # 2. Fallback: .ini-Format (ASTAP & N.I.N.A.-Wrapper)
        if ini_path and os.path.exists(ini_path):
            kv = {}
            with open(ini_path, "r", encoding="utf-8", errors="ignore") as f_ini:
                for line in f_ini:
                    line = line.strip()
                    if not line or "=" not in line or line.startswith(("[", "#", ";")):
                        continue
                    k, v = line.split("=", 1)
                    kv[k.strip().upper()] = v.strip()

            if kv.get("PLTSOLVD", "").upper() != "T":
                return None

            clean_head = fits.Header()
            float_keys = {
                "CRPIX1", "CRPIX2", "CRVAL1", "CRVAL2",
                "CDELT1", "CDELT2", "CROTA1", "CROTA2",
                "CD1_1", "CD1_2", "CD2_1", "CD2_2", "EQUINOX"
            }
            str_keys = {"CTYPE1", "CTYPE2", "CUNIT1", "CUNIT2", "RADESYS"}

            for k in float_keys:
                if k in kv:
                    try:
                        clean_head[k] = float(kv[k].replace(",", "."))
                    except ValueError:
                        pass
            for k in str_keys:
                if k in kv:
                    clean_head[k] = kv[k].strip().strip("'\"")

            if "CTYPE1" not in clean_head:
                clean_head["CTYPE1"] = "RA---TAN"
            if "CTYPE2" not in clean_head:
                clean_head["CTYPE2"] = "DEC--TAN"

            if "CRVAL1" in clean_head and "CRVAL2" in clean_head:
                return clean_head

        return None

    def _read_ini_error(self, ini_paths):
        for p in ini_paths:
            if p and os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read().strip()
                    if content:
                        for line in content.splitlines():
                            if line.upper().startswith(("ERROR=", "WARNING=", "PLTSOLVD=")):
                                return line.strip()
                except Exception:
                    pass
        return ""

    def _resolve_saspro_cli(self, exe_path):
        if not exe_path:
            return None
        name = os.path.basename(exe_path).lower()
        if "setiastrosuitepro" in name:
            return exe_path
        if "sasprosolver" in name:
            candidate = os.path.join(os.path.dirname(exe_path), "SetiAstroSuitePro.exe")
            if os.path.exists(candidate):
                return candidate
        return None

    def _get_solver_chain(self):
        astap_raw = self.logic.get_astap_path() if hasattr(self.logic, "get_astap_path") else ""
        saspro_raw = self.logic.get_saspro_path() if hasattr(self.logic, "get_saspro_path") else ""
        mode = self.logic.get_solver_mode() if hasattr(self.logic, "get_solver_mode") else "Auto (ASTAP → SASpro)"

        astap_exe = astap_raw if (astap_raw and os.path.exists(astap_raw) and not self._resolve_saspro_cli(astap_raw)) else None
        saspro_cli = self._resolve_saspro_cli(saspro_raw) if (saspro_raw and os.path.exists(saspro_raw)) else None

        # Abwärtskompatibel, falls SetiAstro noch im alten ASTAP-Feld steht:
        if not saspro_cli and astap_raw and os.path.exists(astap_raw):
            saspro_cli = self._resolve_saspro_cli(astap_raw)

        if "Nur ASTAP" in mode:
            return [("astap", astap_exe)] if astap_exe else []
        if "Nur SetiAstro" in mode:
            return [("saspro", saspro_cli)] if saspro_cli else []
        if "SASpro → ASTAP" in mode or "SASpro -> ASTAP" in mode:
            chain = []
            if saspro_cli: chain.append(("saspro", saspro_cli))
            if astap_exe: chain.append(("astap", astap_exe))
            return chain

        chain = []
        if astap_exe: chain.append(("astap", astap_exe))
        if saspro_cli: chain.append(("saspro", saspro_cli))
        return chain

    def _run_saspro_solve(self, saspro_cli, img_path, temp_fits, temp_wcs, temp_ini, ra_hint, dec_hint, intensive=False, log_cb=None):
        ext = os.path.splitext(img_path)[1].lower()
        with Image.open(img_path) as im:
            w_px, h_px = im.size
            if ext == ".webp":
                arr_16 = np.array(im.convert("L"), dtype=np.uint16) * 257
                fits.PrimaryHDU(data=np.flip(arr_16, axis=0)).writeto(temp_fits, overwrite=True)
                sas_input, flipped_y = temp_fits, True
            else:
                sas_input, flipped_y = img_path, (ext in [".fits", ".fit", ".fts"])

        modes = [("Gaia Seeded", True), ("Gaia Blind", False)] if (flipped_y and ra_hint is not None and dec_hint is not None) else [("Gaia Blind", False)]
        timeout_s = 120 if intensive else 60
        last_err = ""

        for mode_lbl, use_seed in modes:
            if intensive and log_cb:
                log_cb(f"🔍 Starte SetiAstro {mode_lbl} ({os.path.basename(img_path)})...")
            for tmp_out in (temp_wcs, temp_ini):
                if os.path.exists(tmp_out):
                    try: os.remove(tmp_out)
                    except: pass

            cmd = [saspro_cli, "solve", "-i", sas_input, "--solver", "gaia", "--wcs-only", "--wcs-path", temp_wcs, "--json"]
            if use_seed:
                cmd.extend(["--ra", f"{float(ra_hint):.6f}", "--dec", f"{float(dec_hint):.6f}"])
            else:
                cmd.append("--force-blind")

            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
                clean_head = self._read_solver_header(temp_wcs, temp_ini)
                if clean_head:
                    geom = self._extract_geometry_from_wcs_header(clean_head, w_px, h_px, is_flipped_y=flipped_y)
                    if geom:
                        return geom, mode_lbl, ""
                raw_out = (res.stdout or res.stderr or "").strip()
                json_start = raw_out.find("{")
                msg_out = raw_out[json_start:] if json_start != -1 else raw_out
                try:
                    jdata = json.loads(msg_out)
                    msg_out = jdata.get("error") or jdata.get("reason") or jdata.get("message") or json.dumps(jdata)
                except Exception:
                    pass
                last_err = msg_out[:250]
                if intensive and log_cb and last_err:
                    log_cb(f"ℹ️ {mode_lbl} (Code {res.returncode}): {last_err}")
            except subprocess.TimeoutExpired:
                last_err = f"Timeout nach {timeout_s}s"
                if intensive and log_cb:
                    log_cb(f"⏱️ {mode_lbl} {last_err}.")
            except Exception as ex:
                last_err = str(ex)
                if intensive and log_cb:
                    log_cb(f"⚠️ {mode_lbl} Fehler: {ex}")

        return None, "SetiAstro Gaia", last_err

    def _run_astap_solve(self, astap_exe, img_path, temp_fits, temp_wcs, temp_ini, ra_hint, dec_hint, default_fov=1.1, intensive=False, log_cb=None):
        if os.path.exists(temp_wcs):
            try: os.remove(temp_wcs)
            except: pass
        with Image.open(img_path) as im:
            im_gray = im.convert("L")
            w_px, h_px = im_gray.size
            if max(w_px, h_px) > 2200:
                scale = 2000.0 / max(w_px, h_px)
                w_px, h_px = int(w_px * scale), int(h_px * scale)
                im_gray = im_gray.resize((w_px, h_px))
            arr_16 = np.array(im_gray, dtype=np.uint16) * 257
        fits.PrimaryHDU(data=np.flip(arr_16, axis=0)).writeto(temp_fits, overwrite=True)

        temp_base = os.path.splitext(temp_fits)[0]
        temp_fits_ini = temp_fits + ".ini"
        fov_list = ["0", "0.75", "1.25", "1.65", "0.50", "0.35", "2.40", "3.80", "6.00", "12.0", "25.0"] if intensive else [f"{default_fov:.2f}", "0"]
        timeout_s = 30 if intensive else 15
        last_err = ""

        for fov_try in fov_list:
            if intensive and log_cb:
                log_cb(f"🔍 Starte ASTAP ({os.path.basename(img_path)}, FOV: {fov_try if fov_try != '0' else 'Auto'})...")
            for tmp_out in (temp_wcs, temp_ini, temp_fits_ini):
                if os.path.exists(tmp_out):
                    try: os.remove(tmp_out)
                    except: pass

            cmd = [astap_exe, "-f", temp_fits, "-o", temp_base]
            if fov_try != "0":
                cmd.extend(["-fov", fov_try])
            if ra_hint is not None and dec_hint is not None:
                r_deg = "60" if intensive else "30"
                cmd.extend(["-ra", f"{float(ra_hint)/15.0:.4f}", "-spd", f"{float(dec_hint)+90.0:.4f}", "-r", r_deg])
            else:
                cmd.extend(["-r", "180"])

            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
                clean_head = self._read_solver_header(temp_wcs, temp_ini) or self._read_solver_header(None, temp_fits_ini)
                if clean_head:
                    geom = self._extract_geometry_from_wcs_header(clean_head, w_px, h_px, is_flipped_y=True)
                    if geom:
                        return geom, "ASTAP", ""
                last_err = self._read_ini_error([temp_ini, temp_fits_ini]) or (res.stderr or res.stdout or "").strip()[:180]
                if intensive and log_cb and last_err:
                    log_cb(f"ℹ️ ASTAP (Code {res.returncode}): {last_err}")
            except subprocess.TimeoutExpired:
                last_err = f"Timeout nach {timeout_s}s"
                if intensive and log_cb:
                    log_cb(f"⏱️ ASTAP {last_err}.")
            except Exception as ex:
                last_err = str(ex)
                if intensive and log_cb:
                    log_cb(f"⚠️ ASTAP Fehler: {ex}")

        return None, "ASTAP", last_err

    def _solve_or_get_image_wcs(self, img_path, fits_sample, ra_hint, dec_hint, default_fov, run_astap=False, force_retry=False, log_cb=None, obj_label=""):
        norm_key = os.path.normpath(img_path).lower()
        keep_full = self.is_full_res(img_path)
        keep_excl = not self.is_included_in_atlas(img_path)
        try:
            mtime = int(os.path.getmtime(img_path))
        except Exception:
            mtime = 0

        cached = self.wcs_cache.get(norm_key)
        if cached and cached.get("mtime") == mtime and not force_retry:
            if cached.get("astap_solved") or not run_astap or cached.get("astap_tried"):
                return cached

        solver_chain = self._get_solver_chain()
        is_starless = "starless" in os.path.basename(img_path).lower()
        can_use_solver = run_astap and (not is_starless) and len(solver_chain) > 0

        if can_use_solver:
            temp_dir = os.path.join(self.logic.app_path, "temp")
            os.makedirs(temp_dir, exist_ok=True)
            temp_fits = os.path.join(temp_dir, "temp_atlas_solve.fits")
            temp_wcs = os.path.join(temp_dir, "temp_atlas_solve.wcs")
            temp_ini = os.path.join(temp_dir, "temp_atlas_solve.ini")

            for s_kind, s_exe in solver_chain:
                try:
                    if s_kind == "saspro":
                        geom, used_lbl, _ = self._run_saspro_solve(
                            s_exe, img_path, temp_fits, temp_wcs, temp_ini,
                            ra_hint, dec_hint, intensive=False, log_cb=log_cb
                        )
                    else:
                        geom, used_lbl, _ = self._run_astap_solve(
                            s_exe, img_path, temp_fits, temp_wcs, temp_ini,
                            ra_hint, dec_hint, default_fov=default_fov, intensive=False, log_cb=log_cb
                        )

                    if geom:
                        geom["mtime"] = mtime
                        geom["astap_tried"] = True
                        geom["full_res"] = self.is_full_res(img_path)
                        geom["exclude_atlas"] = not self.is_included_in_atlas(img_path)
                        self.wcs_cache[norm_key] = geom
                        self._save_wcs_cache()
                        if log_cb:
                            log_cb(f"✅ {used_lbl} gelöst: {obj_label} -> FOV {geom['fov']:.2f}°, Rot {geom['rot']:+.1f}°")
                        return geom
                except Exception as e:
                    logging.debug(f"Solver ({s_kind}) Fehler {img_path}: {e}")

            if log_cb:
                log_cb(f"⚠️ Nicht gelöst: {obj_label} ({os.path.basename(img_path)})")

        fallback = {
            "ra": round(float(ra_hint), 5), "dec": round(float(dec_hint), 5),
            "fov": round(float(default_fov), 4), "rot": 0.0, "flipped": False,
            "solved": False, "astap_solved": False,
            "astap_tried": bool(can_use_solver or is_starless), "mtime": mtime,
            "full_res": keep_full, "exclude_atlas": keep_excl
        }
        self.wcs_cache[norm_key] = fallback
        if can_use_solver:
            self._save_wcs_cache()
        return fallback

    def get_cached_wcs(self, img_path):
        if not img_path or not os.path.exists(img_path):
            return None
        norm_key = os.path.normpath(img_path).lower()
        try:
            mtime = int(os.path.getmtime(img_path))
        except Exception:
            mtime = 0
        cached = self.wcs_cache.get(norm_key)
        if cached and cached.get("mtime") == mtime and cached.get("astap_solved"):
            return cached
        return None

    def is_full_res(self, img_path):
        if not img_path:
            return False
        norm_key = os.path.normpath(img_path).lower()
        return bool(self.wcs_cache.get(norm_key, {}).get("full_res", False))

    def set_full_res(self, img_path, state):
        if not img_path:
            return False
        norm_key = os.path.normpath(img_path).lower()
        if norm_key not in self.wcs_cache:
            try:
                mtime = int(os.path.getmtime(img_path))
            except Exception:
                mtime = 0
            self.wcs_cache[norm_key] = {"mtime": mtime}
        self.wcs_cache[norm_key]["full_res"] = bool(state)
        self._save_wcs_cache()
        return bool(state)

    def toggle_full_res(self, img_path):
        return self.set_full_res(img_path, not self.is_full_res(img_path))

    def is_included_in_atlas(self, img_path):
        if not img_path:
            return True
        norm_key = os.path.normpath(img_path).lower()
        entry = self.wcs_cache.get(norm_key, {})
        if "exclude_atlas" in entry:
            return not bool(entry["exclude_atlas"])
        ext = os.path.splitext(img_path)[1].lower()
        return ext in [".jpg", ".jpeg"]

    def set_included_in_atlas(self, img_path, include_state):
        if not img_path:
            return True
        norm_key = os.path.normpath(img_path).lower()
        if norm_key not in self.wcs_cache:
            try:
                mtime = int(os.path.getmtime(img_path))
            except Exception:
                mtime = 0
            self.wcs_cache[norm_key] = {"mtime": mtime}
        self.wcs_cache[norm_key]["exclude_atlas"] = not bool(include_state)
        self._save_wcs_cache()
        return bool(include_state)

    def solve_single_image_targeted(self, img_path, ra_hint=None, dec_hint=None, use_cloud=False, log_cb=None):
        """Löst gezielt ein einzelnes Bild (lokale Solver-Kette oder nova.astrometry.net Cloud) und speichert es im Atlas-Cache."""
        import ssl
        import urllib.request

        if not img_path or not os.path.exists(img_path):
            return False, "Bilddatei nicht gefunden."

        norm_key = os.path.normpath(img_path).lower()
        keep_full_res = self.is_full_res(img_path)
        keep_excl = not self.is_included_in_atlas(img_path)
        try:
            mtime = int(os.path.getmtime(img_path))
        except Exception:
            mtime = 0

        temp_dir = os.path.join(self.logic.app_path, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        temp_fits = os.path.join(temp_dir, "temp_targeted_solve.fits")
        temp_wcs = os.path.join(temp_dir, "temp_targeted_solve.wcs")
        temp_ini = os.path.join(temp_dir, "temp_targeted_solve.ini")

        # --- MODUS A: INTENSIVER LOKAL-SOLVE (Kette aus Einstellungen) ---
        if not use_cloud:
            solver_chain = self._get_solver_chain()
            if not solver_chain:
                return False, "Kein lokaler Solver (ASTAP / SetiAstro) in den Einstellungen hinterlegt!"

            for s_kind, s_exe in solver_chain:
                try:
                    if s_kind == "saspro":
                        geom, used_lbl, _ = self._run_saspro_solve(
                            s_exe, img_path, temp_fits, temp_wcs, temp_ini,
                            ra_hint, dec_hint, intensive=True, log_cb=log_cb
                        )
                    else:
                        geom, used_lbl, _ = self._run_astap_solve(
                            s_exe, img_path, temp_fits, temp_wcs, temp_ini,
                            ra_hint, dec_hint, intensive=True, log_cb=log_cb
                        )

                    if geom:
                        geom["mtime"] = mtime
                        geom["astap_tried"] = True
                        geom["full_res"] = keep_full_res
                        geom["exclude_atlas"] = keep_excl
                        self.wcs_cache[norm_key] = geom
                        self._save_wcs_cache()
                        msg = f"✅ {used_lbl} hat {os.path.basename(img_path)} gelöst! (FOV: {geom['fov']:.2f}°, Drehung: {geom['rot']:+.1f}°)"
                        if log_cb:
                            log_cb(msg)
                        return True, msg
                except Exception as ex:
                    if log_cb:
                        log_cb(f"⚠️ Solver-Fehler ({s_kind}): {ex}")

            if log_cb:
                log_cb("⚠️ Lokaler Solver konnte das Bild nicht lösen. Tipp: Cloud Solve testen!")
            return False, "Lokaler Solver konnte das Bild nicht lösen. Nutze den Cloud-Solve!"

        # --- MODUS B: ASTROMETRY.NET CLOUD SOLVE ---
        api_key = self.logic.get_astrometry_key()
        if not api_key:
            return False, "Kein Astrometry.net API-Key in den Einstellungen hinterlegt!"

        try:
            if os.path.exists(temp_wcs):
                os.remove(temp_wcs)
            with Image.open(img_path) as im:
                im_gray = im.convert("L")
                w_px, h_px = im_gray.size
                if max(w_px, h_px) > 1800:
                    scale = 1800.0 / max(w_px, h_px)
                    w_px, h_px = int(w_px * scale), int(h_px * scale)
                    im_gray = im_gray.resize((w_px, h_px))
                arr_16 = np.array(im_gray, dtype=np.uint16) * 257
            fits.PrimaryHDU(data=np.flip(arr_16, axis=0)).writeto(temp_fits, overwrite=True)
        except Exception as e:
            return False, f"Fehler beim Vorbereiten des Bildes: {e}"

        try:
            from logic_astrometry import AstrometryClient
            client = AstrometryClient(api_key)
            if log_cb: log_cb("☁️ Verbinde mit nova.astrometry.net...")
            ok, msg = client.login()
            if not ok:
                return False, f"Cloud-Login fehlgeschlagen: {msg}"

            if log_cb: log_cb(f"☁️ Lade {os.path.basename(img_path)} hoch (bitte warten)...")
            sub_id, msg = client.upload_file(temp_fits)
            if not sub_id:
                return False, f"Cloud-Upload fehlgeschlagen: {msg}"

            if log_cb: log_cb(f"☁️ Upload erfolgreich (Sub-ID: {sub_id}). Warte auf Job-Start...")
            job_id = client.wait_for_job(sub_id, timeout=300)
            if not job_id:
                return False, "Cloud-Timeout: Job wurde nicht rechtzeitig gestartet."

            if log_cb: log_cb(f"☁️ Astrometry.net rechnet (Job-ID: {job_id})... Das dauert ca. 20-60 Sekunden...")
            is_solved = client.wait_for_calibration(job_id, timeout=600)
            if not is_solved:
                return False, "Astrometry.net konnte das Bild leider nicht lösen."

            if log_cb: log_cb(f"☁️ Lösung gefunden! Lade WCS-Kalibrierungsdaten für Job {job_id}...")
            wcs_url = f"https://nova.astrometry.net/wcs_file/{job_id}"
            ctx_ssl = ssl._create_unverified_context()
            req = urllib.request.Request(wcs_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20, context=ctx_ssl) as resp:
                with open(temp_wcs, "wb") as f_w:
                    f_w.write(resp.read())

            clean_head = fits.Header()
            with fits.open(temp_wcs, ignore_missing_end=True) as h:
                for k, v in h[0].header.items():
                    if k not in ['COMMENT', 'HISTORY', 'SIMPLE', 'BITPIX', 'NAXIS', 'NAXIS1', 'NAXIS2', 'NAXIS3'] and not k.startswith('WARN'):
                        clean_head[k] = v

            geom = self._extract_geometry_from_wcs_header(clean_head, w_px, h_px, is_flipped_y=True)
            if geom:
                geom["mtime"] = mtime
                geom["astap_tried"] = True
                geom["full_res"] = keep_full_res
                geom["exclude_atlas"] = keep_excl
                self.wcs_cache[norm_key] = geom
                self._save_wcs_cache()
                msg = f"☁️✅ Cloud-Solve erfolgreich! {os.path.basename(img_path)} exakt im Atlas gespeichert (FOV: {geom['fov']:.2f}°, Rot: {geom['rot']:+.1f}°)"
                if log_cb: log_cb(msg)
                return True, msg
            else:
                return False, "WCS-Datei aus der Cloud konnte nicht ausgewertet werden."
        except Exception as e:
            return False, f"Cloud-Fehler: {e}"

    def generate_atlas(self, callback=None, for_web=False, use_astap=False, force_retry=False, retry_failed=False, log_cb=None):
        try:
            export_dir = self.logic.get_export_path()
            if not export_dir or not os.path.isdir(export_dir):
                export_dir = self.logic.app_path

            if for_web:
                web_base_dir = os.path.join(export_dir, "Web_Atlas")
                web_img_dir = os.path.join(web_base_dir, "atlas_images")
                os.makedirs(web_img_dir, exist_ok=True)
                output_html_path = os.path.join(web_base_dir, "index.html")
            else:
                output_html_path = os.path.join(export_dir, "Mein_Himmelsatlas.html")

            if force_retry:
                for entry in self.wcs_cache.values():
                    entry["astap_solved"] = False
                    entry["astap_tried"] = False
                self._save_wcs_cache()
            elif retry_failed:
                for entry in self.wcs_cache.values():
                    if not entry.get("astap_solved"):
                        entry["astap_tried"] = False
                self._save_wcs_cache()

            atlas_objects_dict = {}
            date_pattern = re.compile(r'(202\d)[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])')
            total_items = len(self.logic.index)
            processed_count = 0

            for key, data in self.logic.index.items():
                processed_count += 1
                pretty_name = generate_pretty_name(data["original_name"], self.logic.caldwell_map, self.logic.reverse_caldwell_map, self.logic.common_names)
                pretty_name = re.sub(r'(?i)\s*(?:seestar\s*s\d+\s*(?:pro)?|dwarf\s*\d+|s30\s*(?:pro)?)\s*\d*$', '', pretty_name).strip()
                if pretty_name.lower().strip() in ["bilder", "sonstiges", "export", "test", "misc", "allgemein", "mond", "moon", "jupiter", "saturn", "mars", "venus", "sonne", "sun"]:
                    continue
                if data.get("path") and "xxx_" in data.get("path").lower():
                    continue

                obj_data = self.logic.get_object_data(key, exact_match=True)
                ra, dec = obj_data.get("ra_decimal"), obj_data.get("dec_decimal")
                clean_k = self.logic._get_clean_key(pretty_name)
                if (ra is None or dec is None) and clean_k in self.logic.coord_cache:
                    ra, dec = self.logic.coord_cache[clean_k]["ra"], self.logic.coord_cache[clean_k]["dec"]

                if ra is not None and dec is not None:
                    obj_dates = []
                    if data.get("path") and os.path.isdir(data["path"]):
                        try:
                            for file in os.listdir(data["path"]):
                                m = date_pattern.search(file)
                                if m:
                                    obj_dates.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
                        except Exception:
                            pass
                        if not obj_dates:
                            try:
                                _, dt_str, _ = get_fits_stats(data["path"])
                                if dt_str and dt_str != "---":
                                    parts = dt_str.split('.')
                                    if len(parts) == 3:
                                        obj_dates.append(f"{parts[2]}-{parts[1]}-{parts[0]}")
                            except Exception:
                                pass

                    imgs = obj_data.get("images", []) or ([obj_data.get("image")] if obj_data.get("image") else [])
                    tel_type = data.get("telescope", "other")
                    default_fov = 0.75 if tel_type == "seestar" else (1.60 if tel_type == "s30" else (1.80 if tel_type == "dwarf" else 1.10))

                    clean_imgs, image_meta_list = [], []
                    for img_idx, p in enumerate(imgs):
                        if not self.is_included_in_atlas(p):
                            continue
                        if os.path.exists(p) and os.path.splitext(p)[1].lower() in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                            want_full_res = self.is_full_res(p)
                            if for_web:
                                safe_obj = re.sub(r'[^a-zA-Z0-9_-]', '_', clean_k)
                                orig_base = re.sub(r'[^a-zA-Z0-9_-]', '_', os.path.splitext(os.path.basename(p))[0])
                                path_hash = hashlib.md5(os.path.normpath(p).lower().encode("utf-8")).hexdigest()[:6]
                                web_filename = f"{safe_obj}_{path_hash}_{orig_base}.jpg"
                                dest_img_path = os.path.join(web_img_dir, web_filename)
                                if not os.path.exists(dest_img_path):
                                    try:
                                        with Image.open(p) as pil_img:
                                            rgb_img = pil_img.convert("RGB")
                                            rgb_img.thumbnail((1400, 1400))
                                            rgb_img.save(dest_img_path, "JPEG", quality=85, optimize=True)
                                    except Exception:
                                        continue
                                img_url = f"atlas_images/{web_filename}"

                                if want_full_res:
                                    full_filename = f"{safe_obj}_{path_hash}_{orig_base}_FULL.jpg"
                                    dest_full_path = os.path.join(web_img_dir, full_filename)
                                    if not os.path.exists(dest_full_path):
                                        try:
                                            with Image.open(p) as pil_full:
                                                rgb_full = pil_full.convert("RGB")
                                                rgb_full.save(dest_full_path, "JPEG", quality=93, optimize=True)
                                        except Exception:
                                            pass
                                    full_url = f"atlas_images/{full_filename}"
                                else:
                                    full_url = img_url
                            else:
                                img_url = pathlib.Path(os.path.abspath(p)).as_uri()
                                full_url = img_url

                            wcs_info = self._solve_or_get_image_wcs(
                                p, data.get("fits_sample"), ra, dec, default_fov,
                                run_astap=use_astap, force_retry=force_retry,
                                log_cb=log_cb, obj_label=f"[{processed_count}/{total_items}] {pretty_name}"
                            )
                            clean_imgs.append(img_url)
                            image_meta_list.append({
                                "url": img_url, "full_url": full_url, "full_res": want_full_res,
                                "ra": wcs_info["ra"], "dec": wcs_info["dec"],
                                "fov": wcs_info["fov"], "rot": wcs_info["rot"],
                                "flipped": wcs_info.get("flipped", False), "solved": wcs_info.get("astap_solved", False)
                            })
                    if not clean_imgs:
                        continue

                    otype = self.logic.determine_object_type(pretty_name, pretty_name)
                    fm = next((m for m in image_meta_list if m["solved"]), image_meta_list[0])
                    if pretty_name not in atlas_objects_dict:
                        atlas_objects_dict[pretty_name] = {
                            "name": pretty_name, "ra": fm["ra"], "dec": fm["dec"], "fov": fm["fov"],
                            "rot": fm["rot"], "flipped": fm["flipped"], "solved": fm["solved"],
                            "type": otype, "images": clean_imgs, "image_meta": image_meta_list,
                            "dates": obj_dates, "wiki_url": obj_data.get("wiki_url", "")
                        }
                    else:
                        ex = atlas_objects_dict[pretty_name]
                        for idx_m, img_u in enumerate(clean_imgs):
                            if img_u not in ex["images"]:
                                ex["images"].append(img_u)
                                ex["image_meta"].append(image_meta_list[idx_m])
                                if image_meta_list[idx_m]["solved"] and not ex["solved"]:
                                    ex.update({
                                        "ra": image_meta_list[idx_m]["ra"], "dec": image_meta_list[idx_m]["dec"],
                                        "fov": image_meta_list[idx_m]["fov"], "rot": image_meta_list[idx_m]["rot"],
                                        "flipped": image_meta_list[idx_m]["flipped"], "solved": True
                                    })
                        ex["dates"].extend(obj_dates)

            self._save_wcs_cache()
            solved_cnt = sum(1 for o in atlas_objects_dict.values() if o.get("solved"))
            total_cnt = len(atlas_objects_dict)

            for obj in atlas_objects_dict.values():
                if obj["dates"]:
                    ud = sorted(list(set(obj["dates"])))
                    p1, p2 = ud[0].split('-'), ud[-1].split('-')
                    obj["first_obs"], obj["last_obs"] = f"{p1[2]}.{p1[1]}.{p1[0]}", f"{p2[2]}.{p2[1]}.{p2[0]}"
                else:
                    obj["first_obs"] = obj["last_obs"] = "---"
                del obj["dates"]

            atlas_objects = list(atlas_objects_dict.values())
            atlas_objects.sort(key=lambda o: o["name"].lower())
            if not atlas_objects:
                if callback:
                    callback(False, "Keine aufgelösten Bilder gefunden!")
                return False

            back_btn_text = "🏠 Zurück zu Astro-Nerd.info" if for_web else "✖ Atlas schließen"

            html_content = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Astro-Nerd.info | Interaktiver 3D-Himmelsatlas</title>
<style>
body,html{{margin:0;padding:0;width:100%;height:100%;background:#05080f;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden;user-select:none}}
#survey-picker-box{{position:absolute;bottom:52px;left:14px;z-index:1500;background:rgba(11,17,27,0.92);border:1px solid rgba(56,189,248,0.45);border-radius:8px;padding:6px 12px;font-size:12px;color:#e2e8f0;backdrop-filter:blur(6px)}}
#survey-picker{{background:#1e293b;color:#38bdf8;border:1px solid #334155;border-radius:5px;padding:3px 8px;font-size:12px;font-weight:600;margin-left:6px;cursor:pointer;outline:none}}
#sky-canvas{{position:absolute;top:0;left:0;width:100vw;height:100vh;cursor:grab;z-index:100}}
#sky-canvas:active{{cursor:grabbing}}
#top-bar{{position:absolute;top:14px;left:50%;transform:translateX(-50%);z-index:1000;display:flex;align-items:center;gap:14px;background:rgba(11,17,27,0.88);padding:8px 18px;border-radius:10px;border:1px solid rgba(56,189,248,0.35);box-shadow:0 6px 20px rgba(0,0,0,0.7);backdrop-filter:blur(8px)}}
#btn-home{{background:rgba(56,189,248,0.15);border:1px solid rgba(56,189,248,0.5);color:#38bdf8;border-radius:7px;padding:6px 12px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}}
#btn-home:hover{{background:#38bdf8;color:#000}}
#atlas-title h1{{margin:0;font-size:16px;color:#38bdf8;font-weight:700;white-space:nowrap}}
#atlas-title span{{font-size:11px;color:#94a3b8;display:block;white-space:nowrap}}
#search-input{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.18);color:#fff;border-radius:6px;padding:6px 10px;font-size:12px;width:190px;outline:none}}
#overlay-controls{{display:none;position:absolute;top:78px;left:14px;z-index:1500;width:320px;background:rgba(11,17,27,0.90);border:1px solid rgba(255,255,255,0.14);border-radius:10px;padding:14px 16px;box-shadow:0 8px 25px rgba(0,0,0,0.75);backdrop-filter:blur(8px)}}
#ov-title{{font-size:15px;font-weight:700;color:#fff;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
#ov-sub{{font-size:11px;color:#94a3b8;margin-bottom:12px;line-height:1.4}}
.ov-badge-solved{{display:inline-block;background:rgba(16,185,129,0.18);border:1px solid rgba(16,185,129,0.5);color:#34d399;border-radius:5px;padding:2px 7px;font-size:10.5px;font-weight:700;margin-bottom:5px}}
.ov-row{{background:rgba(255,255,255,0.035);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:8px 10px;margin-bottom:8px}}
.ov-label-row{{display:flex;justify-content:space-between;font-size:12px;margin-bottom:5px;color:#cbd5e1}}
.ov-val{{font-weight:bold;color:#38bdf8;font-family:Consolas,monospace}}
.ov-slider-wrap{{display:flex;align-items:center;gap:6px}}
.ov-slider{{flex:1;accent-color:#38bdf8;cursor:pointer;height:5px}}
.ov-mini-btn{{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);color:#ddd;border-radius:5px;padding:2px 7px;font-size:11px;cursor:pointer}}
.ov-mini-btn:hover{{background:#38bdf8;color:#000}}
.ov-actions{{display:flex;gap:6px;margin-top:10px}}
.ov-action-btn{{flex:1;background:rgba(56,189,248,0.15);border:1px solid rgba(56,189,248,0.45);color:#e2e8f0;border-radius:6px;padding:6px 8px;font-size:12px;font-weight:600;cursor:pointer;text-align:center}}
.ov-action-btn:hover{{background:#38bdf8;color:#000}}
.ov-action-btn.close{{flex:0 0 auto;background:rgba(239,68,68,0.18);border-color:rgba(239,68,68,0.5)}}
.ov-action-btn.close:hover{{background:#ef4444;color:#fff}}
#sky-overlay-anchor{{display:none;position:absolute;top:0;left:0;pointer-events:none;z-index:200;transform-origin:center center}}
#sky-overlay-box{{position:relative;display:inline-block}}
#sky-overlay-caption{{position:absolute;top:-22px;left:50%;transform:translateX(-50%);white-space:nowrap;font-size:12px;font-weight:700;color:#f8fafc;text-shadow:0 1px 4px #000,0 0 8px #000}}
#sky-overlay-img{{display:block;width:300px;height:auto;border:1.5px solid rgba(255,255,255,0.92);box-shadow:0 0 30px rgba(0,0,0,0.85);transform-origin:center center}}
#bottom-status{{position:absolute;bottom:14px;left:14px;z-index:1000;background:rgba(11,17,27,0.88);border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:7px 14px;font-size:11.5px;color:#94a3b8;pointer-events:none}}
#bottom-status b{{color:#e2e8f0}}
#bottom-controls{{position:absolute;bottom:14px;right:14px;z-index:1000;display:flex;align-items:center;gap:6px;background:rgba(11,17,27,0.90);border:1px solid rgba(255,255,255,0.14);border-radius:9px;padding:6px 10px;transition:right 0.3s cubic-bezier(0.25,0.8,0.25,1)}}
.ctrl-btn{{background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.14);color:#e2e8f0;border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer;font-weight:600}}
.ctrl-btn:hover{{background:#38bdf8;color:#000}}
.ctrl-btn.active{{background:rgba(56,189,248,0.25);border-color:#38bdf8;color:#38bdf8}}
#fov-readout{{font-family:Consolas,monospace;font-size:12px;color:#cbd5e1;min-width:92px;text-align:center}}
#side-panel{{position:absolute;top:0;right:-430px;width:380px;max-width:90vw;height:100vh;background:rgba(11,15,23,0.96);border-left:1px solid rgba(56,189,248,0.4);box-shadow:-5px 0 25px rgba(0,0,0,0.85);z-index:2000;transition:right 0.3s cubic-bezier(0.25,0.8,0.25,1);overflow-y:auto;padding:22px;box-sizing:border-box}}
#close-panel{{float:right;background:none;border:none;color:#888;font-size:24px;cursor:pointer;margin-top:-5px}}
#panel-title{{margin-top:0;color:#38bdf8;font-size:20px;margin-bottom:5px}}
#panel-coords{{font-size:13px;color:#aaa;margin-bottom:15px;padding-bottom:15px;border-bottom:1px solid #263040;line-height:1.5}}
.atlas-btn{{display:inline-block;padding:5px 10px;margin-right:6px;margin-bottom:5px;border-radius:5px;border:1px solid #38bdf8;color:#38bdf8;text-decoration:none;font-size:12px;font-weight:600;cursor:pointer;background:transparent}}
.atlas-btn:hover{{background:#38bdf8;color:#000}}
.panel-img-container{{margin-bottom:18px;text-align:center;background:rgba(255,255,255,0.03);padding:10px;border-radius:8px;border:1px solid #1e293b}}
.panel-img{{max-width:100%;border-radius:6px;cursor:zoom-in;border:1px solid #334155;margin-bottom:8px}}
#lightbox{{display:none;position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.93);z-index:5000;align-items:center;justify-content:center;flex-direction:column;cursor:zoom-out}}
#lightbox img{{max-width:92vw;max-height:88vh;border-radius:6px;border:1px solid #444}}
#lightbox-caption{{margin-top:12px;color:#38bdf8;font-size:16px;font-weight:bold}}
#click-picker{{display:none;position:fixed;z-index:3000;background:rgba(11,17,27,0.96);border:1px solid rgba(56,189,248,0.55);border-radius:9px;padding:6px;min-width:220px;max-width:320px;max-height:300px;overflow-y:auto;box-shadow:0 10px 30px rgba(0,0,0,0.85);backdrop-filter:blur(8px)}}
.cp-header{{font-size:11px;color:#94a3b8;padding:4px 8px 6px 8px;border-bottom:1px solid #1e293b;margin-bottom:4px;font-weight:600}}
.cp-item{{display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;text-align:left;background:transparent;border:none;color:#e2e8f0;padding:6px 8px;border-radius:6px;cursor:pointer;font-size:12.5px}}
.cp-item:hover{{background:rgba(56,189,248,0.2);color:#38bdf8}}
.cp-meta{{font-size:11px;color:#94a3b8;white-space:nowrap}}
#panel-nearby{{display:none;margin-bottom:15px;padding-bottom:12px;border-bottom:1px solid #263040}}
.nearby-title{{font-size:12px;color:#94a3b8;font-weight:600;margin-bottom:7px}}
.nearby-list{{display:flex;flex-wrap:wrap;gap:6px}}
.nearby-chip{{background:rgba(45,212,191,0.12);border:1px solid rgba(45,212,191,0.4);color:#5eead4;border-radius:6px;padding:4px 8px;font-size:11.5px;cursor:pointer;text-align:left}}
.nearby-chip:hover{{background:#2dd4bf;color:#000}}
#credits-modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.78);z-index:4500;align-items:center;justify-content:center;backdrop-filter:blur(4px)}}
#credits-box{{background:#0b111b;border:1px solid rgba(56,189,248,0.45);border-radius:12px;max-width:520px;width:90vw;padding:20px 24px;box-shadow:0 15px 40px rgba(0,0,0,0.85);color:#cbd5e1;font-size:13px;line-height:1.55;user-select:text}}
#credits-box h3{{margin:0 0 10px 0;color:#38bdf8;font-size:17px}}
#credits-box ul{{margin:8px 0 14px 18px;padding:0}}
#credits-box li{{margin-bottom:6px}}
#credits-box a{{color:#38bdf8;text-decoration:none}}
#credits-box a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div id="top-bar">
    <button id="btn-home" onclick="exitAtlas()">{back_btn_text}</button>
    <div id="atlas-title"><h1>Mein Himmelsatlas</h1><span>360° HiPS Sky-Survey (AWS/ESA) &amp; Lokale Planerkarte · {len(atlas_objects)} Objekte</span></div>
    <input type="text" id="search-input" list="obj-datalist" placeholder="🔍 Objekt suchen (z.B. M 31)..." onchange="onSearchSelect(this.value)" />
    <datalist id="obj-datalist"></datalist>
</div>
<div id="overlay-controls">
    <div id="ov-title">Objektname</div><div id="ov-badge-wrap"></div>
    <div id="ov-sub">Bild mit Rahmen eingepasst · Ziehen: verschieben · Mausrad: zoomen · Shift+Ziehen: drehen</div>
    <div class="ov-row"><div class="ov-label-row"><span>Deckkraft</span><span class="ov-val" id="lbl-opacity">100 %</span></div><div class="ov-slider-wrap"><input type="range" id="slider-opacity" class="ov-slider" min="0" max="100" value="100" oninput="updateOverlayParams(false)"></div></div>
    <div class="ov-row"><div class="ov-label-row"><span>Drehung</span><span class="ov-val" id="lbl-rot">0°</span></div><div class="ov-slider-wrap"><button class="ov-mini-btn" onclick="stepRotation(-1)">↶</button><input type="range" id="slider-rot" class="ov-slider" min="-180" max="180" step="0.2" value="0" oninput="updateOverlayParams(true)"><button class="ov-mini-btn" onclick="stepRotation(1)">↷</button><button class="ov-mini-btn" onclick="resetRotation()">↺</button></div></div>
    <div class="ov-row"><div class="ov-label-row"><span>Bildfeld (Größe)</span><span class="ov-val" id="lbl-fov">1.00°</span></div><div class="ov-slider-wrap"><button class="ov-mini-btn" onclick="stepFov(-0.05)">-</button><input type="range" id="slider-fov" class="ov-slider" min="0.15" max="65.0" step="0.02" value="1.0" oninput="updateOverlayParams(true)"><button class="ov-mini-btn" onclick="stepFov(0.05)">+</button></div></div>
    <div class="ov-actions"><button class="ov-action-btn" onclick="fitOverlayInView()">🎯 Bild einpassen</button><button class="ov-action-btn" onclick="toggleOverlayFrame()">🖼️ Rahmen</button><button class="ov-action-btn close" onclick="closeSkyOverlay()">✖</button></div>
</div>
<div id="sky-overlay-anchor"><div id="sky-overlay-box"><div id="sky-overlay-caption"></div><img id="sky-overlay-img" src="" alt="Overlay" /></div></div>
<div id="survey-picker-box">
    🛰️ 360° HiPS Foto-Himmel:
    <select id="survey-picker" onchange="changeHipsSurvey(this.value)">
        <option value="https://stpubdata.s3.us-east-1.amazonaws.com/mast/skybackgrounds/DSSColor">🚀 DSS2 Color (NASA / Amazon AWS Cloud - Schnell!)</option>
        <option value="https://stpubdata.s3.us-east-1.amazonaws.com/mast/skybackgrounds/PanSTARRS">🌌 PanSTARRS DR1 Color (NASA / Amazon AWS Cloud)</option>
        <option value="https://skies.esac.esa.int/DSSColor">🛰️ DSS2 Color (ESA Server Europa)</option>
        <option value="https://skies.esac.esa.int/2MASSColor">🌡️ 2MASS Infrarot (ESA Server)</option>
        <option value="https://skies.esac.esa.int/AllWISEColor">🔥 AllWISE Infrarot (ESA Server)</option>
    </select>
</div>
<div id="bottom-status"><b id="status-obj-lbl">Gesamter Himmel</b> · <span id="status-mode-lbl">🛰️ 360° Foto-Himmel (NASA / AWS Cloud)</span> · ({solved_cnt}/{total_cnt} eingemessen)</div>
<div id="bottom-controls">
    <button class="ctrl-btn" onclick="zoomBy(1.3)">−</button><span id="fov-readout">60,0° × 60,0°</span><button class="ctrl-btn" onclick="zoomBy(0.75)">+</button>
    <button class="ctrl-btn" onclick="fitOverlayInView()">Bild einpassen</button>
    <button class="ctrl-btn active" id="btn-survey" onclick="toggleSurveyMode()" title="360° HiPS Foto-Himmel (DSS2 / PanSTARRS / 2MASS) ein/ausschalten">🛰️</button>
    <button class="ctrl-btn active" id="btn-const" onclick="toggleConstellations()" title="Sternbilder ein/aus">✨</button>
    <button class="ctrl-btn active" id="btn-mw" onclick="toggleMilkyWay()" title="Milchstraße ein/aus">🌌</button>
    <button class="ctrl-btn active" id="btn-grid" onclick="toggleGrid()" title="RA/Dec-Gitter ein/aus">⊞</button>
    <button class="ctrl-btn" onclick="resetSkyView()">↺</button>
    <button class="ctrl-btn" onclick="toggleCredits(true)" title="Quellen &amp; Lizenzhinweise">ℹ</button>
</div>
<div id="side-panel">
    <button id="close-panel" onclick="document.getElementById('side-panel').style.right='-430px';document.getElementById('bottom-controls').style.right='14px';">✖</button>
    <h2 id="panel-title">Objektname</h2><div id="panel-coords"></div><div id="panel-nearby"></div><div id="panel-images"></div>
</div>
<div id="click-picker"></div>
<div id="lightbox" onclick="this.style.display='none';"><img id="lightbox-img" src="" /><div id="lightbox-caption"></div></div>
<div id="credits-modal" onclick="if(event.target===this)toggleCredits(false)">
    <div id="credits-box">
        <button style="float:right;background:none;border:none;color:#94a3b8;font-size:20px;cursor:pointer" onclick="toggleCredits(false)">✖</button>
        <h3>ℹ Quellen &amp; Lizenzhinweise</h3>
        <p>Dieser Himmelsatlas nutzt eine eigene HTML5-Canvas/HEALPix-Engine (IVOA HiPS-Standard) ohne externes <i>aladin.js</i>-Widget.</p>
        <ul>
            <li><b>Eigene Aufnahmen:</b> © Astro-Nerd.info / Stefan Raphael</li>
            <li><b>DSS2 Color:</b> Digitized Sky Survey (STScI / NASA / MAST, via AWS &amp; ESA ESAC)</li>
            <li><b>PanSTARRS DR1:</b> Pan-STARRS1 Science Consortium / STScI / MAST (AWS Cloud)</li>
            <li><b>2MASS Color:</b> Two Micron All Sky Survey (UMass / IPAC-Caltech / NASA / NSF, via ESA Sky)</li>
            <li><b>AllWISE Color:</b> WISE / AllWISE (NASA / JPL-Caltech / UCLA, via ESA Sky)</li>
            <li><b>Software:</b> Erstellt mit <a href="https://www.gnu.org/licenses/gpl-3.0.html" target="_blank">AstroArchive Explorer (GPL v3)</a></li>
        </ul>
        <button class="atlas-btn" onclick="toggleCredits(false)">Schließen</button>
    </div>
</div>
<canvas id="sky-canvas"></canvas>
<script>
const myPhotos = {json.dumps(atlas_objects)};
const canvas = document.getElementById('sky-canvas'), ctx = canvas.getContext('2d');
let currentSelectedObj = null;
let renderRequested = false;

function scheduleRender() {{
    if (!renderRequested) {{
        renderRequested = true;
        requestAnimationFrame(renderSky);
    }}
}}

const isEmbedded = (window.self !== window.top);
if (isEmbedded) document.getElementById('btn-home').innerText = '⛶ Vollbild';

function exitAtlas() {{
    if (isEmbedded) {{
        if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => window.open(window.location.href, '_blank'));
        else document.exitFullscreen();
    }} else if (window.location.protocol === 'file:') {{
        window.close(); setTimeout(() => {{ window.location.href = 'https://astro-nerd.info/'; }}, 200);
    }} else window.top.location.href = 'https://astro-nerd.info/';
}}

let cam = {{ ra: 10.68, dec: 41.27, fov: 60.0, showGrid: true, showMilkyWay: true, showConstellations: true, showSurvey: true }};
let anim = {{ active: false, startRa: 0, startDec: 0, startFov: 60, targetRa: 0, targetDec: 0, targetFov: 5, t0: 0, dur: 650 }};
let activeOverlay = {{ active: false, name: "", imgIndex: 0, ra: 0, dec: 0, fov: 1.0, rot: 0, baseFov: 1.0, baseRot: 0, flipped: false, solved: false, opacity: 1.0, showFrame: true }};
const DEG2RAD = Math.PI / 180.0, RAD2DEG = 180.0 / Math.PI;

let currentHipsBaseUrl = "https://stpubdata.s3.us-east-1.amazonaws.com/mast/skybackgrounds/DSSColor";
const hipsTileCache = new Map();
const JRLL = [2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4];
const JPLL = [1, 3, 5, 7, 0, 2, 4, 6, 1, 3, 5, 7];

function healpixXYToRaDec(nside, face, x, y) {{
    const jr = JRLL[face] * nside - x - y;
    let z = 0, phi = 0;
    if (jr < nside) {{
        if (jr <= 0.0001) return [0, 89.99];
        z = 1.0 - (jr * jr) / (3.0 * nside * nside);
        phi = (Math.PI / 4.0) * (JPLL[face] + (x - y) / jr);
    }} else if (jr <= 3 * nside) {{
        z = (2.0 / 3.0) * (2.0 - jr / nside);
        phi = (Math.PI / 4.0) * (JPLL[face] + (x - y) / nside);
    }} else {{
        const nr = 4 * nside - jr;
        if (nr <= 0.0001) return [0, -89.99];
        z = -1.0 + (nr * nr) / (3.0 * nside * nside);
        phi = (Math.PI / 4.0) * (JPLL[face] + (x - y) / nr);
    }}
    let ra = (phi * RAD2DEG) % 360.0;
    if (ra < 0) ra += 360.0;
    const dec = Math.asin(Math.max(-1, Math.min(1, z))) * RAD2DEG;
    return [ra, dec];
}}

function angDistDeg(ra1, dec1, ra2, dec2) {{
    const d1 = dec1 * DEG2RAD, d2 = dec2 * DEG2RAD, dRa = (ra1 - ra2) * DEG2RAD;
    const c = Math.sin(d1) * Math.sin(d2) + Math.cos(d1) * Math.cos(d2) * Math.cos(dRa);
    return Math.acos(Math.max(-1, Math.min(1, c))) * RAD2DEG;
}}

function getHipsTileImage(baseUrl, order, npix) {{
    const key = baseUrl + '_' + order + '_' + npix;
    if (hipsTileCache.has(key)) return hipsTileCache.get(key);
    if (hipsTileCache.size > 900) {{
        const firstK = hipsTileCache.keys().next().value;
        hipsTileCache.delete(firstK);
    }}
    const dir = Math.floor(npix / 10000) * 10000;
    const img = new Image();
    const entry = {{ img: img, loaded: false, failed: false }};
    img.onload = () => {{ entry.loaded = true; scheduleRender(); }};
    img.onerror = () => {{ entry.failed = true; }};
    img.src = `${{baseUrl}}/Norder${{order}}/Dir${{dir}}/Npix${{npix}}.jpg`;
    hipsTileCache.set(key, entry);
    return entry;
}}

function drawHipsQuad(img, p00, p10, p01, p11) {{
    const W = img.width || 512, H = img.height || 512;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(p00[0], p00[1]); ctx.lineTo(p10[0], p10[1]); ctx.lineTo(p11[0], p11[1]); ctx.lineTo(p01[0], p01[1]);
    ctx.closePath(); ctx.clip();
    ctx.transform((p10[0] - p00[0]) / W, (p10[1] - p00[1]) / W, (p01[0] - p00[0]) / H, (p01[1] - p00[1]) / H, p00[0], p00[1]);
    ctx.drawImage(img, 0, 0, W, H);
    ctx.restore();

    ctx.save();
    ctx.beginPath();
    ctx.moveTo(p11[0], p11[1]); ctx.lineTo(p10[0], p10[1]); ctx.lineTo(p01[0], p01[1]);
    ctx.closePath(); ctx.clip();
    ctx.transform((p11[0] - p01[0]) / W, (p11[1] - p01[1]) / W, (p11[0] - p10[0]) / H, (p11[1] - p10[1]) / H, p10[0] + p01[0] - p11[0], p10[1] + p01[1] - p11[1]);
    ctx.drawImage(img, 0, 0, W, H);
    ctx.restore();
}}

function renderHipsTileNode(order, targetOrder, face, ix, iy, npix) {{
    const nside = 1 << order;
    const [raC, decC] = healpixXYToRaDec(nside, face, ix + 0.5, iy + 0.5);
    const tileRadius = 75.0 / nside;
    const aspectBoost = Math.hypot(canvas.width, canvas.height) / Math.max(1, Math.min(canvas.width, canvas.height));
    if (angDistDeg(raC, decC, cam.ra, cam.dec) > cam.fov * 0.58 * aspectBoost + tileRadius) return;

    if (order < targetOrder) {{
        for (let dy = 0; dy < 2; dy++) {{
            for (let dx = 0; dx < 2; dx++) {{
                renderHipsTileNode(order + 1, targetOrder, face, 2 * ix + dx, 2 * iy + dy, 4 * npix + 2 * dy + dx);
            }}
        }}
        return;
    }}

    let drawOrd = order, drawIx = ix, drawIy = iy, drawNpix = npix, entry = null;
    while (drawOrd >= 3) {{
        const e = getHipsTileImage(currentHipsBaseUrl, drawOrd, drawNpix);
        if (e.loaded) {{ entry = e; break; }}
        drawOrd--; drawIx >>= 1; drawIy >>= 1; drawNpix >>= 2;
    }}
    if (!entry || !entry.loaded) return;

    const dNside = 1 << drawOrd;
    const c00 = healpixXYToRaDec(dNside, face, drawIx, drawIy);
    const c10 = healpixXYToRaDec(dNside, face, drawIx, drawIy + 1);
    const c01 = healpixXYToRaDec(dNside, face, drawIx + 1, drawIy);
    const c11 = healpixXYToRaDec(dNside, face, drawIx + 1, drawIy + 1);

    const p00 = world2pix(c00[0], c00[1]), p10 = world2pix(c10[0], c10[1]);
    const p01 = world2pix(c01[0], c01[1]), p11 = world2pix(c11[0], c11[1]);
    if (!p00 || !p10 || !p01 || !p11) return;
    if (Math.hypot(p10[0] - p01[0], p10[1] - p01[1]) > canvas.width * 1.4) return;
    drawHipsQuad(entry.img, p00, p10, p01, p11);
}}

function drawHipsSurveyLayer() {{
    let targetOrder = 3;
    if (cam.fov < 1.8) targetOrder = 7;
    else if (cam.fov < 4.5) targetOrder = 6;
    else if (cam.fov < 11.0) targetOrder = 5;
    else if (cam.fov < 26.0) targetOrder = 4;

    for (let face = 0; face < 12; face++) {{
        renderHipsTileNode(0, targetOrder, face, 0, 0, face);
    }}
}}

function changeHipsSurvey(url) {{
    currentHipsBaseUrl = url;
    const selText = document.getElementById('survey-picker').options[document.getElementById('survey-picker').selectedIndex].text;
    document.getElementById('status-mode-lbl').innerText = '🛰️ ' + selText.split('(')[0].trim();
    scheduleRender();
}}

function toggleSurveyMode() {{
    cam.showSurvey = !cam.showSurvey;
    document.getElementById('btn-survey').classList.toggle('active', cam.showSurvey);
    document.getElementById('survey-picker-box').style.display = cam.showSurvey ? 'block' : 'none';
    if (cam.showSurvey) {{
        changeHipsSurvey(document.getElementById('survey-picker').value);
    }} else {{
        document.getElementById('status-mode-lbl').innerText = '🗺️ Lokale Planerkarte';
    }}
    scheduleRender();
}}

function gal2eq(lDeg, bDeg) {{
    const aNGP = 192.85948 * DEG2RAD, dNGP = 27.12825 * DEG2RAD, lNCP = 122.93192 * DEG2RAD;
    const l = lDeg * DEG2RAD, b = bDeg * DEG2RAD, dL = lNCP - l;
    const sinB = Math.sin(b), cosB = Math.cos(b), sinD = Math.sin(dNGP), cosD = Math.cos(dNGP);
    const dec = Math.asin(Math.max(-1, Math.min(1, sinD * sinB + cosD * cosB * Math.cos(dL))));
    let ra = (aNGP + Math.atan2(cosB * Math.sin(dL), cosD * sinB - sinD * cosB * Math.cos(dL))) * RAD2DEG;
    return [((ra % 360) + 360) % 360, dec * RAD2DEG];
}}

const milkyWaySpine = [];
for (let l = 0; l <= 360; l += 2) {{ const [ra, dec] = gal2eq(l, 0); milkyWaySpine.push({{ l, ra, dec }}); }}

const brightStars = [
    {{name:"Beteigeuze",ra:88.79,dec:7.41,mag:0.45,col:"#ffaa60"}},{{name:"Rigel",ra:78.63,dec:-8.20,mag:0.18,col:"#bfdfff"}},{{name:"Bellatrix",ra:81.28,dec:6.35,mag:1.64,col:"#cce6ff"}},{{name:"Alnilam",ra:84.05,dec:-1.20,mag:1.69,col:"#cce6ff"}},{{name:"Alnitak",ra:85.19,dec:-1.94,mag:1.77,col:"#cce6ff"}},{{name:"Mintaka",ra:83.00,dec:-0.30,mag:2.23,col:"#cce6ff"}},{{name:"Saiph",ra:86.94,dec:-9.67,mag:2.06,col:"#cce6ff"}},{{name:"Meissa",ra:83.78,dec:9.93,mag:3.39,col:"#d8ecff"}},{{name:"Hatysa",ra:83.86,dec:-5.91,mag:2.77,col:"#d8ecff"}},{{name:"Sirius",ra:101.29,dec:-16.72,mag:-1.46,col:"#e0f2ff"}},{{name:"Aldebaran",ra:68.98,dec:16.51,mag:0.87,col:"#ffb066"}},{{name:"Elnath",ra:81.57,dec:28.61,mag:1.65,col:"#cce6ff"}},{{name:"Capella",ra:79.17,dec:46.00,mag:0.08,col:"#fff0b8"}},{{name:"Menkalinan",ra:89.88,dec:44.95,mag:1.90,col:"#e8f4ff"}},{{name:"Procyon",ra:114.83,dec:5.22,mag:0.34,col:"#fff6d8"}},{{name:"Pollux",ra:116.33,dec:28.03,mag:1.14,col:"#ffc885"}},{{name:"Castor",ra:113.65,dec:31.89,mag:1.58,col:"#e8f4ff"}},{{name:"Alhena",ra:99.43,dec:16.40,mag:1.93,col:"#e8f4ff"}},{{name:"Wega",ra:279.23,dec:38.78,mag:0.03,col:"#d8ecff"}},{{name:"Deneb",ra:310.36,dec:45.28,mag:1.25,col:"#e0f0ff"}},{{name:"Sadr",ra:305.56,dec:40.26,mag:2.23,col:"#fff2cc"}},{{name:"Albireo",ra:292.68,dec:27.96,mag:3.05,col:"#ffd28a"}},{{name:"Gienah",ra:311.55,dec:33.97,mag:2.48,col:"#ffcc88"}},{{name:"Altair",ra:297.70,dec:8.87,mag:0.76,col:"#eef6ff"}},{{name:"Schedar",ra:10.13,dec:56.54,mag:2.24,col:"#ffcc88"}},{{name:"Caph",ra:2.29,dec:59.15,mag:2.28,col:"#fff4d8"}},{{name:"Gamma Cas",ra:14.18,dec:60.72,mag:2.15,col:"#bfdfff"}},{{name:"Ruchbah",ra:21.45,dec:60.24,mag:2.68,col:"#e8f4ff"}},{{name:"Segin",ra:28.60,dec:63.67,mag:3.35,col:"#cce6ff"}},{{name:"Mirfak",ra:51.08,dec:49.86,mag:1.79,col:"#fff2cc"}},{{name:"Algol",ra:47.04,dec:40.96,mag:2.12,col:"#d8ecff"}},{{name:"Alpheratz",ra:2.09,dec:29.09,mag:2.07,col:"#d8ecff"}},{{name:"Mirach",ra:17.43,dec:35.62,mag:2.07,col:"#ffaa66"}},{{name:"Almach",ra:30.97,dec:42.33,mag:2.10,col:"#ffcc80"}},{{name:"Enif",ra:326.05,dec:9.88,mag:2.38,col:"#ffbb77"}},{{name:"Markab",ra:346.19,dec:15.21,mag:2.49,col:"#d8ecff"}},{{name:"Scheat",ra:345.94,dec:28.08,mag:2.44,col:"#ffaa60"}},{{name:"Algenib",ra:3.31,dec:15.18,mag:2.83,col:"#cce6ff"}},{{name:"Polaris",ra:37.95,dec:89.26,mag:1.97,col:"#fff4cc"}},{{name:"Kochab",ra:222.68,dec:74.16,mag:2.07,col:"#ffbb77"}},{{name:"Dubhe",ra:165.93,dec:61.75,mag:1.79,col:"#ffcc88"}},{{name:"Merak",ra:165.46,dec:56.38,mag:2.37,col:"#e8f4ff"}},{{name:"Phecda",ra:178.46,dec:53.69,mag:2.44,col:"#e8f4ff"}},{{name:"Megrez",ra:183.86,dec:57.03,mag:3.32,col:"#e8f4ff"}},{{name:"Alioth",ra:193.51,dec:55.96,mag:1.76,col:"#e8f4ff"}},{{name:"Mizar",ra:200.98,dec:54.93,mag:2.23,col:"#e8f4ff"}},{{name:"Alkaid",ra:206.89,dec:49.31,mag:1.85,col:"#bfdfff"}},{{name:"Arcturus",ra:213.92,dec:19.18,mag:-0.05,col:"#ffb86c"}},{{name:"Spica",ra:201.30,dec:-11.16,mag:0.98,col:"#bfdfff"}},{{name:"Regulus",ra:152.09,dec:11.97,mag:1.36,col:"#d8ecff"}},{{name:"Algieba",ra:154.99,dec:19.84,mag:2.01,col:"#ffcc80"}},{{name:"Denebola",ra:177.26,dec:14.57,mag:2.14,col:"#e8f4ff"}},{{name:"Antares",ra:247.35,dec:-26.43,mag:1.06,col:"#ff8c44"}},{{name:"Shaula",ra:263.40,dec:-37.10,mag:1.62,col:"#bfdfff"}},{{name:"Kaus Australis",ra:276.04,dec:-34.38,mag:1.79,col:"#d8ecff"}},{{name:"Nunki",ra:283.82,dec:-26.30,mag:2.05,col:"#bfdfff"}},{{name:"Fomalhaut",ra:344.41,dec:-29.62,mag:1.16,col:"#e8f4ff"}},{{name:"Deneb Kaitos",ra:10.90,dec:-17.99,mag:2.04,col:"#ffcc80"}},{{name:"Menkar",ra:45.57,dec:4.09,mag:2.54,col:"#ffaa60"}},{{name:"Alderamin",ra:319.64,dec:62.59,mag:2.45,col:"#f0f6ff"}},{{name:"Eltanin",ra:269.15,dec:51.49,mag:2.24,col:"#ffbb77"}}
];

const constellations = [
    {{name:"Orion",ra:83.8,dec:3.2,lines:[[88.79,7.41,83.78,9.93],[83.78,9.93,81.28,6.35],[88.79,7.41,81.28,6.35],[81.28,6.35,83.00,-0.30],[83.00,-0.30,84.05,-1.20],[84.05,-1.20,85.19,-1.94],[85.19,-1.94,88.79,7.41],[83.00,-0.30,78.63,-8.20],[78.63,-8.20,86.94,-9.67],[86.94,-9.67,85.19,-1.94],[84.05,-1.20,83.86,-5.91]]}},
    {{name:"Taurus (Stier)",ra:68.0,dec:19.0,lines:[[68.98,16.51,81.57,28.61],[68.98,16.51,67.15,19.18],[67.15,19.18,84.41,21.14],[68.98,16.51,64.95,15.63],[64.95,15.63,56.87,24.11]]}},
    {{name:"Auriga (Fuhrmann)",ra:84.0,dec:42.0,lines:[[79.17,46.00,89.88,44.95],[89.88,44.95,89.93,37.21],[89.93,37.21,81.57,28.61],[81.57,28.61,74.25,33.17],[74.25,33.17,79.17,46.00]]}},
    {{name:"Gemini (Zwillinge)",ra:108.0,dec:24.0,lines:[[113.65,31.89,116.33,28.03],[113.65,31.89,100.98,25.13],[100.98,25.13,95.74,22.51],[116.33,28.03,106.03,20.57],[106.03,20.57,99.43,16.40]]}},
    {{name:"Canis Major",ra:104.0,dec:-22.0,lines:[[101.29,-16.72,95.67,-17.96],[101.29,-16.72,107.10,-26.39],[107.10,-26.39,104.66,-28.97],[107.10,-26.39,111.02,-29.30]]}},
    {{name:"Cygnus (Schwan)",ra:307.0,dec:42.0,lines:[[310.36,45.28,305.56,40.26],[305.56,40.26,292.68,27.96],[296.24,45.13,305.56,40.26],[305.56,40.26,311.55,33.97]]}},
    {{name:"Lyra (Leier)",ra:282.0,dec:36.0,lines:[[279.23,38.78,283.63,32.69],[283.63,32.69,284.74,32.69],[284.74,32.69,284.05,36.90],[284.05,36.90,279.23,38.78]]}},
    {{name:"Aquila (Adler)",ra:296.0,dec:6.0,lines:[[296.56,10.61,297.70,8.87],[297.70,8.87,298.83,6.41],[297.70,8.87,291.37,3.11],[291.37,3.11,286.35,13.86],[291.37,3.11,302.83,-0.82]]}},
    {{name:"Cassiopeia",ra:15.0,dec:60.0,lines:[[2.29,59.15,10.13,56.54],[10.13,56.54,14.18,60.72],[14.18,60.72,21.45,60.24],[21.45,60.24,28.60,63.67]]}},
    {{name:"Cepheus",ra:332.0,dec:68.0,lines:[[319.64,62.59,322.16,70.56],[322.16,70.56,354.84,77.63],[354.84,77.63,341.97,66.20],[341.97,66.20,332.71,58.20],[332.71,58.20,319.64,62.59]]}},
    {{name:"Perseus",ra:51.0,dec:45.0,lines:[[43.56,55.90,51.08,49.86],[51.08,49.86,47.04,40.96],[51.08,49.86,56.08,47.79],[56.08,47.79,58.53,40.01],[58.53,40.01,57.59,31.88]]}},
    {{name:"Andromeda",ra:14.0,dec:38.0,lines:[[2.09,29.09,9.83,30.86],[9.83,30.86,17.43,35.62],[17.43,35.62,30.97,42.33]]}},
    {{name:"Pegasus",ra:340.0,dec:20.0,lines:[[2.09,29.09,345.94,28.08],[345.94,28.08,346.19,15.21],[346.19,15.21,3.31,15.18],[3.31,15.18,2.09,29.09],[346.19,15.21,340.37,10.83],[340.37,10.83,326.05,9.88]]}},
    {{name:"Ursa Major (Großer Bär)",ra:185.0,dec:55.0,lines:[[165.93,61.75,165.46,56.38],[165.46,56.38,178.46,53.69],[178.46,53.69,183.86,57.03],[183.86,57.03,165.93,61.75],[183.86,57.03,193.51,55.96],[193.51,55.96,200.98,54.93],[200.98,54.93,206.89,49.31]]}},
    {{name:"Ursa Minor (Kleiner Bär)",ra:230.0,dec:78.0,lines:[[37.95,89.26,263.05,86.59],[263.05,86.59,251.49,82.04],[251.49,82.04,236.01,77.79],[236.01,77.79,222.68,74.16],[222.68,74.16,230.18,71.83],[230.18,71.83,244.38,75.76],[244.38,75.76,236.01,77.79]]}},
    {{name:"Boötes (Bärenhüter)",ra:218.0,dec:30.0,lines:[[213.92,19.18,221.25,27.07],[221.25,27.07,225.49,40.39],[225.49,40.39,218.02,38.31],[218.02,38.31,213.92,19.18]]}},
    {{name:"Hercules",ra:255.0,dec:35.0,lines:[[247.55,21.49,250.32,31.60],[250.32,31.60,250.42,38.92],[250.42,38.92,260.03,36.81],[260.03,36.81,254.46,29.83],[254.46,29.83,250.32,31.60]]}},
    {{name:"Leo (Löwe)",ra:160.0,dec:18.0,lines:[[152.09,11.97,154.99,19.84],[154.99,19.84,154.17,23.42],[154.17,23.42,148.19,26.01],[154.99,19.84,168.53,20.52],[168.53,20.52,177.26,14.57],[177.26,14.57,168.56,15.43],[168.56,15.43,152.09,11.97]]}},
    {{name:"Virgo (Jungfrau)",ra:195.0,dec:-3.0,lines:[[201.30,-11.16,190.42,-1.45],[190.42,-1.45,193.90,3.40],[193.90,3.40,195.54,10.96],[190.42,-1.45,184.98,-0.67],[184.98,-0.67,177.67,1.76]]}},
    {{name:"Scorpius (Skorpion)",ra:252.0,dec:-28.0,lines:[[241.36,-19.81,240.08,-22.62],[240.08,-22.62,247.35,-26.43],[247.35,-26.43,252.97,-34.29],[252.97,-34.29,253.08,-38.05],[253.08,-38.05,264.33,-43.00],[264.33,-43.00,265.62,-39.03],[265.62,-39.03,263.40,-37.10]]}},
    {{name:"Sagittarius (Schütze)",ra:282.0,dec:-27.0,lines:[[276.04,-34.38,275.25,-29.83],[275.25,-29.83,276.99,-25.42],[276.99,-25.42,283.82,-26.30],[283.82,-26.30,286.74,-29.88],[286.74,-29.88,276.04,-34.38],[275.25,-29.83,271.45,-30.42]]}}
];

const starSet = new Set(brightStars.map(s => Math.round(s.ra) + '_' + Math.round(s.dec)));
constellations.forEach(c => c.lines.forEach(ln => [[ln[0],ln[1]],[ln[2],ln[3]]].forEach(pt => {{
    const k = Math.round(pt[0]) + '_' + Math.round(pt[1]);
    if (!starSet.has(k)) {{ starSet.add(k); brightStars.push({{name:"",ra:pt[0],dec:pt[1],mag:2.5,col:"#e2f0ff"}}); }}
}})));

const dl = document.getElementById('obj-datalist');
myPhotos.forEach(p => {{ const o = document.createElement('option'); o.value = p.name; dl.appendChild(o); }});

function onSearchSelect(val) {{
    const f = myPhotos.find(p => p.name.toLowerCase().includes(val.toLowerCase()));
    if (f) {{ selectObject(f, true); document.getElementById('search-input').blur(); }}
}}

function world2pix(raDeg, decDeg) {{
    const dRa = (raDeg - cam.ra) * DEG2RAD, dec = decDeg * DEG2RAD, dec0 = cam.dec * DEG2RAD;
    const sinDec = Math.sin(dec), cosDec = Math.cos(dec), sinDec0 = Math.sin(dec0), cosDec0 = Math.cos(dec0);
    const cosDra = Math.cos(dRa), sinDra = Math.sin(dRa);
    const cosC = sinDec0 * sinDec + cosDec0 * cosDec * cosDra;
    if (cosC <= -0.15) return null;
    const k = 2.0 / (1.0 + cosC);
    const scale = Math.min(canvas.width, canvas.height) / (2.0 * Math.tan(Math.min(130, cam.fov) * DEG2RAD / 2.0));
    return [canvas.width * 0.5 - (k * cosDec * sinDra) * scale, canvas.height * 0.5 - (k * (cosDec0 * sinDec - sinDec0 * cosDec * cosDra)) * scale];
}}

function hash2D(ix, iy) {{ let n = Math.sin(ix * 127.1 + iy * 311.7) * 43758.5453123; return n - Math.floor(n); }}

function formatRA(raDeg) {{
    let r = ((raDeg % 360) + 360) % 360, tm = (r / 15.0) * 60.0, h = Math.floor(tm / 60), m = Math.round(tm % 60);
    if (m === 60) {{ h = (h + 1) % 24; m = 0; }}
    return 'RA ' + h + 'h ' + (m < 10 ? '0' + m : m) + 'm';
}}

function formatDec(decDeg) {{
    let s = decDeg >= 0 ? '+' : '−', a = Math.abs(decDeg), d = Math.floor(a), m = Math.round((a - d) * 60);
    if (m === 60) {{ d += 1; m = 0; }}
    return m === 0 ? ('Dec ' + s + d + '°') : ('Dec ' + s + d + '° ' + (m < 10 ? '0' + m : m) + "'");
}}

function drawMilkyWayBand() {{
    const pxPerDeg = Math.min(canvas.width, canvas.height) / cam.fov, fade = cam.fov < 5.0 ? Math.max(0.2, cam.fov / 5.0) : 1.0;
    ctx.save(); ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    if (!cam.showSurvey) {{
        [{{w:22.0,c:`rgba(110,155,230,${{(0.045*fade).toFixed(3)}})`}},{{w:13.0,c:`rgba(140,185,245,${{(0.065*fade).toFixed(3)}})`}},{{w:6.5,c:`rgba(186,225,255,${{(0.075*fade).toFixed(3)}})`}}].forEach(b => {{
            ctx.beginPath(); ctx.strokeStyle = b.c; ctx.lineWidth = Math.min(700, b.w * pxPerDeg);
            let prev = null;
            milkyWaySpine.forEach(pt => {{
                const p = world2pix(pt.ra, pt.dec);
                if (p && p[0] > -400 && p[0] < canvas.width + 400 && p[1] > -400 && p[1] < canvas.height + 400) {{
                    if (!prev || Math.hypot(p[0]-prev[0], p[1]-prev[1]) > 350) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
                    prev = p;
                }} else prev = null;
            }});
            ctx.stroke();
        }});
    }}
    ctx.setLineDash([6, 6]); ctx.lineWidth = 1.2; ctx.strokeStyle = `rgba(147,197,253,${{(0.28*fade).toFixed(3)}})`;
    ctx.beginPath(); let prev = null;
    milkyWaySpine.forEach(pt => {{
        const p = world2pix(pt.ra, pt.dec);
        if (p && p[0] > -200 && p[0] < canvas.width + 200 && p[1] > -200 && p[1] < canvas.height + 200) {{
            if (!prev || Math.hypot(p[0]-prev[0], p[1]-prev[1]) > 250) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
            prev = p;
        }} else prev = null;
    }});
    ctx.stroke(); ctx.setLineDash([]); ctx.restore();
}}

function renderSky(timestamp) {{
    renderRequested = false;
    if (canvas.width !== window.innerWidth || canvas.height !== window.innerHeight) {{ canvas.width = window.innerWidth; canvas.height = window.innerHeight; }}
    if (anim.active) {{
        let t = Math.min(1.0, (timestamp - anim.t0) / anim.dur), ease = t < 0.5 ? 2*t*t : -1+(4-2*t)*t;
        let dRa = ((anim.targetRa - anim.startRa + 540) % 360) - 180;
        cam.ra = (anim.startRa + dRa * ease + 360) % 360;
        cam.dec = anim.startDec + (anim.targetDec - anim.startDec) * ease;
        cam.fov = anim.startFov * Math.pow(anim.targetFov / anim.startFov, ease);
        if (t >= 1.0) anim.active = false;
    }}

    const grad = ctx.createRadialGradient(canvas.width*0.5, canvas.height*0.5, 50, canvas.width*0.5, canvas.height*0.5, Math.max(canvas.width, canvas.height)*0.7);
    grad.addColorStop(0, '#0a101d'); grad.addColorStop(1, '#04060b');
    ctx.fillStyle = grad; ctx.fillRect(0, 0, canvas.width, canvas.height);

    if (cam.showSurvey) {{
        drawHipsSurveyLayer();
    }}

    if (cam.showMilkyWay) drawMilkyWayBand();

    if (cam.showGrid) {{
        let decStep = 10, raStep = 15;
        if (cam.fov < 3.5) {{ decStep = 0.5; raStep = 0.5; }} else if (cam.fov < 10) {{ decStep = 1; raStep = 1.25; }} else if (cam.fov < 25) {{ decStep = 2; raStep = 3.75; }} else if (cam.fov < 50) {{ decStep = 5; raStep = 7.5; }}
        ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(71,118,168,0.32)'; ctx.fillStyle = 'rgba(148,184,218,0.78)'; ctx.font = '11px "Segoe UI",sans-serif';
        const minDec = Math.max(-85, Math.floor((cam.dec - cam.fov*0.8) / decStep) * decStep), maxDec = Math.min(85, Math.ceil((cam.dec + cam.fov*0.8) / decStep) * decStep);
        const raSpan = Math.min(180, (cam.fov * 0.9) / Math.max(0.18, Math.cos(cam.dec * DEG2RAD)));
        const minRa = Math.floor((cam.ra - raSpan) / raStep) * raStep, maxRa = Math.ceil((cam.ra + raSpan) / raStep) * raStep;
        for (let d = minDec; d <= maxDec; d += decStep) {{
            ctx.beginPath(); let st = false, lp = null;
            for (let r = minRa; r <= maxRa; r += raStep * 0.25) {{
                const p = world2pix(r, d);
                if (p && p[0] > -200 && p[0] < canvas.width + 200 && p[1] > -200 && p[1] < canvas.height + 200) {{
                    if (!st) {{ ctx.moveTo(p[0], p[1]); st = true; }} else ctx.lineTo(p[0], p[1]);
                    if (!lp && p[0] > 80 && p[0] < canvas.width * 0.35 && p[1] > 60 && p[1] < canvas.height - 60) lp = p;
                }} else st = false;
            }}
            ctx.stroke(); if (lp) ctx.fillText(formatDec(d), lp[0] + 4, lp[1] - 4);
        }}
        for (let r = minRa; r <= maxRa; r += raStep) {{
            ctx.beginPath(); let st = false, lp = null;
            for (let d = minDec; d <= maxDec; d += decStep * 0.25) {{
                const p = world2pix(r, d);
                if (p && p[0] > -200 && p[0] < canvas.width + 200 && p[1] > -200 && p[1] < canvas.height + 200) {{
                    if (!st) {{ ctx.moveTo(p[0], p[1]); st = true; }} else ctx.lineTo(p[0], p[1]);
                    if (!lp && p[0] > canvas.width * 0.6 && p[0] < canvas.width - 90 && p[1] > 60 && p[1] < canvas.height - 60) lp = p;
                }} else st = false;
            }}
            ctx.stroke(); if (lp) ctx.fillText(formatRA(r), lp[0] + 4, lp[1] - 4);
        }}
    }}

    if (cam.showConstellations) {{
        ctx.strokeStyle = 'rgba(56,189,248,0.38)'; ctx.lineWidth = 1.25; ctx.fillStyle = 'rgba(125,211,252,0.65)'; ctx.font = 'bold 12px "Segoe UI",sans-serif';
        constellations.forEach(c => {{
            c.lines.forEach(ln => {{ const p1 = world2pix(ln[0], ln[1]), p2 = world2pix(ln[2], ln[3]); if (p1 && p2) {{ ctx.beginPath(); ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]); ctx.stroke(); }} }});
            const pc = world2pix(c.ra, c.dec); if (pc && pc[0] > 0 && pc[0] < canvas.width && pc[1] > 0 && pc[1] < canvas.height) ctx.fillText(c.name, pc[0] - 25, pc[1]);
        }});
    }}

    if (!cam.showSurvey) {{
        const cosDec0 = Math.max(0.15, Math.cos(cam.dec * DEG2RAD));
        [{{c:5.0,m:130,n:6,r:1.6,a:0.70}},{{c:1.5,m:40,n:8,r:1.4,a:0.65}},{{c:0.4,m:12,n:12,r:1.1,a:0.55}}].forEach(lv => {{
            if (cam.fov > lv.m) return;
            const dS = cam.fov * 0.75, rS = Math.min(180, dS / cosDec0);
            const idMin = Math.floor((cam.dec - dS) / lv.c), idMax = Math.ceil((cam.dec + dS) / lv.c);
            const irMin = Math.floor((cam.ra - rS) / lv.c), irMax = Math.ceil((cam.ra + rS) / lv.c);
            ctx.fillStyle = `rgba(235,244,255,${{lv.a}})`;
            const nCells = Math.round(360 / lv.c);
            for (let id = idMin; id <= idMax; id++) {{
                if (id * lv.c < -89 || id * lv.c > 89) continue;
                for (let ir = irMin; ir <= irMax; ir++) {{
                    let irW = ((ir % nCells) + nCells) % nCells;
                    for (let s = 0; s < lv.n; s++) {{
                        const h1 = hash2D(irW * 17 + s, id * 31 + s), h2 = hash2D(id * 23 + s, irW * 41 + s);
                        const p = world2pix((irW + h1) * lv.c, (id + h2) * lv.c);
                        if (p && p[0] >= 0 && p[0] <= canvas.width && p[1] >= 0 && p[1] <= canvas.height) {{
                            ctx.beginPath(); ctx.arc(p[0], p[1], 0.4 + (h1 * h2) * lv.r, 0, Math.PI * 2); ctx.fill();
                        }}
                    }}
                }}
            }}
        }});
    }}

    brightStars.forEach(st => {{
        const p = world2pix(st.ra, st.dec);
        if (!p || p[0] < -30 || p[0] > canvas.width + 30 || p[1] < -30 || p[1] > canvas.height + 30) return;
        const coreR = Math.max(1.8, 4.6 - st.mag * 0.9);
        if (!cam.showSurvey) {{
            const haloR = coreR * 3.2, sG = ctx.createRadialGradient(p[0], p[1], coreR * 0.3, p[0], p[1], haloR);
            sG.addColorStop(0, st.col); sG.addColorStop(0.4, st.col + '66'); sG.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.fillStyle = sG; ctx.beginPath(); ctx.arc(p[0], p[1], haloR, 0, Math.PI * 2); ctx.fill();
            ctx.fillStyle = '#fff'; ctx.beginPath(); ctx.arc(p[0], p[1], coreR * 0.75, 0, Math.PI * 2); ctx.fill();
        }}
        if (cam.showConstellations && st.name && (cam.fov < 28 || st.mag < 1.3)) {{
            ctx.font = '11px "Segoe UI",sans-serif'; ctx.fillStyle = 'rgba(226,232,240,0.88)'; ctx.fillText(st.name, p[0] + coreR + 4, p[1] - 3);
        }}
    }});

    const placedLabels = [];
    myPhotos.forEach(obj => {{
        const p = world2pix(obj.ra, obj.dec);
        if (!p || p[0] < -50 || p[0] > canvas.width + 50 || p[1] < -50 || p[1] > canvas.height + 50) return;
        const isSel = activeOverlay.active && activeOverlay.name === obj.name, rad = cam.fov < 10 ? 10 : 7;
        ctx.beginPath(); ctx.arc(p[0], p[1], rad, 0, Math.PI * 2); ctx.strokeStyle = isSel ? '#38bdf8' : '#2dd4bf'; ctx.lineWidth = isSel ? 2.2 : 1.5; ctx.stroke();
        ctx.beginPath(); ctx.arc(p[0], p[1], 2.2, 0, Math.PI * 2); ctx.fillStyle = isSel ? '#fff' : '#5eead4'; ctx.fill();
        if (cam.fov < 35 || obj.name.startsWith('M ') || isSel) {{
            const lbl = obj.name.split(' - ')[0];
            ctx.font = isSel ? 'bold 12px "Segoe UI",sans-serif' : '11.5px "Segoe UI",sans-serif';
            let lx = p[0] + rad + 5, ly = p[1] + 4;
            for (let a = 0; a < 5; a++) {{ if (!placedLabels.some(pl => Math.abs(pl.x - lx) < 85 && Math.abs(pl.y - ly) < 13)) break; ly += 13; }}
            placedLabels.push({{x: lx, y: ly}}); ctx.fillStyle = isSel ? '#38bdf8' : '#5eead4'; ctx.fillText(lbl, lx, ly);
        }}
    }});

    const anchor = document.getElementById('sky-overlay-anchor'), imgEl = document.getElementById('sky-overlay-img');
    if (activeOverlay.active) {{
        const pC = world2pix(activeOverlay.ra, activeOverlay.dec);
        const eps = 0.05;
        const pDir = activeOverlay.dec < 89.9
            ? world2pix(activeOverlay.ra, activeOverlay.dec + eps)
            : world2pix(activeOverlay.ra, activeOverlay.dec - eps);
        const dRa = (activeOverlay.ra - cam.ra) * DEG2RAD, dec = activeOverlay.dec * DEG2RAD, dec0 = cam.dec * DEG2RAD;
        const cosC = Math.sin(dec0) * Math.sin(dec) + Math.cos(dec0) * Math.cos(dec) * Math.cos(dRa);
        if (pC && pDir && cosC > -0.1) {{
            const k = 2.0 / (1.0 + cosC);
            const pxScale = Math.min(canvas.width, canvas.height) / (2.0 * Math.tan(Math.min(130, cam.fov) * DEG2RAD / 2.0));
            const distPx = k * pxScale * 4.0 * Math.tan(Math.min(130, activeOverlay.fov) * DEG2RAD / 4.0);
            if (distPx < 8 || distPx > 9000) anchor.style.display = 'none';
            else {{
                anchor.style.display = 'block'; imgEl.style.width = distPx + 'px';
                const dxN = activeOverlay.dec < 89.9 ? (pDir[0] - pC[0]) : (pC[0] - pDir[0]);
                const dyN = activeOverlay.dec < 89.9 ? (pDir[1] - pC[1]) : (pC[1] - pDir[1]);
                const totAng = (Math.atan2(dyN, dxN) * RAD2DEG + 90) + activeOverlay.rot;
                anchor.style.left = pC[0] + 'px'; anchor.style.top = pC[1] + 'px';
                anchor.style.transform = 'translate(-50%, -50%) rotate(' + totAng.toFixed(2) + 'deg)';
                imgEl.style.transform = 'scaleX(' + (activeOverlay.flipped ? -1 : 1) + ')';
            }}
        }} else anchor.style.display = 'none';
    }} else anchor.style.display = 'none';

    document.getElementById('fov-readout').innerText = cam.fov.toFixed(2).replace('.', ',') + '° × ' + cam.fov.toFixed(2).replace('.', ',') + '°';
    if (anim.active) scheduleRender();
}}

let isDragging = false, dragMoved = false, lastX = 0, lastY = 0;
canvas.addEventListener('mousedown', e => {{ if (e.button === 0) {{ hideClickPicker(); isDragging = true; dragMoved = false; lastX = e.clientX; lastY = e.clientY; anim.active = false; }} }});
window.addEventListener('mousemove', e => {{
    if (!isDragging) return;
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    if (Math.hypot(dx, dy) > 4) dragMoved = true;
    lastX = e.clientX; lastY = e.clientY;
    if (e.shiftKey && activeOverlay.active) {{ stepRotation(dx * 0.3); return; }}
    const degPerPx = cam.fov / Math.min(canvas.width, canvas.height);
    cam.ra = (cam.ra + (dx * degPerPx) / Math.max(0.15, Math.cos(cam.dec * DEG2RAD)) + 360) % 360;
    cam.dec = Math.max(-89.0, Math.min(89.0, cam.dec + dy * degPerPx));
    scheduleRender();
}});
window.addEventListener('mouseup', e => {{
    if (isDragging && !dragMoved) handleCanvasClick(e.clientX, e.clientY);
    isDragging = false;
}});
canvas.addEventListener('wheel', e => {{
    e.preventDefault(); anim.active = false; hideClickPicker();
    cam.fov = Math.max(0.25, Math.min(115.0, cam.fov * (e.deltaY > 0 ? 1.14 : 0.88)));
    scheduleRender();
}}, {{ passive: false }});
window.addEventListener('resize', scheduleRender);
function toggleCredits(show) {{
    document.getElementById('credits-modal').style.display = show ? 'flex' : 'none';
}}
window.addEventListener('keydown', e => {{ if (e.key === 'Escape') {{ hideClickPicker(); toggleCredits(false); }} }});

function hideClickPicker() {{
    const cp = document.getElementById('click-picker');
    if (cp) cp.style.display = 'none';
}}

function selectObjectByIdx(idx) {{
    hideClickPicker();
    if (idx >= 0 && idx < myPhotos.length) {{
        selectObject(myPhotos[idx], true);
    }}
}}

function showClickPicker(mx, my, hits) {{
    const cp = document.getElementById('click-picker');
    let html = '<div class="cp-header">📍 ' + hits.length + ' Objekte an dieser Position:</div>';
    hits.forEach(h => {{
        const cnt = h.obj.images ? h.obj.images.length : 0;
        html += '<button class="cp-item" onclick="selectObjectByIdx(' + h.idx + ')"><span>' + h.obj.name + '</span><span class="cp-meta">' + cnt + ' Aufn.</span></button>';
    }});
    cp.innerHTML = html;
    cp.style.display = 'block';
    const left = Math.min(mx + 10, Math.max(10, window.innerWidth - 330));
    const top = Math.min(my + 10, Math.max(10, window.innerHeight - 280));
    cp.style.left = left + 'px';
    cp.style.top = top + 'px';
}}

function handleCanvasClick(mx, my) {{
    hideClickPicker();
    const hits = [];
    myPhotos.forEach((obj, idx) => {{
        const p = world2pix(obj.ra, obj.dec);
        if (p) {{
            const d = Math.hypot(p[0] - mx, p[1] - my);
            if (d < 18) hits.push({{ obj: obj, idx: idx, dist: d }});
        }}
    }});
    if (hits.length === 0) return;
    hits.sort((a, b) => a.dist - b.dist || (b.obj.images.length - a.obj.images.length));
    if (hits.length === 1) {{
        selectObject(hits[0].obj, true);
    }} else {{
        showClickPicker(mx, my, hits);
    }}
}}

function flyTo(tRa, tDec, tFov) {{
    anim.startRa = cam.ra; anim.startDec = cam.dec; anim.startFov = cam.fov;
    anim.targetRa = tRa; anim.targetDec = tDec; anim.targetFov = tFov;
    anim.t0 = performance.now(); anim.active = true;
    scheduleRender();
}}

function selectObject(data, zoomIn) {{
    currentSelectedObj = data;
    document.getElementById('panel-title').innerText = data.name;
    document.getElementById('status-obj-lbl').innerText = data.name;
    let cH = '<b>RA:</b> ' + data.ra.toFixed(4) + '° | <b>DEC:</b> ' + data.dec.toFixed(4) + '°<br><span style="font-size:12px;color:#999;display:block;margin-top:6px;">📅 <b>Erste Aufnahme:</b> ' + data.first_obs + '<br>📅 <b>Letzte Aufnahme:</b> ' + data.last_obs + '</span>';
    let sN = '', mN = data.name.match(/NGC\\s*(\\d+)/i), mM = data.name.match(/M\\s*(\\d+)/i), mI = data.name.match(/IC\\s*(\\d+)/i);
    if (mN) sN = 'NGC' + mN[1]; else if (mM) sN = 'M' + mM[1]; else if (mI) sN = 'IC' + mI[1];
    let lH = '<div class="panel-link-container">';
    if (data.wiki_url) lH += '<a href="' + data.wiki_url + '" target="_blank" class="atlas-btn">🌐 Wikipedia</a>';
    if (sN) lH += '<a href="https://stellarium-web.org/skysource/' + sN + '" target="_blank" class="atlas-btn">✨ Stellarium</a><a href="https://www.astrobin.com/search/?q=' + sN + '" target="_blank" class="atlas-btn">📷 AstroBin</a>';
    document.getElementById('panel-coords').innerHTML = cH + lH + '</div>';

    const nearbyEl = document.getElementById('panel-nearby');
    const searchRad = Math.max((data.fov || 1.0) * 1.5, 2.5);
    const nearby = [];
    myPhotos.forEach((other, idx) => {{
        if (other === data || other.name === data.name) return;
        const sep = angDistDeg(data.ra, data.dec, other.ra, other.dec);
        if (sep <= searchRad) {{
            nearby.push({{ obj: other, idx: idx, sep: sep }});
        }}
    }});
    nearby.sort((a, b) => a.sep - b.sep || (b.obj.images.length - a.obj.images.length));
    if (nearby.length > 0) {{
        let nH = '<div class="nearby-title">📍 Objekte in der Nähe (' + nearby.length + '):</div><div class="nearby-list">';
        nearby.slice(0, 15).forEach(n => {{
            const sepTxt = n.sep < 0.05 ? 'selbe Pos.' : n.sep.toFixed(1).replace('.', ',') + '°';
            const cnt = n.obj.images ? n.obj.images.length : 0;
            nH += '<button class="nearby-chip" onclick="selectObjectByIdx(' + n.idx + ')">' + n.obj.name + ' <span style="opacity:0.75">(' + sepTxt + ' · ' + cnt + ')</span></button>';
        }});
        nH += '</div>';
        nearbyEl.innerHTML = nH;
        nearbyEl.style.display = 'block';
    }} else {{
        nearbyEl.innerHTML = '';
        nearbyEl.style.display = 'none';
    }}

    let iH = '';
    data.images.forEach((imgUrl, idx) => {{
        const meta = (data.image_meta && data.image_meta[idx]) ? data.image_meta[idx] : data;
        const bdg = (meta.solved ? ' <span style="color:#34d399;font-size:11px;">(✅ WCS)</span>' : '') + (meta.full_res ? ' <span style="color:#fbbf24;font-size:11px;">(🌟 HD Original)</span>' : '');
        iH += '<div class="panel-img-container"><span style="font-size:12px;color:#aaa;display:block;margin-bottom:4px;">Aufnahme ' + (idx+1) + bdg + '</span>';
        iH += '<img src="' + imgUrl + '" class="panel-img" onclick="openLightboxByIdx(' + idx + ')" />';
        iH += '<div><button class="atlas-btn" onclick="activateSkyOverlayByIndex(' + idx + ',true)">🌌 Im Himmel überblenden</button><button class="atlas-btn" onclick="openLightboxByIdx(' + idx + ')">🔍 Vollbild' + (meta.full_res ? ' (HD)' : '') + '</button><button class="atlas-btn" onclick="copyImageLink(' + idx + ',this)">🔗 Link kopieren</button></div></div>';
    }});
    document.getElementById('panel-images').innerHTML = iH;
    document.getElementById('side-panel').style.right = '0px';
    document.getElementById('bottom-controls').style.right = '394px';
    if (data.images.length > 0) activateSkyOverlayByIndex(0, zoomIn);
}}

function openLightboxByIdx(idx) {{
    if (!currentSelectedObj) return;
    const meta = (currentSelectedObj.image_meta && currentSelectedObj.image_meta[idx]) ? currentSelectedObj.image_meta[idx] : currentSelectedObj;
    const fullUrl = meta.full_url || currentSelectedObj.images[idx];
    openLightbox(fullUrl, currentSelectedObj.name);
}}

function openLightbox(url, title) {{
    document.getElementById('lightbox-img').src = url;
    document.getElementById('lightbox-caption').innerText = title;
    document.getElementById('lightbox').style.display = 'flex';
}}

function activateSkyOverlayByIndex(index, zoomToTarget) {{
    if (!currentSelectedObj) return;
    const data = currentSelectedObj, meta = (data.image_meta && data.image_meta[index]) ? data.image_meta[index] : data;
    activeOverlay.active = true; activeOverlay.name = data.name; activeOverlay.imgIndex = index;
    activeOverlay.ra = meta.ra; activeOverlay.dec = meta.dec;
    activeOverlay.fov = meta.fov || 1.0; activeOverlay.rot = meta.rot || 0.0;
    activeOverlay.baseFov = activeOverlay.fov; activeOverlay.baseRot = activeOverlay.rot;
    activeOverlay.flipped = !!meta.flipped; activeOverlay.solved = !!meta.solved;
    if (!activeOverlay.solved) {{
        try {{
            const sv = JSON.parse(localStorage.getItem('astro_ov_manual_' + data.name + '_' + index));
            if (sv) {{ if (typeof sv.rot === 'number') activeOverlay.rot = sv.rot; if (typeof sv.fov === 'number') activeOverlay.fov = sv.fov; }}
        }} catch(e) {{}}
    }}
    document.getElementById('sky-overlay-img').src = meta.url || data.images[index];
    document.getElementById('sky-overlay-caption').innerText = data.name;
    document.getElementById('ov-title').innerText = data.name;
    document.getElementById('ov-badge-wrap').innerHTML = activeOverlay.solved ? '<span class="ov-badge-solved">✅ Plate-Solved · Position, Maßstab &amp; Drehung exakt</span>' : '';
    document.getElementById('slider-rot').value = activeOverlay.rot;
    document.getElementById('slider-fov').value = activeOverlay.fov;
    document.getElementById('overlay-controls').style.display = 'block';
    document.getElementById('sky-overlay-anchor').style.display = 'block';
    updateOverlayParams(false);
    if (zoomToTarget) fitOverlayInView();
}}

function updateOverlayParams(saveManual) {{
    const op = parseFloat(document.getElementById('slider-opacity').value), rt = parseFloat(document.getElementById('slider-rot').value), fv = parseFloat(document.getElementById('slider-fov').value);
    activeOverlay.opacity = op / 100.0; activeOverlay.rot = rt; activeOverlay.fov = fv;
    document.getElementById('lbl-opacity').innerText = Math.round(op) + ' %';
    document.getElementById('lbl-rot').innerText = rt.toFixed(1) + '°';
    document.getElementById('lbl-fov').innerText = fv.toFixed(2) + '°';
    document.getElementById('sky-overlay-img').style.opacity = activeOverlay.opacity;
    if (saveManual && activeOverlay.name) {{
        try {{ localStorage.setItem('astro_ov_manual_' + activeOverlay.name + '_' + activeOverlay.imgIndex, JSON.stringify({{rot: activeOverlay.rot, fov: activeOverlay.fov}})); }} catch(e) {{}}
    }}
    scheduleRender();
}}

function stepRotation(d) {{ const s = document.getElementById('slider-rot'); let v = parseFloat(s.value) + d; if (v > 180) v -= 360; if (v < -180) v += 360; s.value = v; updateOverlayParams(true); }}
function resetRotation() {{ document.getElementById('slider-rot').value = activeOverlay.baseRot; document.getElementById('slider-fov').value = activeOverlay.baseFov; updateOverlayParams(false); }}
function stepFov(d) {{
    const s = document.getElementById('slider-fov');
    const cur = parseFloat(s.value);
    const step = d > 0 ? Math.max(0.05, cur * 0.08) : -Math.max(0.05, cur * 0.08);
    s.value = Math.max(0.15, Math.min(65.0, cur + step));
    updateOverlayParams(true);
}}
function fitOverlayInView() {{ if (activeOverlay.active) flyTo(activeOverlay.ra, activeOverlay.dec, Math.max(activeOverlay.fov * 2.4, 1.5)); }}
function toggleOverlayFrame() {{ activeOverlay.showFrame = !activeOverlay.showFrame; document.getElementById('sky-overlay-img').style.border = activeOverlay.showFrame ? '1.5px solid rgba(255,255,255,0.92)' : 'none'; document.getElementById('sky-overlay-caption').style.display = activeOverlay.showFrame ? 'block' : 'none'; }}
function closeSkyOverlay() {{ activeOverlay.active = false; document.getElementById('overlay-controls').style.display = 'none'; document.getElementById('sky-overlay-anchor').style.display = 'none'; scheduleRender(); }}
function zoomBy(f) {{ anim.active = false; cam.fov = Math.max(0.25, Math.min(115.0, cam.fov * f)); scheduleRender(); }}
function toggleConstellations() {{ cam.showConstellations = !cam.showConstellations; document.getElementById('btn-const').classList.toggle('active', cam.showConstellations); scheduleRender(); }}
function toggleMilkyWay() {{ cam.showMilkyWay = !cam.showMilkyWay; document.getElementById('btn-mw').classList.toggle('active', cam.showMilkyWay); scheduleRender(); }}
function toggleGrid() {{ cam.showGrid = !cam.showGrid; document.getElementById('btn-grid').classList.toggle('active', cam.showGrid); scheduleRender(); }}
function resetSkyView() {{ flyTo(10.68, 41.27, 60.0); }}

function copyImageLink(idx, btn) {{
    if (!currentSelectedObj) return;
    const baseUrl = window.location.href.split('?')[0].split('#')[0];
    const shareUrl = baseUrl + '?obj=' + encodeURIComponent(currentSelectedObj.name) + '&img=' + idx;
    const showCopied = () => {{
        if (!btn) return;
        const orig = btn.innerText;
        btn.innerText = '✅ Link kopiert!';
        setTimeout(() => {{ btn.innerText = orig; }}, 2000);
    }};
    if (navigator.clipboard && window.isSecureContext) {{
        navigator.clipboard.writeText(shareUrl).then(showCopied).catch(() => fallbackCopy(shareUrl, showCopied));
    }} else {{
        fallbackCopy(shareUrl, showCopied);
    }}
}}

function fallbackCopy(text, onSuccess) {{
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {{
        if (document.execCommand('copy')) {{
            onSuccess();
        }} else {{
            window.prompt('Link zum Kopieren (Strg+C):', text);
        }}
    }} catch (e) {{
        window.prompt('Link zum Kopieren (Strg+C):', text);
    }}
    document.body.removeChild(ta);
}}

function openFromUrlParams() {{
    const params = new URLSearchParams(window.location.search || window.location.hash.replace(/^#/, '?'));
    const qObj = params.get('obj');
    if (!qObj) {{
        scheduleRender();
        return;
    }}
    const norm = s => s.toLowerCase().replace(/[\\s_-]+/g, '');
    let match = myPhotos.find(p => p.name.toLowerCase() === qObj.toLowerCase());
    if (!match) match = myPhotos.find(p => norm(p.name) === norm(qObj));
    if (!match) match = myPhotos.find(p => p.name.toLowerCase().includes(qObj.toLowerCase()));
    if (!match) {{
        scheduleRender();
        return;
    }}
    let imgIdx = parseInt(params.get('img') || '0', 10);
    if (isNaN(imgIdx) || imgIdx < 0 || imgIdx >= match.images.length) imgIdx = 0;

    selectObject(match, false);
    activateSkyOverlayByIndex(imgIdx, false);
    cam.ra = activeOverlay.ra;
    cam.dec = activeOverlay.dec;
    cam.fov = Math.max(activeOverlay.fov * 2.4, 1.5);
    anim.active = false;
    scheduleRender();
}}

document.getElementById('sky-overlay-img').addEventListener('load', scheduleRender);
openFromUrlParams();
</script>
</body>
</html>
"""
            with open(output_html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            if for_web:
                self.logic.open_system_folder(web_base_dir)
                if callback:
                    callback(True, f"Website-Paket erstellt ({solved_cnt}/{total_cnt} Objekte gelöst)!")
            else:
                webbrowser.open(pathlib.Path(os.path.abspath(output_html_path)).as_uri())
                if callback:
                    callback(True, f"Himmelsatlas geöffnet ({solved_cnt}/{total_cnt} Objekte gelöst)!")
            return True

        except Exception as e:
            logging.error(f"Fehler beim Erstellen des Atlas: {e}\n{traceback.format_exc()}")
            if callback:
                callback(False, f"Kritischer Fehler: {e}")
            return False