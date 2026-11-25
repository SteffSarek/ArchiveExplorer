import os
import json
import logging
import webbrowser
import traceback
import re
import math
import subprocess
import numpy as np
from PIL import Image
from astropy.io import fits
from astropy.wcs import WCS
from utils import generate_pretty_name

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
            logging.warning(f"Konnte atlas_wcs_cache.json nicht speichern: {e}")

    def _extract_geometry_from_wcs_header(self, header, width_px, height_px, is_flipped_y=True):
        """Berechnet aus einem WCS-Header exakt Bildmitte (RA/Dec), Bildbreite (FOV in °), Drehwinkel und Spiegelung."""
        try:
            w = WCS(header, naxis=2)
            if not w.has_celestial:
                return None

            c_center = w.pixel_to_world(width_px / 2.0, height_px / 2.0)
            c_left   = w.pixel_to_world(0.0, height_px / 2.0)
            c_right  = w.pixel_to_world(width_px - 1.0, height_px / 2.0)
            y_top    = (height_px - 1.0) if is_flipped_y else 0.0
            c_top    = w.pixel_to_world(width_px / 2.0, y_top)

            ra_c  = float(c_center.ra.deg)
            dec_c = float(c_center.dec.deg)
            fov_w = float(c_left.separation(c_right).deg)

            if math.isnan(ra_c) or math.isnan(dec_c) or math.isnan(fov_w) or not (0.05 <= fov_w <= 30.0):
                return None

            pa_top   = float(c_center.position_angle(c_top).deg)
            pa_right = float(c_center.position_angle(c_right).deg)

            delta_pa = (pa_right - pa_top + 360.0) % 360.0
            flipped  = (delta_pa < 180.0)
            rot_deg  = ((-pa_top + 180.0) % 360.0) - 180.0

            return {
                "ra": round(ra_c, 5),
                "dec": round(dec_c, 5),
                "fov": round(fov_w, 4),
                "rot": round(rot_deg, 2),
                "flipped": bool(flipped),
                "solved": True
            }
        except Exception:
            return None

    def _solve_or_get_image_wcs(self, img_path, fits_sample, ra_hint, dec_hint, default_fov, use_astap=True, force_retry=False, log_cb=None, obj_label=""):
        """Holt die exakte Bildausrichtung aus dem Cache, löst mit ASTAP oder liest den FITS-Header."""
        norm_key = os.path.normpath(img_path).lower()
        try:
            mtime = int(os.path.getmtime(img_path))
        except Exception:
            mtime = 0

        cached = self.wcs_cache.get(norm_key)
        if cached and cached.get("mtime") == mtime:
            if cached.get("solved"):
                return cached
            if not force_retry and not use_astap:
                return cached
            if not force_retry and cached.get("astap_tried"):
                return cached

        astap_exe = self.logic.get_astap_path() if hasattr(self.logic, "get_astap_path") else ""
        can_use_astap = use_astap and astap_exe and os.path.exists(astap_exe)

        # 1. Direktes Plate-Solving des finalen Vorschau-Bildes (JPG/PNG) mit ASTAP
        if can_use_astap:
            temp_dir  = os.path.join(self.logic.app_path, "temp")
            os.makedirs(temp_dir, exist_ok=True)
            temp_fits = os.path.join(temp_dir, "temp_atlas_solve.fits")
            temp_wcs  = os.path.join(temp_dir, "temp_atlas_solve.wcs")

            try:
                if os.path.exists(temp_wcs):
                    os.remove(temp_wcs)

                with Image.open(img_path) as im:
                    im_gray = im.convert("L")
                    w_px, h_px = im_gray.size
                    if max(w_px, h_px) > 2200:
                        scale = 2000.0 / max(w_px, h_px)
                        w_px = int(w_px * scale)
                        h_px = int(h_px * scale)
                        im_gray = im_gray.resize((w_px, h_px))
                    arr_16 = np.array(im_gray, dtype=np.uint16) * 257

                fits.PrimaryHDU(data=np.flip(arr_16, axis=0)).writeto(temp_fits, overwrite=True)

                ra_hours = float(ra_hint) / 15.0
                spd_deg  = float(dec_hint) + 90.0

                for fov_try in [f"{default_fov:.2f}", "0"]:
                    if os.path.exists(temp_wcs):
                        try: os.remove(temp_wcs)
                        except: pass

                    cmd = [
                        astap_exe, "-f", temp_fits,
                        "-ra", f"{ra_hours:.4f}",
                        "-spd", f"{spd_deg:.4f}",
                        "-r", "30",
                        "-fov", fov_try
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                    if res.returncode == 0 and os.path.exists(temp_wcs):
                        clean_head = fits.Header()
                        with open(temp_wcs, "r", encoding="ascii", errors="ignore") as f_wcs:
                            for line in f_wcs:
                                l = line.strip("\r\n")
                                if "=" not in l or l.startswith(("COMMENT", "WARNING", "HISTORY", "SIMPLE", "BITPIX", "NAXIS")):
                                    continue
                                try:
                                    clean_head.append(fits.Card.fromstring(l.ljust(80)[:80]))
                                except Exception:
                                    pass

                        geom = self._extract_geometry_from_wcs_header(clean_head, w_px, h_px, is_flipped_y=True)
                        if geom:
                            geom["mtime"] = mtime
                            geom["astap_tried"] = True
                            self.wcs_cache[norm_key] = geom
                            if log_cb:
                                log_cb(f"🎯 ASTAP gelöst: {obj_label} ({os.path.basename(img_path)}) -> FOV {geom['fov']:.2f}°, Rot {geom['rot']:+.1f}°")
                            return geom
            except Exception as e:
                logging.debug(f"ASTAP Quick-Solve fehlgeschlagen für {img_path}: {e}")

        # 2. Fallback: FITS-Header im Ordner auf vorhandene WCS-Matrix prüfen
        if fits_sample and os.path.exists(fits_sample):
            try:
                with fits.open(fits_sample, ignore_missing_end=True, memmap=True) as hdul:
                    for hdu in hdul:
                        hdr = hdu.header
                        if ("CD1_1" in hdr or "CDELT1" in hdr or "PC1_1" in hdr) and ("CRVAL1" in hdr):
                            w_f = hdr.get("NAXIS1", 1080)
                            h_f = hdr.get("NAXIS2", 1920)
                            geom = self._extract_geometry_from_wcs_header(hdr, w_f, h_f, is_flipped_y=True)
                            if geom:
                                geom["mtime"] = mtime
                                geom["astap_tried"] = bool(can_use_astap)
                                self.wcs_cache[norm_key] = geom
                                return geom
            except Exception:
                pass

        # 3. Fallback: Katalog-/FITS-Mittelpunkt + Standard-Teleskop-FOV
        fallback = {
            "ra": round(float(ra_hint), 5),
            "dec": round(float(dec_hint), 5),
            "fov": round(float(default_fov), 4),
            "rot": 0.0,
            "flipped": False,
            "solved": False,
            "astap_tried": bool(can_use_astap),
            "mtime": mtime
        }
        self.wcs_cache[norm_key] = fallback
        return fallback

    def generate_atlas(self, callback=None, for_web=False, use_astap=True, force_retry=False, log_cb=None):
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

            atlas_objects_dict = {}
            date_pattern = re.compile(r'(202\d)[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])')
            
            total_items = len(self.logic.index)
            processed_count = 0
            solved_total = 0

            for key, data in self.logic.index.items():
                processed_count += 1
                pretty_name = generate_pretty_name(
                    data["original_name"], 
                    self.logic.caldwell_map, 
                    self.logic.reverse_caldwell_map, 
                    self.logic.common_names
                )
                pretty_name = re.sub(r'(?i)\s*(?:seestar\s*s\d+\s*(?:pro)?|dwarf\s*\d+|s30\s*(?:pro)?)\s*\d*$', '', pretty_name).strip()
                
                ignore_names = [
                    "bilder", "sonstiges", "export", "test", "misc", "allgemein", 
                    "mond", "moon", "jupiter", "saturn", "mars", "venus", "sonne", "sun"
                ]
                if pretty_name.lower().strip() in ignore_names:
                    continue
                    
                if data.get("path") and "xxx_" in data.get("path").lower():
                    continue
                
                obj_data = self.logic.get_object_data(key, exact_match=True)
                
                ra = obj_data.get("ra_decimal")
                dec = obj_data.get("dec_decimal")
                clean_k = self.logic._get_clean_key(pretty_name)
                
                if (ra is None or dec is None) and clean_k in self.logic.coord_cache:
                    ra = self.logic.coord_cache[clean_k]["ra"]
                    dec = self.logic.coord_cache[clean_k]["dec"]
                
                if ra is not None and dec is not None:
                    obj_dates = []
                    if data.get("path") and os.path.isdir(data["path"]):
                        try:
                            for file in os.listdir(data["path"]):
                                match = date_pattern.search(file)
                                if match:
                                    obj_dates.append(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
                        except Exception:
                            pass
                    
                    if not obj_dates and obj_data.get("date") and obj_data["date"] != "---":
                        try:
                            parts = obj_data["date"].split('.')
                            if len(parts) == 3:
                                obj_dates.append(f"{parts[2]}-{parts[1]}-{parts[0]}")
                        except Exception:
                            pass

                    imgs = obj_data.get("images", [])
                    if not imgs and obj_data.get("image"):
                        imgs = [obj_data.get("image")]

                    tel_type = data.get("telescope", "other")
                    if tel_type == "seestar": default_fov = 0.75
                    elif tel_type == "s30": default_fov = 1.60
                    elif tel_type == "dwarf": default_fov = 1.80
                    else: default_fov = 1.10

                    valid_exts = [".jpg", ".jpeg", ".png", ".gif", ".webp"]
                    clean_imgs = []
                    image_meta_list = []

                    for p in imgs:
                        if os.path.exists(p):
                            ext = os.path.splitext(p)[1].lower()
                            if ext in valid_exts:
                                if for_web:
                                    safe_obj = re.sub(r'[^a-zA-Z0-9_-]', '_', clean_k)
                                    orig_base = re.sub(r'[^a-zA-Z0-9_-]', '_', os.path.splitext(os.path.basename(p))[0])
                                    web_filename = f"{safe_obj}_{orig_base}.jpg"
                                    dest_img_path = os.path.join(web_img_dir, web_filename)
                                    
                                    if not os.path.exists(dest_img_path):
                                        try:
                                            with Image.open(p) as pil_img:
                                                rgb_img = pil_img.convert("RGB")
                                                rgb_img.thumbnail((1400, 1400))
                                                rgb_img.save(dest_img_path, "JPEG", quality=85, optimize=True)
                                        except Exception as e:
                                            logging.warning(f"Konnte Bild für Web nicht komprimieren ({p}): {e}")
                                            continue
                                            
                                    img_url = f"atlas_images/{web_filename}"
                                else:
                                    img_url = f"file:///{p.replace('\\', '/')}"

                                wcs_info = self._solve_or_get_image_wcs(
                                    p, data.get("fits_sample"), ra, dec, default_fov,
                                    use_astap=use_astap, force_retry=force_retry,
                                    log_cb=log_cb, obj_label=f"[{processed_count}/{total_items}] {pretty_name}"
                                )
                                if wcs_info.get("solved"):
                                    solved_total += 1

                                clean_imgs.append(img_url)
                                image_meta_list.append({
                                    "url": img_url,
                                    "ra": wcs_info["ra"],
                                    "dec": wcs_info["dec"],
                                    "fov": wcs_info["fov"],
                                    "rot": wcs_info["rot"],
                                    "flipped": wcs_info.get("flipped", False),
                                    "solved": wcs_info.get("solved", False)
                                })
                    
                    if not clean_imgs:
                        continue

                    otype = self.logic.determine_object_type(pretty_name, pretty_name)
                    first_meta = image_meta_list[0]
                    
                    if pretty_name not in atlas_objects_dict:
                        atlas_objects_dict[pretty_name] = {
                            "name": pretty_name,
                            "ra": first_meta["ra"],
                            "dec": first_meta["dec"],
                            "fov": first_meta["fov"],
                            "rot": first_meta["rot"],
                            "flipped": first_meta["flipped"],
                            "solved": first_meta["solved"],
                            "type": otype,
                            "images": clean_imgs,
                            "image_meta": image_meta_list,
                            "dates": obj_dates,
                            "wiki_url": obj_data.get("wiki_url", "")
                        }
                    else:
                        existing = atlas_objects_dict[pretty_name]
                        for idx_m, img_u in enumerate(clean_imgs):
                            if img_u not in existing["images"]:
                                existing["images"].append(img_u)
                                existing["image_meta"].append(image_meta_list[idx_m])
                                if image_meta_list[idx_m]["solved"] and not existing["solved"]:
                                    existing["ra"] = image_meta_list[idx_m]["ra"]
                                    existing["dec"] = image_meta_list[idx_m]["dec"]
                                    existing["fov"] = image_meta_list[idx_m]["fov"]
                                    existing["rot"] = image_meta_list[idx_m]["rot"]
                                    existing["flipped"] = image_meta_list[idx_m]["flipped"]
                                    existing["solved"] = True
                        existing["dates"].extend(obj_dates)

            self._save_wcs_cache()

            for obj in atlas_objects_dict.values():
                if obj["dates"]:
                    unique_dates = sorted(list(set(obj["dates"])))
                    p_first = unique_dates[0].split('-')
                    p_last = unique_dates[-1].split('-')
                    obj["first_obs"] = f"{p_first[2]}.{p_first[1]}.{p_first[0]}"
                    obj["last_obs"] = f"{p_last[2]}.{p_last[1]}.{p_last[0]}"
                else:
                    obj["first_obs"] = "---"
                    obj["last_obs"] = "---"
                del obj["dates"]

            atlas_objects = list(atlas_objects_dict.values())

            def sort_importance(obj):
                score = len(obj["images"]) * 100
                if "M " in obj["name"]: score += 50
                elif "NGC " in obj["name"]: score += 20
                return score
            atlas_objects.sort(key=sort_importance)

            if not atlas_objects:
                if callback: callback(False, "Keine aufgelösten Bilder im Archiv gefunden!")
                return False

            back_btn_text = "🏠 Zurück zu Astro-Nerd.info" if for_web else "✖ Atlas schließen"

            html_content = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Astro-Nerd.info | Interaktiver 3D-Himmelsatlas & Planerkarte</title>
    <style>
        body, html {{
            margin: 0; padding: 0; width: 100%; height: 100%;
            background-color: #05080f; color: #e2e8f0;
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            overflow: hidden; user-select: none;
        }}
        #sky-canvas {{
            position: absolute; top: 0; left: 0; width: 100vw; height: 100vh;
            cursor: grab; z-index: 100;
        }}
        #sky-canvas:active {{ cursor: grabbing; }}

        #top-bar {{
            position: absolute; top: 14px; left: 50%; transform: translateX(-50%); z-index: 1000;
            display: flex; align-items: center; gap: 14px;
            background: rgba(11, 17, 27, 0.88); padding: 8px 18px;
            border-radius: 10px; border: 1px solid rgba(56, 189, 248, 0.35);
            box-shadow: 0 6px 20px rgba(0,0,0,0.7); backdrop-filter: blur(8px);
        }}
        #btn-home {{
            background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.5);
            color: #38bdf8; border-radius: 7px; padding: 6px 12px; font-size: 12px;
            font-weight: 700; cursor: pointer; text-decoration: none; white-space: nowrap;
            transition: all 0.2s;
        }}
        #btn-home:hover {{ background: #38bdf8; color: #000; border-color: #38bdf8; }}
        #atlas-title h1 {{ margin: 0; font-size: 16px; color: #38bdf8; font-weight: 700; white-space: nowrap; }}
        #atlas-title span {{ font-size: 11px; color: #94a3b8; display: block; white-space: nowrap; }}
        #search-input {{
            background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.18);
            color: #fff; border-radius: 6px; padding: 6px 10px; font-size: 12px; width: 190px;
            outline: none;
        }}
        #search-input:focus {{ border-color: #38bdf8; background: rgba(255,255,255,0.1); }}

        #overlay-controls {{
            display: none; position: absolute; top: 78px; left: 14px; z-index: 1500;
            width: 320px; background: rgba(11, 17, 27, 0.90);
            border: 1px solid rgba(255, 255, 255, 0.14); border-radius: 10px;
            padding: 14px 16px; box-shadow: 0 8px 25px rgba(0,0,0,0.75);
            backdrop-filter: blur(8px); color: #e2e8f0;
        }}
        #ov-title {{
            font-size: 15px; font-weight: 700; color: #fff; margin-bottom: 4px;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }}
        #ov-sub {{
            font-size: 11px; color: #94a3b8; margin-bottom: 12px; line-height: 1.4;
        }}
        .ov-badge-solved {{
            display: inline-block; background: rgba(16, 185, 129, 0.18);
            border: 1px solid rgba(16, 185, 129, 0.5); color: #34d399;
            border-radius: 5px; padding: 2px 7px; font-size: 10.5px; font-weight: 700;
            margin-bottom: 5px;
        }}
        .ov-row {{
            background: rgba(255, 255, 255, 0.035); border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px; padding: 8px 10px; margin-bottom: 8px;
        }}
        .ov-label-row {{
            display: flex; justify-content: space-between; align-items: center;
            font-size: 12px; margin-bottom: 5px; color: #cbd5e1;
        }}
        .ov-val {{ font-weight: bold; color: #38bdf8; font-family: Consolas, monospace; }}
        .ov-slider-wrap {{ display: flex; align-items: center; gap: 6px; }}
        .ov-slider {{ flex: 1; accent-color: #38bdf8; cursor: pointer; height: 5px; }}
        .ov-mini-btn {{
            background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);
            color: #ddd; border-radius: 5px; padding: 2px 7px; font-size: 11px;
            cursor: pointer; transition: all 0.15s;
        }}
        .ov-mini-btn:hover {{ background: #38bdf8; color: #000; border-color: #38bdf8; }}
        .ov-actions {{ display: flex; gap: 6px; margin-top: 10px; }}
        .ov-action-btn {{
            flex: 1; background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.45);
            color: #e2e8f0; border-radius: 6px; padding: 6px 8px; font-size: 12px;
            font-weight: 600; cursor: pointer; transition: all 0.2s; text-align: center;
        }}
        .ov-action-btn:hover {{ background: #38bdf8; color: #000; }}
        .ov-action-btn.close {{
            flex: 0 0 auto; background: rgba(239, 68, 68, 0.18); border-color: rgba(239, 68, 68, 0.5);
        }}
        .ov-action-btn.close:hover {{ background: #ef4444; color: #fff; }}

        #sky-overlay-anchor {{
            display: none; position: absolute; top: 0; left: 0;
            pointer-events: none; z-index: 200;
            transform-origin: center center;
        }}
        #sky-overlay-box {{ position: relative; display: inline-block; }}
        #sky-overlay-caption {{
            position: absolute; top: -22px; left: 50%; transform: translateX(-50%);
            white-space: nowrap; font-size: 12px; font-weight: 700; color: #f8fafc;
            text-shadow: 0 1px 4px #000, 0 0 8px #000; letter-spacing: 0.3px;
        }}
        #sky-overlay-img {{
            display: block; width: 300px; height: auto;
            border: 1.5px solid rgba(255, 255, 255, 0.92);
            box-shadow: 0 0 30px rgba(0, 0, 0, 0.85);
        }}

        #bottom-status {{
            position: absolute; bottom: 14px; left: 14px; z-index: 1000;
            background: rgba(11, 17, 27, 0.88); border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 8px; padding: 7px 14px; font-size: 11.5px; color: #94a3b8;
            backdrop-filter: blur(6px); pointer-events: none;
        }}
        #bottom-status b {{ color: #e2e8f0; }}

        #bottom-controls {{
            position: absolute; bottom: 14px; right: 14px; z-index: 1000;
            display: flex; align-items: center; gap: 6px;
            background: rgba(11, 17, 27, 0.90); border: 1px solid rgba(255, 255, 255, 0.14);
            border-radius: 9px; padding: 6px 10px; backdrop-filter: blur(6px);
            transition: right 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
        }}
        .ctrl-btn {{
            background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.14);
            color: #e2e8f0; border-radius: 6px; padding: 5px 10px; font-size: 12px;
            cursor: pointer; transition: all 0.15s; font-weight: 600;
        }}
        .ctrl-btn:hover {{ background: #38bdf8; color: #000; border-color: #38bdf8; }}
        .ctrl-btn.active {{ background: rgba(56, 189, 248, 0.25); border-color: #38bdf8; color: #38bdf8; }}
        #fov-readout {{
            font-family: Consolas, monospace; font-size: 12px; color: #cbd5e1;
            min-width: 92px; text-align: center;
        }}

        #side-panel {{
            position: absolute; top: 0; right: -430px;
            width: 380px; max-width: 90vw; height: 100vh; background: rgba(11, 15, 23, 0.96);
            border-left: 1px solid rgba(56, 189, 248, 0.4); box-shadow: -5px 0 25px rgba(0,0,0,0.85);
            z-index: 2000; transition: right 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
            overflow-y: auto; padding: 22px; box-sizing: border-box; color: #fff;
        }}
        #close-panel {{
            float: right; background: none; border: none; color: #888; font-size: 24px; cursor: pointer; margin-top: -5px;
        }}
        #close-panel:hover {{ color: #ef4444; }}
        #panel-title {{ margin-top: 0; color: #38bdf8; font-size: 20px; margin-bottom: 5px; }}
        #panel-coords {{ font-size: 13px; color: #aaa; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #263040; line-height: 1.5; }}
        #panel-coords b {{ color: #e2e8f0; }}
        
        .panel-link-container {{ margin-top: 12px; margin-bottom: 5px; }}
        .atlas-btn {{
            display: inline-block; padding: 5px 10px; margin-right: 6px; margin-bottom: 5px;
            border-radius: 5px; border: 1px solid #38bdf8; color: #38bdf8;
            text-decoration: none; font-size: 12px; font-weight: 600; transition: all 0.2s; cursor: pointer;
            background: transparent;
        }}
        .atlas-btn:hover {{ background: #38bdf8; color: #000; }}
        
        .panel-img-container {{
            margin-bottom: 18px; text-align: center; background: rgba(255,255,255,0.03);
            padding: 10px; border-radius: 8px; border: 1px solid #1e293b;
        }}
        .panel-img {{
            max-width: 100%; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.6);
            cursor: zoom-in; transition: transform 0.2s; border: 1px solid #334155; margin-bottom: 8px;
        }}
        .panel-img:hover {{ transform: scale(1.02); border-color: #38bdf8; }}

        #lightbox {{
            display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.93); z-index: 5000; align-items: center; justify-content: center;
            flex-direction: column; cursor: zoom-out;
        }}
        #lightbox img {{
            max-width: 92vw; max-height: 88vh; border-radius: 6px;
            box-shadow: 0 0 30px rgba(0,0,0,0.9); border: 1px solid #444;
        }}
        #lightbox-caption {{ margin-top: 12px; color: #38bdf8; font-size: 16px; font-weight: bold; }}
    </style>
</head>
<body>

    <div id="top-bar">
        <button id="btn-home" onclick="exitAtlas()" title="Zurück zur Hauptseite / Vollbild">{back_btn_text}</button>
        <div id="atlas-title">
            <h1>Mein Himmelsatlas</h1>
            <span>Lokale Planerkarte · {len(atlas_objects)} eigene Katalogobjekte</span>
        </div>
        <input type="text" id="search-input" list="obj-datalist" placeholder="🔍 Objekt suchen (z.B. M 31)..." onchange="onSearchSelect(this.value)" />
        <datalist id="obj-datalist"></datalist>
    </div>

    <div id="overlay-controls">
        <div id="ov-title">Objektname</div>
        <div id="ov-badge-wrap"></div>
        <div id="ov-sub">Bild mit Rahmen eingepasst · Maßstabsgetreu · Ziehen: verschieben · Mausrad: zoomen · Deckkraft & Drehung regelbar</div>

        <div class="ov-row">
            <div class="ov-label-row">
                <span>Deckkraft</span>
                <span class="ov-val" id="lbl-opacity">100 %</span>
            </div>
            <div class="ov-slider-wrap">
                <input type="range" id="slider-opacity" class="ov-slider" min="0" max="100" value="100" oninput="updateOverlayParams(false)">
            </div>
        </div>

        <div class="ov-row">
            <div class="ov-label-row">
                <span>Drehung</span>
                <span class="ov-val" id="lbl-rot">0°</span>
            </div>
            <div class="ov-slider-wrap">
                <button class="ov-mini-btn" onclick="stepRotation(-1)" title="-1°">↶</button>
                <input type="range" id="slider-rot" class="ov-slider" min="-180" max="180" step="0.2" value="0" oninput="updateOverlayParams(true)">
                <button class="ov-mini-btn" onclick="stepRotation(1)" title="+1°">↷</button>
                <button class="ov-mini-btn" onclick="resetRotation()" title="Auf ASTAP-Winkel zurücksetzen">↺</button>
            </div>
        </div>

        <div class="ov-row">
            <div class="ov-label-row">
                <span>Bildfeld (Größe)</span>
                <span class="ov-val" id="lbl-fov">1.00°</span>
            </div>
            <div class="ov-slider-wrap">
                <button class="ov-mini-btn" onclick="stepFov(-0.05)">-</button>
                <input type="range" id="slider-fov" class="ov-slider" min="0.15" max="8.0" step="0.02" value="1.0" oninput="updateOverlayParams(true)">
                <button class="ov-mini-btn" onclick="stepFov(0.05)">+</button>
            </div>
        </div>

        <div class="ov-actions">
            <button class="ov-action-btn" onclick="fitOverlayInView()">🎯 Bild einpassen</button>
            <button class="ov-action-btn" onclick="toggleOverlayFrame()">🖼️ Rahmen</button>
            <button class="ov-action-btn close" onclick="closeSkyOverlay()" title="Overlay ausblenden">✖</button>
        </div>
    </div>

    <div id="sky-overlay-anchor">
        <div id="sky-overlay-box">
            <div id="sky-overlay-caption"></div>
            <img id="sky-overlay-img" src="" alt="Sky Overlay" />
        </div>
    </div>

    <div id="bottom-status">
        <b id="status-obj-lbl">Gesamter Himmel</b> · Sky Atlas · Lokale Planerkarte · <b>{len(atlas_objects)} Katalogobjekte</b> · Echte Hauptsterne (J2000) & Feldsterne
    </div>

    <div id="bottom-controls">
        <button class="ctrl-btn" onclick="zoomBy(1.3)" title="Herauszoomen">−</button>
        <span id="fov-readout">65,0° × 65,0°</span>
        <button class="ctrl-btn" onclick="zoomBy(0.75)" title="Hineinzoomen">+</button>
        <button class="ctrl-btn" onclick="fitOverlayInView()">Bild einpassen</button>
        <button class="ctrl-btn active" id="btn-const" onclick="toggleConstellations()" title="Sternbilder & Hauptstern-Namen ein/aus">✨</button>
        <button class="ctrl-btn active" id="btn-mw" onclick="toggleMilkyWay()" title="Milchstraßen-Band ein/aus">🌌</button>
        <button class="ctrl-btn active" id="btn-grid" onclick="toggleGrid()" title="RA/Dec-Gitter ein/aus">⊞</button>
        <button class="ctrl-btn" onclick="resetSkyView()" title="Gesamten Himmel zeigen">↺</button>
    </div>

    <div id="side-panel">
        <button id="close-panel" onclick="document.getElementById('side-panel').style.right = '-430px'; document.getElementById('bottom-controls').style.right = '14px';">✖</button>
        <h2 id="panel-title">Objektname</h2>
        <div id="panel-coords"></div>
        <div id="panel-images"></div>
    </div>

    <div id="lightbox" onclick="this.style.display='none';">
        <img id="lightbox-img" src="" alt="Großansicht" />
        <div id="lightbox-caption"></div>
    </div>

    <canvas id="sky-canvas"></canvas>

    <script>
        const myPhotos = {json.dumps(atlas_objects, indent=4)};
        const canvas = document.getElementById('sky-canvas');
        const ctx = canvas.getContext('2d');
        let currentSelectedObj = null;

        const isEmbeddedInWordPress = (window.self !== window.top);
        if (isEmbeddedInWordPress) {{
            document.getElementById('btn-home').innerText = '⛶ Vollbild';
            document.getElementById('btn-home').title = 'Atlas im Vollbildmodus umschalten';
        }}

        function exitAtlas() {{
            if (isEmbeddedInWordPress) {{
                if (!document.fullscreenElement) {{
                    document.documentElement.requestFullscreen().catch(() => {{
                        window.open(window.location.href, '_blank');
                    }});
                }} else {{
                    document.exitFullscreen();
                }}
            }} else if (window.location.protocol === 'file:') {{
                window.close();
                setTimeout(() => {{ window.location.href = 'https://astro-nerd.info/'; }}, 200);
            }} else {{
                window.top.location.href = 'https://astro-nerd.info/';
            }}
        }}

        let cam = {{
            ra: 10.68,
            dec: 41.27,
            fov: 65.0,
            showGrid: true,
            showMilkyWay: true,
            showConstellations: true
        }};

        let anim = {{ active: false, startRa: 0, startDec: 0, startFov: 60, targetRa: 0, targetDec: 0, targetFov: 5, t0: 0, dur: 650 }};

        let activeOverlay = {{
            active: false,
            name: "",
            ra: 0,
            dec: 0,
            fov: 1.0,
            rot: 0,
            baseFov: 1.0,
            baseRot: 0,
            flipped: false,
            solved: false,
            opacity: 1.0,
            showFrame: true
        }};

        const DEG2RAD = Math.PI / 180.0;
        const RAD2DEG = 180.0 / Math.PI;

        function gal2eq(lDeg, bDeg) {{
            const aNGP = 192.85948 * DEG2RAD;
            const dNGP = 27.12825 * DEG2RAD;
            const lNCP = 122.93192 * DEG2RAD;
            const l = lDeg * DEG2RAD;
            const b = bDeg * DEG2RAD;

            const sinB = Math.sin(b), cosB = Math.cos(b);
            const sinD = Math.sin(dNGP), cosD = Math.cos(dNGP);
            const dL = lNCP - l;
            const sinDL = Math.sin(dL), cosDL = Math.cos(dL);

            const sinDec = sinD * sinB + cosD * cosB * cosDL;
            const dec = Math.asin(Math.max(-1, Math.min(1, sinDec)));

            const y = cosB * sinDL;
            const x = cosD * sinB - sinD * cosB * cosDL;
            let ra = (aNGP + Math.atan2(y, x)) * RAD2DEG;
            ra = ((ra % 360) + 360) % 360;
            return [ra, dec * RAD2DEG];
        }}

        const milkyWaySpine = [];
        for (let l = 0; l <= 360; l += 2) {{
            const [ra, dec] = gal2eq(l, 0);
            milkyWaySpine.push({{ l, ra, dec }});
        }}

        const brightStars = [
            {{ name: "Beteigeuze", ra: 88.79, dec: 7.41, mag: 0.45, col: "#ffaa60" }},
            {{ name: "Rigel", ra: 78.63, dec: -8.20, mag: 0.18, col: "#bfdfff" }},
            {{ name: "Bellatrix", ra: 81.28, dec: 6.35, mag: 1.64, col: "#cce6ff" }},
            {{ name: "Alnilam", ra: 84.05, dec: -1.20, mag: 1.69, col: "#cce6ff" }},
            {{ name: "Alnitak", ra: 85.19, dec: -1.94, mag: 1.77, col: "#cce6ff" }},
            {{ name: "Mintaka", ra: 83.00, dec: -0.30, mag: 2.23, col: "#cce6ff" }},
            {{ name: "Saiph", ra: 86.94, dec: -9.67, mag: 2.06, col: "#cce6ff" }},
            {{ name: "Meissa", ra: 83.78, dec: 9.93, mag: 3.39, col: "#d8ecff" }},
            {{ name: "Hatysa (ι Ori)", ra: 83.86, dec: -5.91, mag: 2.77, col: "#d8ecff" }},
            {{ name: "Sirius", ra: 101.29, dec: -16.72, mag: -1.46, col: "#e0f2ff" }},
            {{ name: "Aldebaran", ra: 68.98, dec: 16.51, mag: 0.87, col: "#ffb066" }},
            {{ name: "Elnath", ra: 81.57, dec: 28.61, mag: 1.65, col: "#cce6ff" }},
            {{ name: "Capella", ra: 79.17, dec: 46.00, mag: 0.08, col: "#fff0b8" }},
            {{ name: "Menkalinan", ra: 89.88, dec: 44.95, mag: 1.90, col: "#e8f4ff" }},
            {{ name: "Procyon", ra: 114.83, dec: 5.22, mag: 0.34, col: "#fff6d8" }},
            {{ name: "Pollux", ra: 116.33, dec: 28.03, mag: 1.14, col: "#ffc885" }},
            {{ name: "Castor", ra: 113.65, dec: 31.89, mag: 1.58, col: "#e8f4ff" }},
            {{ name: "Alhena", ra: 99.43, dec: 16.40, mag: 1.93, col: "#e8f4ff" }},
            {{ name: "Wega", ra: 279.23, dec: 38.78, mag: 0.03, col: "#d8ecff" }},
            {{ name: "Deneb", ra: 310.36, dec: 45.28, mag: 1.25, col: "#e0f0ff" }},
            {{ name: "Sadr", ra: 305.56, dec: 40.26, mag: 2.23, col: "#fff2cc" }},
            {{ name: "Albireo", ra: 292.68, dec: 27.96, mag: 3.05, col: "#ffd28a" }},
            {{ name: "Gienah (ε Cyg)", ra: 311.55, dec: 33.97, mag: 2.48, col: "#ffcc88" }},
            {{ name: "Altair", ra: 297.70, dec: 8.87, mag: 0.76, col: "#eef6ff" }},
            {{ name: "Schedar", ra: 10.13, dec: 56.54, mag: 2.24, col: "#ffcc88" }},
            {{ name: "Caph", ra: 2.29, dec: 59.15, mag: 2.28, col: "#fff4d8" }},
            {{ name: "Gamma Cas", ra: 14.18, dec: 60.72, mag: 2.15, col: "#bfdfff" }},
            {{ name: "Ruchbah", ra: 21.45, dec: 60.24, mag: 2.68, col: "#e8f4ff" }},
            {{ name: "Segin", ra: 28.60, dec: 63.67, mag: 3.35, col: "#cce6ff" }},
            {{ name: "Mirfak", ra: 51.08, dec: 49.86, mag: 1.79, col: "#fff2cc" }},
            {{ name: "Algol", ra: 47.04, dec: 40.96, mag: 2.12, col: "#d8ecff" }},
            {{ name: "Alpheratz", ra: 2.09, dec: 29.09, mag: 2.07, col: "#d8ecff" }},
            {{ name: "Mirach", ra: 17.43, dec: 35.62, mag: 2.07, col: "#ffaa66" }},
            {{ name: "Almach", ra: 30.97, dec: 42.33, mag: 2.10, col: "#ffcc80" }},
            {{ name: "Enif", ra: 326.05, dec: 9.88, mag: 2.38, col: "#ffbb77" }},
            {{ name: "Markab", ra: 346.19, dec: 15.21, mag: 2.49, col: "#d8ecff" }},
            {{ name: "Scheat", ra: 345.94, dec: 28.08, mag: 2.44, col: "#ffaa60" }},
            {{ name: "Algenib", ra: 3.31, dec: 15.18, mag: 2.83, col: "#cce6ff" }},
            {{ name: "Polaris", ra: 37.95, dec: 89.26, mag: 1.97, col: "#fff4cc" }},
            {{ name: "Kochab", ra: 222.68, dec: 74.16, mag: 2.07, col: "#ffbb77" }},
            {{ name: "Dubhe", ra: 165.93, dec: 61.75, mag: 1.79, col: "#ffcc88" }},
            {{ name: "Merak", ra: 165.46, dec: 56.38, mag: 2.37, col: "#e8f4ff" }},
            {{ name: "Phecda", ra: 178.46, dec: 53.69, mag: 2.44, col: "#e8f4ff" }},
            {{ name: "Megrez", ra: 183.86, dec: 57.03, mag: 3.32, col: "#e8f4ff" }},
            {{ name: "Alioth", ra: 193.51, dec: 55.96, mag: 1.76, col: "#e8f4ff" }},
            {{ name: "Mizar", ra: 200.98, dec: 54.93, mag: 2.23, col: "#e8f4ff" }},
            {{ name: "Alkaid", ra: 206.89, dec: 49.31, mag: 1.85, col: "#bfdfff" }},
            {{ name: "Arcturus", ra: 213.92, dec: 19.18, mag: -0.05, col: "#ffb86c" }},
            {{ name: "Spica", ra: 201.30, dec: -11.16, mag: 0.98, col: "#bfdfff" }},
            {{ name: "Regulus", ra: 152.09, dec: 11.97, mag: 1.36, col: "#d8ecff" }},
            {{ name: "Algieba", ra: 154.99, dec: 19.84, mag: 2.01, col: "#ffcc80" }},
            {{ name: "Denebola", ra: 177.26, dec: 14.57, mag: 2.14, col: "#e8f4ff" }},
            {{ name: "Antares", ra: 247.35, dec: -26.43, mag: 1.06, col: "#ff8c44" }},
            {{ name: "Shaula", ra: 263.40, dec: -37.10, mag: 1.62, col: "#bfdfff" }},
            {{ name: "Kaus Australis", ra: 276.04, dec: -34.38, mag: 1.79, col: "#d8ecff" }},
            {{ name: "Nunki", ra: 283.82, dec: -26.30, mag: 2.05, col: "#bfdfff" }},
            {{ name: "Fomalhaut", ra: 344.41, dec: -29.62, mag: 1.16, col: "#e8f4ff" }},
            {{ name: "Deneb Kaitos", ra: 10.90, dec: -17.99, mag: 2.04, col: "#ffcc80" }},
            {{ name: "Menkar", ra: 45.57, dec: 4.09, mag: 2.54, col: "#ffaa60" }},
            {{ name: "Alderamin", ra: 319.64, dec: 62.59, mag: 2.45, col: "#f0f6ff" }},
            {{ name: "Eltanin", ra: 269.15, dec: 51.49, mag: 2.24, col: "#ffbb77" }}
        ];

        const constellations = [
            {{ name: "Orion", ra: 83.8, dec: 3.2, lines: [
                [88.79,7.41, 83.78,9.93], [83.78,9.93, 81.28,6.35], [88.79,7.41, 81.28,6.35],
                [81.28,6.35, 83.00,-0.30], [83.00,-0.30, 84.05,-1.20], [84.05,-1.20, 85.19,-1.94],
                [85.19,-1.94, 88.79,7.41], [83.00,-0.30, 78.63,-8.20], [78.63,-8.20, 86.94,-9.67],
                [86.94,-9.67, 85.19,-1.94], [84.05,-1.20, 83.86,-5.91]
            ]}},
            {{ name: "Taurus (Stier)", ra: 68.0, dec: 19.0, lines: [
                [68.98,16.51, 81.57,28.61], [68.98,16.51, 67.15,19.18], [67.15,19.18, 84.41,21.14],
                [68.98,16.51, 64.95,15.63], [64.95,15.63, 56.87,24.11]
            ]}},
            {{ name: "Auriga (Fuhrmann)", ra: 84.0, dec: 42.0, lines: [
                [79.17,46.00, 89.88,44.95], [89.88,44.95, 89.93,37.21], [89.93,37.21, 81.57,28.61],
                [81.57,28.61, 74.25,33.17], [74.25,33.17, 79.17,46.00]
            ]}},
            {{ name: "Gemini (Zwillinge)", ra: 108.0, dec: 24.0, lines: [
                [113.65,31.89, 116.33,28.03], [113.65,31.89, 100.98,25.13], [100.98,25.13, 95.74,22.51],
                [116.33,28.03, 106.03,20.57], [106.03,20.57, 99.43,16.40]
            ]}},
            {{ name: "Canis Major", ra: 104.0, dec: -22.0, lines: [
                [101.29,-16.72, 95.67,-17.96], [101.29,-16.72, 107.10,-26.39], [107.10,-26.39, 104.66,-28.97],
                [107.10,-26.39, 111.02,-29.30]
            ]}},
            {{ name: "Cygnus (Schwan)", ra: 307.0, dec: 42.0, lines: [
                [310.36,45.28, 305.56,40.26], [305.56,40.26, 292.68,27.96],
                [296.24,45.13, 305.56,40.26], [305.56,40.26, 311.55,33.97]
            ]}},
            {{ name: "Lyra (Leier)", ra: 282.0, dec: 36.0, lines: [
                [279.23,38.78, 283.63,32.69], [283.63,32.69, 284.74,32.69], [284.74,32.69, 284.05,36.90],
                [284.05,36.90, 279.23,38.78]
            ]}},
            {{ name: "Aquila (Adler)", ra: 296.0, dec: 6.0, lines: [
                [296.56,10.61, 297.70,8.87], [297.70,8.87, 298.83,6.41], [297.70,8.87, 291.37,3.11],
                [291.37,3.11, 286.35,13.86], [291.37,3.11, 302.83,-0.82]
            ]}},
            {{ name: "Cassiopeia", ra: 15.0, dec: 60.0, lines: [
                [2.29,59.15, 10.13,56.54], [10.13,56.54, 14.18,60.72],
                [14.18,60.72, 21.45,60.24], [21.45,60.24, 28.60,63.67]
            ]}},
            {{ name: "Cepheus", ra: 332.0, dec: 68.0, lines: [
                [319.64,62.59, 322.16,70.56], [322.16,70.56, 354.84,77.63], [354.84,77.63, 341.97,66.20],
                [341.97,66.20, 332.71,58.20], [332.71,58.20, 319.64,62.59]
            ]}},
            {{ name: "Perseus", ra: 51.0, dec: 45.0, lines: [
                [43.56,55.90, 51.08,49.86], [51.08,49.86, 47.04,40.96], [51.08,49.86, 56.08,47.79],
                [56.08,47.79, 58.53,40.01], [58.53,40.01, 57.59,31.88]
            ]}},
            {{ name: "Andromeda", ra: 14.0, dec: 38.0, lines: [
                [2.09,29.09, 9.83,30.86], [9.83,30.86, 17.43,35.62], [17.43,35.62, 30.97,42.33]
            ]}},
            {{ name: "Pegasus", ra: 340.0, dec: 20.0, lines: [
                [2.09,29.09, 345.94,28.08], [345.94,28.08, 346.19,15.21], [346.19,15.21, 3.31,15.18],
                [3.31,15.18, 2.09,29.09], [346.19,15.21, 340.37,10.83], [340.37,10.83, 326.05,9.88]
            ]}},
            {{ name: "Ursa Major (Großer Bär)", ra: 185.0, dec: 55.0, lines: [
                [165.93,61.75, 165.46,56.38], [165.46,56.38, 178.46,53.69], [178.46,53.69, 183.86,57.03],
                [183.86,57.03, 165.93,61.75], [183.86,57.03, 193.51,55.96], [193.51,55.96, 200.98,54.93],
                [200.98,54.93, 206.89,49.31]
            ]}},
            {{ name: "Ursa Minor (Kleiner Bär)", ra: 230.0, dec: 78.0, lines: [
                [37.95,89.26, 263.05,86.59], [263.05,86.59, 251.49,82.04], [251.49,82.04, 236.01,77.79],
                [236.01,77.79, 222.68,74.16], [222.68,74.16, 230.18,71.83], [230.18,71.83, 244.38,75.76],
                [244.38,75.76, 236.01,77.79]
            ]}},
            {{ name: "Boötes (Bärenhüter)", ra: 218.0, dec: 30.0, lines: [
                [213.92,19.18, 221.25,27.07], [221.25,27.07, 225.49,40.39], [225.49,40.39, 218.02,38.31],
                [218.02,38.31, 213.92,19.18]
            ]}},
            {{ name: "Hercules", ra: 255.0, dec: 35.0, lines: [
                [247.55,21.49, 250.32,31.60], [250.32,31.60, 250.42,38.92], [250.42,38.92, 260.03,36.81],
                [260.03,36.81, 254.46,29.83], [254.46,29.83, 250.32,31.60]
            ]}},
            {{ name: "Leo (Löwe)", ra: 160.0, dec: 18.0, lines: [
                [152.09,11.97, 154.99,19.84], [154.99,19.84, 154.17,23.42], [154.17,23.42, 148.19,26.01],
                [154.99,19.84, 168.53,20.52], [168.53,20.52, 177.26,14.57], [177.26,14.57, 168.56,15.43],
                [168.56,15.43, 152.09,11.97]
            ]}},
            {{ name: "Virgo (Jungfrau)", ra: 195.0, dec: -3.0, lines: [
                [201.30,-11.16, 190.42,-1.45], [190.42,-1.45, 193.90,3.40], [193.90,3.40, 195.54,10.96],
                [190.42,-1.45, 184.98,-0.67], [184.98,-0.67, 177.67,1.76]
            ]}},
            {{ name: "Scorpius (Skorpion)", ra: 252.0, dec: -28.0, lines: [
                [241.36,-19.81, 240.08,-22.62], [240.08,-22.62, 247.35,-26.43], [247.35,-26.43, 252.97,-34.29],
                [252.97,-34.29, 253.08,-38.05], [253.08,-38.05, 264.33,-43.00], [264.33,-43.00, 265.62,-39.03],
                [265.62,-39.03, 263.40,-37.10]
            ]}},
            {{ name: "Sagittarius (Schütze)", ra: 282.0, dec: -27.0, lines: [
                [276.04,-34.38, 275.25,-29.83], [275.25,-29.83, 276.99,-25.42], [276.99,-25.42, 283.82,-26.30],
                [283.82,-26.30, 286.74,-29.88], [286.74,-29.88, 276.04,-34.38], [275.25,-29.83, 271.45,-30.42]
            ]}}
        ];

        const starSet = new Set(brightStars.map(s => Math.round(s.ra) + '_' + Math.round(s.dec)));
        constellations.forEach(c => {{
            c.lines.forEach(ln => {{
                [[ln[0], ln[1]], [ln[2], ln[3]]].forEach(pt => {{
                    const key = Math.round(pt[0]) + '_' + Math.round(pt[1]);
                    if (!starSet.has(key)) {{
                        starSet.add(key);
                        brightStars.push({{ name: "", ra: pt[0], dec: pt[1], mag: 2.5, col: "#e2f0ff" }});
                    }}
                }});
            }});
        }});

        const dl = document.getElementById('obj-datalist');
        myPhotos.forEach(p => {{
            const opt = document.createElement('option');
            opt.value = p.name;
            dl.appendChild(opt);
        }});

        function onSearchSelect(val) {{
            const found = myPhotos.find(p => p.name.toLowerCase().includes(val.toLowerCase()));
            if (found) {{
                selectObject(found, true);
                document.getElementById('search-input').blur();
            }}
        }}

        function world2pix(raDeg, decDeg) {{
            const dRa = (raDeg - cam.ra) * DEG2RAD;
            const dec = decDeg * DEG2RAD;
            const dec0 = cam.dec * DEG2RAD;

            const sinDec = Math.sin(dec), cosDec = Math.cos(dec);
            const sinDec0 = Math.sin(dec0), cosDec0 = Math.cos(dec0);
            const cosDra = Math.cos(dRa), sinDra = Math.sin(dRa);

            const cosC = sinDec0 * sinDec + cosDec0 * cosDec * cosDra;
            if (cosC <= -0.15) return null;

            const k = 2.0 / (1.0 + cosC);
            const xProj = -k * cosDec * sinDra;
            const yProj = k * (cosDec0 * sinDec - sinDec0 * cosDec * cosDra);

            const scale = Math.min(canvas.width, canvas.height) / (2.0 * Math.tan(Math.min(130, cam.fov) * DEG2RAD / 2.0));
            return [
                canvas.width * 0.5 + xProj * scale,
                canvas.height * 0.5 - yProj * scale
            ];
        }}

        function hash2D(ix, iy) {{
            let n = Math.sin(ix * 127.1 + iy * 311.7) * 43758.5453123;
            return n - Math.floor(n);
        }}

        function formatRA(raDeg) {{
            let r = ((raDeg % 360) + 360) % 360;
            let totalMin = (r / 15.0) * 60.0;
            let h = Math.floor(totalMin / 60);
            let m = Math.round(totalMin % 60);
            if (m === 60) {{ h = (h + 1) % 24; m = 0; }}
            return 'RA ' + h + 'h ' + (m < 10 ? '0' + m : m) + 'm';
        }}

        function formatDec(decDeg) {{
            let sign = decDeg >= 0 ? '+' : '−';
            let a = Math.abs(decDeg);
            let d = Math.floor(a);
            let m = Math.round((a - d) * 60);
            if (m === 60) {{ d += 1; m = 0; }}
            if (m === 0) return 'Dec ' + sign + d + '°';
            return 'Dec ' + sign + d + '° ' + (m < 10 ? '0' + m : m) + "'";
        }}

        function drawMilkyWayBand() {{
            const pxPerDeg = Math.min(canvas.width, canvas.height) / cam.fov;
            const fade = cam.fov < 5.0 ? Math.max(0.2, cam.fov / 5.0) : 1.0;

            ctx.save();
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';

            const bands = [
                {{ degWidth: 22.0, color: `rgba(110, 155, 230, ${{(0.045 * fade).toFixed(3)}})` }},
                {{ degWidth: 13.0, color: `rgba(140, 185, 245, ${{(0.065 * fade).toFixed(3)}})` }},
                {{ degWidth: 6.5,  color: `rgba(186, 225, 255, ${{(0.075 * fade).toFixed(3)}})` }}
            ];

            bands.forEach(b => {{
                ctx.beginPath();
                ctx.strokeStyle = b.color;
                ctx.lineWidth = Math.min(700, b.degWidth * pxPerDeg);
                let prev = null;
                for (let i = 0; i < milkyWaySpine.length; i++) {{
                    const pt = milkyWaySpine[i];
                    const p = world2pix(pt.ra, pt.dec);
                    if (p && p[0] > -400 && p[0] < canvas.width + 400 && p[1] > -400 && p[1] < canvas.height + 400) {{
                        if (!prev || Math.hypot(p[0] - prev[0], p[1] - prev[1]) > 350) {{
                            ctx.moveTo(p[0], p[1]);
                        }} else {{
                            ctx.lineTo(p[0], p[1]);
                        }}
                        prev = p;
                    }} else {{
                        prev = null;
                    }}
                }}
                ctx.stroke();
            }});

            ctx.setLineDash([6, 6]);
            ctx.lineWidth = 1.1;
            ctx.strokeStyle = `rgba(147, 197, 253, ${{(0.22 * fade).toFixed(3)}})`;
            ctx.beginPath();
            let prev = null;
            for (let i = 0; i < milkyWaySpine.length; i++) {{
                const pt = milkyWaySpine[i];
                const p = world2pix(pt.ra, pt.dec);
                if (p && p[0] > -200 && p[0] < canvas.width + 200 && p[1] > -200 && p[1] < canvas.height + 200) {{
                    if (!prev || Math.hypot(p[0] - prev[0], p[1] - prev[1]) > 250) ctx.moveTo(p[0], p[1]);
                    else ctx.lineTo(p[0], p[1]);
                    prev = p;
                }} else prev = null;
            }}
            ctx.stroke();
            ctx.setLineDash([]);

            if (cam.fov > 15) {{
                ctx.font = 'italic 600 12px "Segoe UI", sans-serif';
                ctx.fillStyle = 'rgba(147, 197, 253, 0.42)';
                const mwLabels = [
                    {{ l: 0, text: "★ Galaktisches Zentrum (Milchstraße)" }},
                    {{ l: 85, text: "Milchstraße (Cygnus)" }},
                    {{ l: 135, text: "Milchstraße (Cassiopeia)" }},
                    {{ l: 205, text: "Milchstraße (Monoceros)" }}
                ];
                mwLabels.forEach(lbl => {{
                    const [ra, dec] = gal2eq(lbl.l, 0);
                    const p = world2pix(ra, dec);
                    if (p && p[0] > 60 && p[0] < canvas.width - 140 && p[1] > 60 && p[1] < canvas.height - 60) {{
                        ctx.fillText(lbl.text, p[0] + 8, p[1] - 8);
                    }}
                }});
            }}

            ctx.restore();
        }}

        function renderSky(timestamp) {{
            if (canvas.width !== window.innerWidth || canvas.height !== window.innerHeight) {{
                canvas.width = window.innerWidth;
                canvas.height = window.innerHeight;
            }}

            if (anim.active) {{
                let t = Math.min(1.0, (timestamp - anim.t0) / anim.dur);
                let ease = t < 0.5 ? 2*t*t : -1+(4-2*t)*t;
                let dRa = ((anim.targetRa - anim.startRa + 540) % 360) - 180;
                cam.ra = (anim.startRa + dRa * ease + 360) % 360;
                cam.dec = anim.startDec + (anim.targetDec - anim.startDec) * ease;
                cam.fov = anim.startFov * Math.pow(anim.targetFov / anim.startFov, ease);
                if (t >= 1.0) anim.active = false;
            }}

            const grad = ctx.createRadialGradient(canvas.width*0.5, canvas.height*0.5, 50, canvas.width*0.5, canvas.height*0.5, Math.max(canvas.width, canvas.height)*0.7);
            grad.addColorStop(0, '#0a101d');
            grad.addColorStop(1, '#04060b');
            ctx.fillStyle = grad;
            ctx.fillRect(0, 0, canvas.width, canvas.height);

            if (cam.showMilkyWay) {{
                drawMilkyWayBand();
            }}

            if (cam.showGrid) {{
                let decStep = 10, raStep = 15;
                if (cam.fov < 3.5) {{ decStep = 0.5; raStep = 0.5; }}
                else if (cam.fov < 10) {{ decStep = 1; raStep = 1.25; }}
                else if (cam.fov < 25) {{ decStep = 2; raStep = 3.75; }}
                else if (cam.fov < 50) {{ decStep = 5; raStep = 7.5; }}

                ctx.lineWidth = 1;
                ctx.strokeStyle = 'rgba(71, 118, 168, 0.24)';
                ctx.fillStyle = 'rgba(148, 184, 218, 0.65)';
                ctx.font = '11px "Segoe UI", sans-serif';

                const minDec = Math.max(-85, Math.floor((cam.dec - cam.fov*0.8) / decStep) * decStep);
                const maxDec = Math.min(85, Math.ceil((cam.dec + cam.fov*0.8) / decStep) * decStep);
                const cosFactor = Math.max(0.18, Math.cos(cam.dec * DEG2RAD));
                const raSpan = Math.min(180, (cam.fov * 0.9) / cosFactor);
                const minRa = Math.floor((cam.ra - raSpan) / raStep) * raStep;
                const maxRa = Math.ceil((cam.ra + raSpan) / raStep) * raStep;

                for (let d = minDec; d <= maxDec; d += decStep) {{
                    ctx.beginPath();
                    let started = false;
                    let labelPos = null;
                    for (let r = minRa; r <= maxRa; r += raStep * 0.25) {{
                        const p = world2pix(r, d);
                        if (p && p[0] > -200 && p[0] < canvas.width + 200 && p[1] > -200 && p[1] < canvas.height + 200) {{
                            if (!started) {{ ctx.moveTo(p[0], p[1]); started = true; }}
                            else ctx.lineTo(p[0], p[1]);
                            if (!labelPos && p[0] > 80 && p[0] < canvas.width * 0.35 && p[1] > 60 && p[1] < canvas.height - 60) {{
                                labelPos = p;
                            }}
                        }} else {{ started = false; }}
                    }}
                    ctx.stroke();
                    if (labelPos) ctx.fillText(formatDec(d), labelPos[0] + 4, labelPos[1] - 4);
                }}

                for (let r = minRa; r <= maxRa; r += raStep) {{
                    ctx.beginPath();
                    let started = false;
                    let labelPos = null;
                    for (let d = minDec; d <= maxDec; d += decStep * 0.25) {{
                        const p = world2pix(r, d);
                        if (p && p[0] > -200 && p[0] < canvas.width + 200 && p[1] > -200 && p[1] < canvas.height + 200) {{
                            if (!started) {{ ctx.moveTo(p[0], p[1]); started = true; }}
                            else ctx.lineTo(p[0], p[1]);
                            if (!labelPos && p[0] > canvas.width * 0.6 && p[0] < canvas.width - 90 && p[1] > 60 && p[1] < canvas.height - 60) {{
                                labelPos = p;
                            }}
                        }} else {{ started = false; }}
                    }}
                    ctx.stroke();
                    if (labelPos) ctx.fillText(formatRA(r), labelPos[0] + 4, labelPos[1] - 4);
                }}
            }}

            if (cam.showConstellations) {{
                ctx.strokeStyle = 'rgba(56, 189, 248, 0.28)';
                ctx.lineWidth = 1.25;
                ctx.fillStyle = 'rgba(125, 211, 252, 0.50)';
                ctx.font = 'bold 12px "Segoe UI", sans-serif';
                constellations.forEach(c => {{
                    c.lines.forEach(ln => {{
                        const p1 = world2pix(ln[0], ln[1]);
                        const p2 = world2pix(ln[2], ln[3]);
                        if (p1 && p2) {{
                            ctx.beginPath();
                            ctx.moveTo(p1[0], p1[1]);
                            ctx.lineTo(p2[0], p2[1]);
                            ctx.stroke();
                        }}
                    }});
                    const pc = world2pix(c.ra, c.dec);
                    if (pc && pc[0] > 0 && pc[0] < canvas.width && pc[1] > 0 && pc[1] < canvas.height) {{
                        ctx.fillText(c.name, pc[0] - 25, pc[1]);
                    }}
                }});
            }}

            const levels = [
                {{ cellDeg: 5.0, minFov: 0, maxFov: 130, count: 6, maxR: 1.6, alpha: 0.70 }},
                {{ cellDeg: 1.5, minFov: 0, maxFov: 40,  count: 8, maxR: 1.4, alpha: 0.65 }},
                {{ cellDeg: 0.4, minFov: 0, maxFov: 12,  count: 12, maxR: 1.1, alpha: 0.55 }}
            ];
            const cosDec0 = Math.max(0.15, Math.cos(cam.dec * DEG2RAD));

            levels.forEach(lv => {{
                if (cam.fov > lv.maxFov) return;
                const dSpan = cam.fov * 0.75;
                const rSpan = Math.min(180, dSpan / cosDec0);
                const iDecMin = Math.floor((cam.dec - dSpan) / lv.cellDeg);
                const iDecMax = Math.ceil((cam.dec + dSpan) / lv.cellDeg);
                const iRaMin = Math.floor((cam.ra - rSpan) / lv.cellDeg);
                const iRaMax = Math.ceil((cam.ra + rSpan) / lv.cellDeg);

                ctx.fillStyle = `rgba(235, 244, 255, ${{lv.alpha}})`;
                for (let id = iDecMin; id <= iDecMax; id++) {{
                    if (id * lv.cellDeg < -89 || id * lv.cellDeg > 89) continue;
                    for (let ir = iRaMin; ir <= iRaMax; ir++) {{
                        let irWrap = ((ir % Math.round(360 / lv.cellDeg)) + Math.round(360 / lv.cellDeg)) % Math.round(360 / lv.cellDeg);
                        for (let s = 0; s < lv.count; s++) {{
                            const h1 = hash2D(irWrap * 17 + s, id * 31 + s);
                            const h2 = hash2D(id * 23 + s, irWrap * 41 + s);
                            const sRa = (irWrap + h1) * lv.cellDeg;
                            const sDec = (id + h2) * lv.cellDeg;
                            const p = world2pix(sRa, sDec);
                            if (p && p[0] >= 0 && p[0] <= canvas.width && p[1] >= 0 && p[1] <= canvas.height) {{
                                const r = (0.4 + (h1 * h2) * lv.maxR);
                                ctx.beginPath();
                                ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
                                ctx.fill();
                            }}
                        }}
                    }}
                }}
            }});

            brightStars.forEach(st => {{
                const p = world2pix(st.ra, st.dec);
                if (!p || p[0] < -30 || p[0] > canvas.width + 30 || p[1] < -30 || p[1] > canvas.height + 30) return;

                const coreR = Math.max(1.8, 4.6 - st.mag * 0.9);
                const haloR = coreR * 3.2;

                const sGrad = ctx.createRadialGradient(p[0], p[1], coreR * 0.3, p[0], p[1], haloR);
                sGrad.addColorStop(0, st.col);
                sGrad.addColorStop(0.4, st.col + '66');
                sGrad.addColorStop(1, 'rgba(0,0,0,0)');
                ctx.fillStyle = sGrad;
                ctx.beginPath();
                ctx.arc(p[0], p[1], haloR, 0, Math.PI * 2);
                ctx.fill();

                ctx.fillStyle = '#ffffff';
                ctx.beginPath();
                ctx.arc(p[0], p[1], coreR * 0.75, 0, Math.PI * 2);
                ctx.fill();

                if (cam.showConstellations && st.name && (cam.fov < 28 || st.mag < 1.3)) {{
                    ctx.font = '11px "Segoe UI", sans-serif';
                    ctx.fillStyle = 'rgba(226, 232, 240, 0.78)';
                    ctx.fillText(st.name, p[0] + coreR + 4, p[1] - 3);
                }}
            }});

            const placedLabels = [];
            myPhotos.forEach(obj => {{
                const p = world2pix(obj.ra, obj.dec);
                if (!p || p[0] < -50 || p[0] > canvas.width + 50 || p[1] < -50 || p[1] > canvas.height + 50) return;

                const isSelected = activeOverlay.active && activeOverlay.name === obj.name;
                const radius = cam.fov < 10 ? 10 : 7;

                ctx.beginPath();
                ctx.arc(p[0], p[1], radius, 0, Math.PI * 2);
                ctx.strokeStyle = isSelected ? '#38bdf8' : '#2dd4bf';
                ctx.lineWidth = isSelected ? 2.2 : 1.5;
                ctx.stroke();

                ctx.beginPath();
                ctx.arc(p[0], p[1], 2.2, 0, Math.PI * 2);
                ctx.fillStyle = isSelected ? '#fff' : '#5eead4';
                ctx.fill();

                if (cam.fov < 35 || obj.name.startsWith('M ') || isSelected) {{
                    const shortLabel = obj.name.split(' - ')[0];
                    ctx.font = isSelected ? 'bold 12px "Segoe UI", sans-serif' : '11.5px "Segoe UI", sans-serif';
                    let lx = p[0] + radius + 5;
                    let ly = p[1] + 4;

                    for (let attempt = 0; attempt < 5; attempt++) {{
                        const collision = placedLabels.some(pl => Math.abs(pl.x - lx) < 85 && Math.abs(pl.y - ly) < 13);
                        if (!collision) break;
                        ly += 13;
                    }}
                    placedLabels.push({{ x: lx, y: ly }});

                    ctx.fillStyle = isSelected ? '#38bdf8' : '#5eead4';
                    ctx.fillText(shortLabel, lx, ly);
                }}
            }});

            const anchor = document.getElementById('sky-overlay-anchor');
            const imgEl = document.getElementById('sky-overlay-img');
            if (activeOverlay.active) {{
                const pCenter = world2pix(activeOverlay.ra, activeOverlay.dec);
                const halfFov = activeOverlay.fov / 2.0;
                const pNorth = world2pix(activeOverlay.ra, Math.min(89.95, activeOverlay.dec + halfFov));
                const pSouth = world2pix(activeOverlay.ra, Math.max(-89.95, activeOverlay.dec - halfFov));

                if (pCenter && pNorth && pSouth) {{
                    const distPx = Math.hypot(pNorth[0] - pSouth[0], pNorth[1] - pSouth[1]);
                    if (distPx < 8 || distPx > 9000) {{
                        anchor.style.display = 'none';
                    }} else {{
                        anchor.style.display = 'block';
                        imgEl.style.width = distPx + 'px';
                        const skyNorthAngle = Math.atan2(pNorth[1] - pCenter[1], pNorth[0] - pCenter[0]) * RAD2DEG + 90;
                        const totalAngle = skyNorthAngle + activeOverlay.rot;
                        const scaleX = activeOverlay.flipped ? -1 : 1;

                        anchor.style.left = pCenter[0] + 'px';
                        anchor.style.top = pCenter[1] + 'px';
                        anchor.style.transform = 'translate(-50%, -50%) rotate(' + totalAngle.toFixed(2) + 'deg) scaleX(' + scaleX + ')';
                    }}
                }} else {{
                    anchor.style.display = 'none';
                }}
            }} else {{
                anchor.style.display = 'none';
            }}

            const fovStr = cam.fov.toFixed(2).replace('.', ',') + '° × ' + cam.fov.toFixed(2).replace('.', ',') + '°';
            document.getElementById('fov-readout').innerText = fovStr;

            requestAnimationFrame(renderSky);
        }}

        let isDragging = false, dragMoved = false, lastX = 0, lastY = 0;

        canvas.addEventListener('mousedown', e => {{
            if (e.button !== 0) return;
            isDragging = true;
            dragMoved = false;
            lastX = e.clientX;
            lastY = e.clientY;
            anim.active = false;
        }});

        window.addEventListener('mousemove', e => {{
            if (!isDragging) return;
            const dx = e.clientX - lastX;
            const dy = e.clientY - lastY;
            if (Math.hypot(dx, dy) > 3) dragMoved = true;
            lastX = e.clientX;
            lastY = e.clientY;

            if (e.shiftKey && activeOverlay.active) {{
                stepRotation(dx * 0.3);
                return;
            }}

            const degPerPx = cam.fov / Math.min(canvas.width, canvas.height);
            const cosDec = Math.max(0.15, Math.cos(cam.dec * DEG2RAD));
            cam.ra = (cam.ra + (dx * degPerPx) / cosDec + 360) % 360;
            cam.dec = Math.max(-89.0, Math.min(89.0, cam.dec + dy * degPerPx));
        }});

        window.addEventListener('mouseup', e => {{
            if (isDragging && !dragMoved) {{
                handleCanvasClick(e.clientX, e.clientY);
            }}
            isDragging = false;
        }});

        canvas.addEventListener('wheel', e => {{
            e.preventDefault();
            anim.active = false;
            const factor = e.deltaY > 0 ? 1.14 : 0.88;
            cam.fov = Math.max(0.25, Math.min(115.0, cam.fov * factor));
        }}, {{ passive: false }});

        function handleCanvasClick(mx, my) {{
            let bestObj = null;
            let bestDist = 18;
            myPhotos.forEach(obj => {{
                const p = world2pix(obj.ra, obj.dec);
                if (p) {{
                    const d = Math.hypot(p[0] - mx, p[1] - my);
                    if (d < bestDist) {{
                        bestDist = d;
                        bestObj = obj;
                    }}
                }}
            }});
            if (bestObj) {{
                selectObject(bestObj, true);
            }}
        }}

        function flyTo(targetRa, targetDec, targetFov) {{
            anim.startRa = cam.ra;
            anim.startDec = cam.dec;
            anim.startFov = cam.fov;
            anim.targetRa = targetRa;
            anim.targetDec = targetDec;
            anim.targetFov = targetFov;
            anim.t0 = performance.now();
            anim.active = true;
        }}

        function selectObject(data, zoomIn) {{
            currentSelectedObj = data;
            document.getElementById('panel-title').innerText = data.name;
            document.getElementById('status-obj-lbl').innerText = data.name;
            
            let coordsHtml = '<b>RA:</b> ' + data.ra.toFixed(4) + '° | <b>DEC:</b> ' + data.dec.toFixed(4) + '°<br>';
            coordsHtml += '<span style="font-size:12px; color:#999; display:block; margin-top:6px;">';
            coordsHtml += '📅 <b>Erste Aufnahme:</b> ' + data.first_obs + '<br>';
            coordsHtml += '📅 <b>Letzte Aufnahme:</b> ' + data.last_obs + '</span>';
            
            let searchName = '';
            let matchNGC = data.name.match(/NGC\\s*(\\d+)/i);
            let matchM = data.name.match(/M\\s*(\\d+)/i);
            let matchIC = data.name.match(/IC\\s*(\\d+)/i);
            
            if (matchNGC) searchName = 'NGC' + matchNGC[1];
            else if (matchM) searchName = 'M' + matchM[1];
            else if (matchIC) searchName = 'IC' + matchIC[1];
            
            let linksHtml = '<div class="panel-link-container">';
            if (data.wiki_url) {{
                linksHtml += '<a href="' + data.wiki_url + '" target="_blank" class="atlas-btn">🌐 Wikipedia</a>';
            }}
            if (searchName) {{
                linksHtml += '<a href="https://stellarium-web.org/skysource/' + searchName + '" target="_blank" class="atlas-btn">✨ Stellarium</a>';
                linksHtml += '<a href="https://www.astrobin.com/search/?q=' + searchName + '" target="_blank" class="atlas-btn">📷 AstroBin</a>';
            }}
            linksHtml += '</div>';
            
            document.getElementById('panel-coords').innerHTML = coordsHtml + linksHtml;
            
            let imgsHtml = '';
            const safeTitle = data.name.replace(/'/g, "\\'");

            data.images.forEach(function(imgUrl, index) {{
                const meta = (data.image_meta && data.image_meta[index]) ? data.image_meta[index] : data;
                const badge = meta.solved ? ' <span style="color:#34d399; font-size:11px;">(✅ ASTAP WCS)</span>' : '';
                imgsHtml += '<div class="panel-img-container">';
                if (data.images.length > 1 || meta.solved) {{
                    imgsHtml += '<span style="font-size:12px; color:#aaa; display:block; margin-bottom:4px;">Aufnahme ' + (index+1) + badge + '</span>';
                }}
                imgsHtml += '<img src="' + imgUrl + '" class="panel-img" onclick="openLightbox(\\'' + imgUrl + '\\', \\'' + safeTitle + '\\')" title="Klicken für Großansicht" />';
                imgsHtml += '<div>';
                imgsHtml += '<button class="atlas-btn" onclick="activateSkyOverlayByIndex(' + index + ', true)">🌌 Im Himmel überblenden</button>';
                imgsHtml += '<button class="atlas-btn" onclick="openLightbox(\\'' + imgUrl + '\\', \\'' + safeTitle + '\\')">🔍 Vollbild</button>';
                imgsHtml += '</div></div>';
            }});
            document.getElementById('panel-images').innerHTML = imgsHtml;
            document.getElementById('side-panel').style.right = '0px';
            document.getElementById('bottom-controls').style.right = '394px';

            if (data.images.length > 0) {{
                activateSkyOverlayByIndex(0, zoomIn);
            }}
        }}

        function openLightbox(url, title) {{
            document.getElementById('lightbox-img').src = url;
            document.getElementById('lightbox-caption').innerText = title;
            document.getElementById('lightbox').style.display = 'flex';
        }}

        function activateSkyOverlayByIndex(index, zoomToTarget) {{
            if (!currentSelectedObj) return;
            const data = currentSelectedObj;
            const meta = (data.image_meta && data.image_meta[index]) ? data.image_meta[index] : data;

            activeOverlay.active = true;
            activeOverlay.name = data.name;
            activeOverlay.ra = meta.ra;
            activeOverlay.dec = meta.dec;
            activeOverlay.fov = meta.fov || 1.0;
            activeOverlay.rot = meta.rot || 0.0;
            activeOverlay.baseFov = activeOverlay.fov;
            activeOverlay.baseRot = activeOverlay.rot;
            activeOverlay.flipped = !!meta.flipped;
            activeOverlay.solved = !!meta.solved;

            // Falls der Nutzer für dieses Bild nicht schon manuell einen Override gespeichert hat, nutze exakt die ASTAP-Werte!
            if (!activeOverlay.solved) {{
                const savedKey = 'astro_ov_manual_' + data.name + '_' + index;
                try {{
                    const saved = JSON.parse(localStorage.getItem(savedKey));
                    if (saved) {{
                        if (typeof saved.rot === 'number') activeOverlay.rot = saved.rot;
                        if (typeof saved.fov === 'number') activeOverlay.fov = saved.fov;
                    }}
                }} catch(e) {{}}
            }}

            document.getElementById('sky-overlay-img').src = meta.url || data.images[index];
            document.getElementById('sky-overlay-caption').innerText = data.name;
            document.getElementById('ov-title').innerText = data.name;

            const badgeWrap = document.getElementById('ov-badge-wrap');
            if (activeOverlay.solved) {{
                badgeWrap.innerHTML = '<span class="ov-badge-solved">✅ ASTAP Plate-Solved · Position, Maßstab & Drehung exakt</span>';
            }} else {{
                badgeWrap.innerHTML = '';
            }}

            document.getElementById('slider-rot').value = activeOverlay.rot;
            document.getElementById('slider-fov').value = activeOverlay.fov;
            document.getElementById('overlay-controls').style.display = 'block';
            document.getElementById('sky-overlay-anchor').style.display = 'block';

            updateOverlayParams(false);

            if (zoomToTarget) {{
                fitOverlayInView();
            }}
        }}

        function updateOverlayParams(saveManual) {{
            const opVal = parseFloat(document.getElementById('slider-opacity').value);
            const rotVal = parseFloat(document.getElementById('slider-rot').value);
            const fovVal = parseFloat(document.getElementById('slider-fov').value);

            activeOverlay.opacity = opVal / 100.0;
            activeOverlay.rot = rotVal;
            activeOverlay.fov = fovVal;

            document.getElementById('lbl-opacity').innerText = Math.round(opVal) + ' %';
            document.getElementById('lbl-rot').innerText = rotVal.toFixed(1) + '°';
            document.getElementById('lbl-fov').innerText = fovVal.toFixed(2) + '°';

            document.getElementById('sky-overlay-img').style.opacity = activeOverlay.opacity;

            if (saveManual && activeOverlay.name) {{
                try {{
                    localStorage.setItem('astro_ov_manual_' + activeOverlay.name + '_0', JSON.stringify({{
                        rot: activeOverlay.rot,
                        fov: activeOverlay.fov
                    }}));
                }} catch(e) {{}}
            }}
        }}

        function stepRotation(delta) {{
            const sl = document.getElementById('slider-rot');
            let v = parseFloat(sl.value) + delta;
            if (v > 180) v -= 360;
            if (v < -180) v += 360;
            sl.value = v;
            updateOverlayParams(true);
        }}

        function resetRotation() {{
            document.getElementById('slider-rot').value = activeOverlay.baseRot;
            document.getElementById('slider-fov').value = activeOverlay.baseFov;
            updateOverlayParams(false);
        }}

        function stepFov(delta) {{
            const sl = document.getElementById('slider-fov');
            let v = Math.max(0.15, Math.min(8.0, parseFloat(sl.value) + delta));
            sl.value = v;
            updateOverlayParams(true);
        }}

        function fitOverlayInView() {{
            if (!activeOverlay.active) return;
            flyTo(activeOverlay.ra, activeOverlay.dec, Math.max(activeOverlay.fov * 2.4, 1.5));
        }}

        function toggleOverlayFrame() {{
            activeOverlay.showFrame = !activeOverlay.showFrame;
            const imgEl = document.getElementById('sky-overlay-img');
            const capEl = document.getElementById('sky-overlay-caption');
            if (activeOverlay.showFrame) {{
                imgEl.style.border = '1.5px solid rgba(255, 255, 255, 0.92)';
                capEl.style.display = 'block';
            }} else {{
                imgEl.style.border = 'none';
                capEl.style.display = 'none';
            }}
        }}

        function closeSkyOverlay() {{
            activeOverlay.active = false;
            document.getElementById('overlay-controls').style.display = 'none';
            document.getElementById('sky-overlay-anchor').style.display = 'none';
        }}

        function zoomBy(factor) {{
            anim.active = false;
            cam.fov = Math.max(0.25, Math.min(115.0, cam.fov * factor));
        }}

        function toggleConstellations() {{
            cam.showConstellations = !cam.showConstellations;
            document.getElementById('btn-const').classList.toggle('active', cam.showConstellations);
        }}

        function toggleMilkyWay() {{
            cam.showMilkyWay = !cam.showMilkyWay;
            document.getElementById('btn-mw').classList.toggle('active', cam.showMilkyWay);
        }}

        function toggleGrid() {{
            cam.showGrid = !cam.showGrid;
            document.getElementById('btn-grid').classList.toggle('active', cam.showGrid);
        }}

        function resetSkyView() {{
            flyTo(10.68, 41.27, 65.0);
        }}

        requestAnimationFrame(renderSky);
    </script>
</body>
</html>
"""
            with open(output_html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            if for_web:
                self.logic.open_system_folder(web_base_dir)
                if callback: callback(True, f"Website-Paket erstellt ({solved_total} Bilder exakt per ASTAP/WCS eingepasst)!")
            else:
                webbrowser.open(f"file:///{output_html_path.replace('\\', '/')}")
                if callback: callback(True, f"Himmelsatlas geöffnet ({solved_total} Bilder exakt per ASTAP/WCS eingepasst)!")
            return True
            
        except Exception as e:
            logging.error(f"Fehler beim Erstellen des Atlas: {e}\n{traceback.format_exc()}")
            if callback: callback(False, f"Kritischer Fehler: {e}")
            return False