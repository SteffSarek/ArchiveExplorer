import os
import json
import logging
import webbrowser
import traceback
import re
import urllib.request
from PIL import Image
from utils import generate_pretty_name

class AtlasExporter:
    def __init__(self, logic_instance):
        self.logic = logic_instance

    def generate_atlas(self, callback=None, for_web=False):
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

            # 1. Sammle alle Objekte und GRUPPIERE Bilder desselben Objekts
            atlas_objects_dict = {}
            date_pattern = re.compile(r'(202\d)[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])')
            
            for key, data in self.logic.index.items():
                pretty_name = generate_pretty_name(
                    data["original_name"], 
                    self.logic.caldwell_map, 
                    self.logic.reverse_caldwell_map, 
                    self.logic.common_names
                )
                
                ignore_names = [
                    "bilder", "sonstiges", "export", "test", "misc", "allgemein", 
                    "mond", "moon", "jupiter", "saturn", "mars", "venus", "sonne", "sun"
                ]
                if pretty_name.lower().strip() in ignore_names:
                    continue
                    
                if data.get("path") and "xxx_" in data.get("path").lower():
                    continue
                
                obj_data = self.logic.get_object_data(key, exact_match=True)
                
                ra = None
                dec = None
                clean_k = self.logic._get_clean_key(pretty_name)
                
                if clean_k in self.logic.coord_cache:
                    ra = self.logic.coord_cache[clean_k]["ra"]
                    dec = self.logic.coord_cache[clean_k]["dec"]
                else:
                    ra = obj_data.get("ra_decimal")
                    dec = obj_data.get("dec_decimal")
                
                if ra is not None and dec is not None:
                    obj_dates = []
                    if data.get("path") and os.path.isdir(data["path"]):
                        try:
                            for file in os.listdir(data["path"]):
                                match = date_pattern.search(file)
                                if match:
                                    obj_dates.append(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
                        except:
                            pass
                    
                    if not obj_dates and obj_data.get("date") and obj_data["date"] != "---":
                        try:
                            parts = obj_data["date"].split('.')
                            if len(parts) == 3:
                                obj_dates.append(f"{parts[2]}-{parts[1]}-{parts[0]}")
                        except:
                            pass

                    imgs = obj_data.get("images", [])
                    if not imgs and obj_data.get("image"):
                        imgs = [obj_data.get("image")]
                        
                    valid_exts = [".jpg", ".jpeg", ".png", ".gif", ".webp"]
                    clean_imgs = []
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
                                            
                                    clean_imgs.append(f"atlas_images/{web_filename}")
                                else:
                                    clean_imgs.append(f"file:///{p.replace('\\', '/')}")
                    
                    if not clean_imgs: continue

                    tel_type = data.get("telescope", "other")
                    if tel_type == "seestar": default_fov = 0.85
                    elif tel_type == "s30": default_fov = 1.60
                    elif tel_type == "dwarf": default_fov = 1.80
                    else: default_fov = 1.10
                    
                    if pretty_name not in atlas_objects_dict:
                        atlas_objects_dict[pretty_name] = {
                            "name": pretty_name,
                            "ra": float(ra),
                            "dec": float(dec),
                            "fov": default_fov,
                            "images": clean_imgs,
                            "dates": obj_dates,
                            "wiki_url": obj_data.get("wiki_url", "")
                        }
                    else:
                        for img in clean_imgs:
                            if img not in atlas_objects_dict[pretty_name]["images"]:
                                atlas_objects_dict[pretty_name]["images"].append(img)
                        atlas_objects_dict[pretty_name]["dates"].extend(obj_dates)

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

            # --- MULTI-MIRROR BACKUP FÜR ALADIN.JS ---
            # Versucht Aladin V3 lokal zu speichern (über CDS-Notfalllink oder weltweite CDNs)
            target_backup_dir = web_base_dir if for_web else export_dir
            local_js_path = os.path.join(target_backup_dir, "aladin_v3_backup.js")
            if not os.path.exists(local_js_path) or os.path.getsize(local_js_path) < 50000:
                js_mirrors = [
                    "https://cds.unistra.fr/aladin.js",
                    "https://cdn.jsdelivr.net/npm/aladin-lite@3.5.1/dist/aladin.umd.cjs",
                    "https://unpkg.com/aladin-lite@3.5.1/dist/aladin.umd.cjs",
                    "https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js"
                ]
                for mirror_url in js_mirrors:
                    try:
                        req = urllib.request.Request(mirror_url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req, timeout=4) as resp:
                            content = resp.read()
                            if len(content) > 50000:
                                with open(local_js_path, "wb") as f_js:
                                    f_js.write(content)
                                break
                    except Exception:
                        continue

            # 3. Das HTML/JavaScript-Template (Mit ESA/NASA Mirror-Surveys & Multi-CDN)
            html_content = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Astro-Nerd.info | Interaktiver 3D-Himmelsatlas</title>
    <style>
        body, html {{
            margin: 0; padding: 0; width: 100%; height: 100%;
            background-color: #111; color: #fff; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            overflow: hidden; user-select: none;
        }}
        #aladin-lite-div {{
            width: 100%; height: 100vh;
        }}
        
        #atlas-title {{
            position: absolute; top: 15px; left: 50%; transform: translateX(-50%); z-index: 1000;
            background: rgba(15, 20, 28, 0.88); padding: 10px 22px;
            border-radius: 10px; border: 1px solid rgba(52, 152, 219, 0.6);
            box-shadow: 0 4px 15px rgba(0,0,0,0.6); pointer-events: none;
            text-align: center; backdrop-filter: blur(6px);
        }}
        h1 {{ margin: 0; font-size: 18px; color: #3498db; font-weight: bold; }}
        #atlas-title span {{ font-size: 12px; color: #aaa; }}

        /* --- Himmels-Server Auswahl (ESA / NASA Mirrors) --- */
        #survey-selector-box {{
            position: absolute; bottom: 18px; left: 15px; z-index: 1500;
            background: rgba(13, 19, 26, 0.88); border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 8px; padding: 6px 12px; font-size: 12px; color: #cbd5e1;
            backdrop-filter: blur(6px); box-shadow: 0 4px 15px rgba(0,0,0,0.6);
        }}
        #survey-select {{
            background: #1e293b; color: #38bdf8; border: 1px solid #334155;
            border-radius: 5px; padding: 3px 6px; font-size: 12px; font-weight: bold;
            margin-left: 6px; cursor: pointer;
        }}

        /* --- SKY-OVERLAY STEUERPULT OBEN LINKS --- */
        #overlay-controls {{
            display: none; position: absolute; top: 15px; left: 15px; z-index: 1500;
            width: 310px; background: rgba(13, 19, 26, 0.90);
            border: 1px solid rgba(255, 255, 255, 0.15); border-radius: 10px;
            padding: 14px 16px; box-shadow: 0 8px 25px rgba(0,0,0,0.75);
            backdrop-filter: blur(8px); color: #e2e8f0;
        }}
        #ov-title {{
            font-size: 15px; font-weight: bold; color: #fff; margin-bottom: 3px;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }}
        #ov-sub {{
            font-size: 11px; color: #94a3b8; margin-bottom: 12px; line-height: 1.35;
        }}
        .ov-row {{
            background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px; padding: 8px 10px; margin-bottom: 8px;
        }}
        .ov-label-row {{
            display: flex; justify-content: space-between; align-items: center;
            font-size: 12px; margin-bottom: 5px; color: #cbd5e1;
        }}
        .ov-val {{ font-weight: bold; color: #38bdf8; font-family: Consolas, monospace; }}
        .ov-slider-wrap {{
            display: flex; align-items: center; gap: 6px;
        }}
        .ov-slider {{
            flex: 1; accent-color: #38bdf8; cursor: pointer; height: 5px;
        }}
        .ov-mini-btn {{
            background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);
            color: #ddd; border-radius: 5px; padding: 2px 7px; font-size: 11px;
            cursor: pointer; transition: all 0.15s;
        }}
        .ov-mini-btn:hover {{ background: #3498db; color: #fff; border-color: #3498db; }}
        .ov-actions {{
            display: flex; gap: 6px; margin-top: 10px;
        }}
        .ov-action-btn {{
            flex: 1; background: rgba(52, 152, 219, 0.18); border: 1px solid rgba(52, 152, 219, 0.5);
            color: #e2e8f0; border-radius: 6px; padding: 6px 8px; font-size: 12px;
            font-weight: bold; cursor: pointer; transition: all 0.2s; text-align: center;
        }}
        .ov-action-btn:hover {{ background: #3498db; color: #fff; }}
        .ov-action-btn.close {{
            flex: 0 0 auto; background: rgba(231, 76, 60, 0.18); border-color: rgba(231, 76, 60, 0.5);
        }}
        .ov-action-btn.close:hover {{ background: #e74c3c; color: #fff; }}

        /* --- BILD-OVERLAY MITTEN IM HIMMEL --- */
        #sky-overlay-anchor {{
            display: none; position: absolute; top: 0; left: 0;
            pointer-events: none; z-index: 600;
            transform-origin: center center;
        }}
        #sky-overlay-box {{
            position: relative; display: inline-block;
        }}
        #sky-overlay-caption {{
            position: absolute; top: -22px; left: 50%; transform: translateX(-50%);
            white-space: nowrap; font-size: 12px; font-weight: bold; color: #fff;
            text-shadow: 0 1px 4px #000, 0 0 8px #000; letter-spacing: 0.3px;
        }}
        #sky-overlay-img {{
            display: block; width: 300px; height: auto;
            border: 1.5px solid rgba(255, 255, 255, 0.9);
            box-shadow: 0 0 25px rgba(0, 0, 0, 0.7);
        }}

        #offline-warning {{
            display: none; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            background: rgba(30, 20, 20, 0.95); border: 2px solid #e74c3c; border-radius: 10px;
            padding: 25px 35px; text-align: center; max-width: 500px; z-index: 3000;
            box-shadow: 0 10px 30px rgba(0,0,0,0.8);
        }}
        #offline-warning h2 {{ color: #e74c3c; margin-top: 0; }}
        #offline-warning p {{ color: #ccc; line-height: 1.5; font-size: 14px; }}
        
        #side-panel {{
            position: absolute; top: 0; right: -450px;
            width: 390px; max-width: 90vw; height: 100vh; background: rgba(15, 18, 24, 0.95);
            border-left: 2px solid #3498db; box-shadow: -5px 0 25px rgba(0,0,0,0.8);
            z-index: 2000; transition: right 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
            overflow-y: auto; padding: 22px; box-sizing: border-box; color: #fff;
        }}
        #close-panel {{
            float: right; background: none; border: none; color: #888; font-size: 24px; cursor: pointer; margin-top: -5px;
        }}
        #close-panel:hover {{ color: #e74c3c; }}
        #panel-title {{ margin-top: 0; color: #3498db; font-size: 21px; margin-bottom: 5px; }}
        #panel-coords {{ font-size: 14px; color: #aaa; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333; line-height: 1.5; }}
        #panel-coords b {{ color: #ddd; }}
        
        .panel-link-container {{ margin-top: 12px; margin-bottom: 5px; }}
        .atlas-btn {{
            display: inline-block; padding: 5px 10px; margin-right: 6px; margin-bottom: 5px;
            border-radius: 4px; border: 1px solid #3498db; color: #3498db;
            text-decoration: none; font-size: 12px; font-weight: bold; transition: all 0.2s; cursor: pointer;
            background: transparent;
        }}
        .atlas-btn:hover {{ background: #3498db; color: #fff; }}
        
        .panel-img-container {{
            margin-bottom: 20px; text-align: center; background: rgba(255,255,255,0.03);
            padding: 10px; border-radius: 8px; border: 1px solid #2a2a2a;
        }}
        .panel-img {{
            max-width: 100%; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.6);
            cursor: zoom-in; transition: transform 0.2s; border: 1px solid #333; margin-bottom: 8px;
        }}
        .panel-img:hover {{ transform: scale(1.02); border-color: #3498db; }}

        #lightbox {{
            display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.92); z-index: 5000; align-items: center; justify-content: center;
            flex-direction: column; cursor: zoom-out;
        }}
        #lightbox img {{
            max-width: 92vw; max-height: 88vh; border-radius: 6px;
            box-shadow: 0 0 30px rgba(0,0,0,0.9); border: 1px solid #444;
        }}
        #lightbox-caption {{
            margin-top: 12px; color: #3498db; font-size: 16px; font-weight: bold;
        }}
    </style>
</head>
<body>

    <div id="atlas-title">
        <h1>Mein Himmelsatlas</h1>
        <span>{len(atlas_objects)} fotografierte Himmelsobjekte</span>
    </div>

    <!-- Himmels-Server Umschalter (ESA / NASA / CDS Mirrors) -->
    <div id="survey-selector-box">
        🛰️ Himmels-Server:
        <select id="survey-select" onchange="changeSkySurvey(this.value)">
            <option value="https://skies.esac.esa.int/DSSColor">DSS2 Color (ESA-Server Europa)</option>
            <option value="https://stpubdata.s3.us-east-1.amazonaws.com/mast/skybackgrounds/DSSColor">DSS2 Color (NASA/AWS-Server USA)</option>
            <option value="https://stpubdata.s3.us-east-1.amazonaws.com/mast/skybackgrounds/PanSTARRS">PanSTARRS DR1 Color (NASA/AWS)</option>
            <option value="https://skies.esac.esa.int/2MASSColor">2MASS Infrarot (ESA-Server)</option>
            <option value="https://skies.esac.esa.int/AllWISEColor">AllWISE Infrarot (ESA-Server)</option>
            <option value="P/DSS2/color">CDS Straßburg Standard</option>
        </select>
    </div>

    <!-- STEUERPULT OBEN LINKS FÜR DAS SKY-OVERLAY -->
    <div id="overlay-controls">
        <div id="ov-title">Objektname</div>
        <div id="ov-sub">Bild im Himmel eingepasst · Ziehen: verschieben · Mausrad: zoomen · Deckkraft & Drehung regelbar</div>

        <div class="ov-row">
            <div class="ov-label-row">
                <span>Deckkraft</span>
                <span class="ov-val" id="lbl-opacity">85 %</span>
            </div>
            <div class="ov-slider-wrap">
                <input type="range" id="slider-opacity" class="ov-slider" min="0" max="100" value="85" oninput="updateOverlayParams()">
            </div>
        </div>

        <div class="ov-row">
            <div class="ov-label-row">
                <span>Drehung</span>
                <span class="ov-val" id="lbl-rot">0°</span>
            </div>
            <div class="ov-slider-wrap">
                <button class="ov-mini-btn" onclick="stepRotation(-1)" title="-1°">↶</button>
                <input type="range" id="slider-rot" class="ov-slider" min="-180" max="180" step="0.5" value="0" oninput="updateOverlayParams()">
                <button class="ov-mini-btn" onclick="stepRotation(1)" title="+1°">↷</button>
                <button class="ov-mini-btn" onclick="resetRotation()" title="Auf 0° zurücksetzen">0°</button>
            </div>
        </div>

        <div class="ov-row">
            <div class="ov-label-row">
                <span>Bildfeld (Größe)</span>
                <span class="ov-val" id="lbl-fov">1.00°</span>
            </div>
            <div class="ov-slider-wrap">
                <button class="ov-mini-btn" onclick="stepFov(-0.05)">-</button>
                <input type="range" id="slider-fov" class="ov-slider" min="0.15" max="6.0" step="0.05" value="1.0" oninput="updateOverlayParams()">
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

    <div id="offline-warning">
        <h2>⚠️ Keine Internetverbindung zum Karten-CDN</h2>
        <p>Weder die lokale Backup-Datei noch die internationalen Ausweich-CDNs konnten geladen werden.</p>
    </div>

    <div id="side-panel">
        <button id="close-panel" onclick="document.getElementById('side-panel').style.right = '-450px';">✖</button>
        <h2 id="panel-title">Objektname</h2>
        <div id="panel-coords"></div>
        <div id="panel-images"></div>
    </div>

    <div id="lightbox" onclick="this.style.display='none';">
        <img id="lightbox-img" src="" alt="Großansicht" />
        <div id="lightbox-caption"></div>
    </div>

    <div id="aladin-lite-div"></div>

    <!-- Multi-Stage Loader: Lokales Backup -> CDS Notfall-Link -> Weltweites jsDelivr CDN -->
    <script>
        const myPhotos = {json.dumps(atlas_objects, indent=4)};
        let aladinInstance = null;

        let activeOverlay = {{
            active: false,
            name: "",
            ra: 0,
            dec: 0,
            fov: 1.0,
            rot: 0,
            opacity: 0.85,
            showFrame: true
        }};

        const scriptSources = [
            "aladin_v3_backup.js",
            "https://cds.unistra.fr/aladin.js",
            "https://cdn.jsdelivr.net/npm/aladin-lite@3.5.1/dist/aladin.umd.cjs",
            "https://unpkg.com/aladin-lite@3.5.1/dist/aladin.umd.cjs"
        ];

        function loadAladinScript(index) {{
            if (typeof A !== 'undefined') {{
                startAladin();
                return;
            }}
            if (index >= scriptSources.length) {{
                document.getElementById('offline-warning').style.display = 'block';
                return;
            }}
            const s = document.createElement('script');
            s.src = scriptSources[index];
            s.charset = 'utf-8';
            s.onload = function() {{
                if (typeof A !== 'undefined') startAladin();
                else loadAladinScript(index + 1);
            }};
            s.onerror = function() {{
                loadAladinScript(index + 1);
            }};
            document.head.appendChild(s);
        }}

        function changeSkySurvey(surveyUrl) {{
            if (!aladinInstance) return;
            try {{
                if (surveyUrl.startsWith('http')) {{
                    const customSurvey = aladinInstance.createImageSurvey(surveyUrl, surveyUrl, surveyUrl, "equatorial", 9, {{imgFormat: 'jpeg'}});
                    aladinInstance.setBaseImageLayer(customSurvey);
                }} else {{
                    aladinInstance.setImageSurvey(surveyUrl);
                }}
            }} catch(e) {{
                try {{ aladinInstance.setImageSurvey(surveyUrl); }} catch(err) {{}}
            }}
        }}

        function openLightbox(url, title) {{
            document.getElementById('lightbox-img').src = url;
            document.getElementById('lightbox-caption').innerText = title;
            document.getElementById('lightbox').style.display = 'flex';
        }}

        function activateSkyOverlay(imgUrl, name, ra, dec, defaultFov, zoomToTarget) {{
            activeOverlay.active = true;
            activeOverlay.name = name;
            activeOverlay.ra = ra;
            activeOverlay.dec = dec;
            activeOverlay.fov = defaultFov || 1.0;

            const savedKey = 'astro_ov_' + name;
            try {{
                const saved = JSON.parse(localStorage.getItem(savedKey));
                if (saved) {{
                    if (typeof saved.rot === 'number') activeOverlay.rot = saved.rot;
                    if (typeof saved.fov === 'number') activeOverlay.fov = saved.fov;
                }} else {{
                    activeOverlay.rot = 0;
                }}
            }} catch(e) {{
                activeOverlay.rot = 0;
            }}

            document.getElementById('sky-overlay-img').src = imgUrl;
            document.getElementById('sky-overlay-caption').innerText = name;
            document.getElementById('ov-title').innerText = name;

            document.getElementById('slider-rot').value = activeOverlay.rot;
            document.getElementById('slider-fov').value = activeOverlay.fov;
            document.getElementById('overlay-controls').style.display = 'block';
            document.getElementById('sky-overlay-anchor').style.display = 'block';

            updateOverlayParams();

            if (zoomToTarget && aladinInstance) {{
                fitOverlayInView();
            }}
        }}

        function updateOverlayParams() {{
            const opVal = parseFloat(document.getElementById('slider-opacity').value);
            const rotVal = parseFloat(document.getElementById('slider-rot').value);
            const fovVal = parseFloat(document.getElementById('slider-fov').value);

            activeOverlay.opacity = opVal / 100.0;
            activeOverlay.rot = rotVal;
            activeOverlay.fov = fovVal;

            document.getElementById('lbl-opacity').innerText = Math.round(opVal) + ' %';
            document.getElementById('lbl-rot').innerText = rotVal.toFixed(1) + '°';
            document.getElementById('lbl-fov').innerText = fovVal.toFixed(2) + '°';

            const imgEl = document.getElementById('sky-overlay-img');
            imgEl.style.opacity = activeOverlay.opacity;

            if (activeOverlay.name) {{
                try {{
                    localStorage.setItem('astro_ov_' + activeOverlay.name, JSON.stringify({{
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
            updateOverlayParams();
        }}

        function resetRotation() {{
            document.getElementById('slider-rot').value = 0;
            updateOverlayParams();
        }}

        function stepFov(delta) {{
            const sl = document.getElementById('slider-fov');
            let v = Math.max(0.15, Math.min(6.0, parseFloat(sl.value) + delta));
            sl.value = v;
            updateOverlayParams();
        }}

        function fitOverlayInView() {{
            if (!aladinInstance || !activeOverlay.active) return;
            aladinInstance.gotoRaDec(activeOverlay.ra, activeOverlay.dec);
            aladinInstance.setFoV(Math.max(activeOverlay.fov * 2.8, 1.5));
        }}

        function toggleOverlayFrame() {{
            activeOverlay.showFrame = !activeOverlay.showFrame;
            const imgEl = document.getElementById('sky-overlay-img');
            const capEl = document.getElementById('sky-overlay-caption');
            if (activeOverlay.showFrame) {{
                imgEl.style.border = '1.5px solid rgba(255, 255, 255, 0.9)';
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

        function syncOverlayLoop() {{
            if (activeOverlay.active && aladinInstance) {{
                const anchor = document.getElementById('sky-overlay-anchor');
                const imgEl = document.getElementById('sky-overlay-img');

                try {{
                    const pCenter = aladinInstance.world2pix(activeOverlay.ra, activeOverlay.dec);
                    const halfFov = activeOverlay.fov / 2.0;
                    const decNorth = Math.min(89.95, activeOverlay.dec + halfFov);
                    const decSouth = Math.max(-89.95, activeOverlay.dec - halfFov);

                    const pNorth = aladinInstance.world2pix(activeOverlay.ra, decNorth);
                    const pSouth = aladinInstance.world2pix(activeOverlay.ra, decSouth);

                    if (pCenter && pNorth && pSouth && !isNaN(pCenter[0]) && !isNaN(pNorth[0])) {{
                        const distPx = Math.hypot(pNorth[0] - pSouth[0], pNorth[1] - pSouth[1]);

                        if (distPx < 8 || distPx > 8000) {{
                            anchor.style.display = 'none';
                        }} else {{
                            anchor.style.display = 'block';
                            imgEl.style.width = distPx + 'px';

                            const skyNorthAngle = Math.atan2(pNorth[1] - pCenter[1], pNorth[0] - pCenter[0]) * 180 / Math.PI + 90;
                            const totalAngle = skyNorthAngle + activeOverlay.rot;

                            anchor.style.left = pCenter[0] + 'px';
                            anchor.style.top = pCenter[1] + 'px';
                            anchor.style.transform = 'translate(-50%, -50%) rotate(' + totalAngle.toFixed(2) + 'deg)';
                        }}
                    }} else {{
                        anchor.style.display = 'none';
                    }}
                }} catch (e) {{}}
            }}
            requestAnimationFrame(syncOverlayLoop);
        }}

        function startAladin() {{
            A.init.then(() => {{
                // Startet direkt mit dem stabilen ESA-Mirror statt dem ausgefallenen Straßburger Server!
                const esaDssUrl = "https://skies.esac.esa.int/DSSColor";
                aladinInstance = A.aladin('#aladin-lite-div', {{
                    survey: esaDssUrl, 
                    fov: 60, 
                    target: "00 42 44.3 +41 16 09", 
                    cooFrame: "ICRS"
                }});

                changeSkySurvey(esaDssUrl);

                const markerLayer = A.catalog({{
                    name: 'Meine Astrofotos', 
                    color: '#3498db', 
                    shape: 'circle', 
                    sourceSize: 14
                }});
                aladinInstance.addCatalog(markerLayer);

                myPhotos.forEach(function(photo) {{
                    const source = A.source(photo.ra, photo.dec, photo);
                    markerLayer.addSources([source]);
                }});

                requestAnimationFrame(syncOverlayLoop);

                aladinInstance.on('objectClicked', function(object) {{
                    if (object) {{
                        const data = object.data;
                        
                        document.getElementById('panel-title').innerText = data.name;
                        
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
                        const defFov = data.fov || 1.0;

                        data.images.forEach(function(imgUrl, index) {{
                            imgsHtml += '<div class="panel-img-container">';
                            if (data.images.length > 1) {{
                                imgsHtml += '<span style="font-size:12px; color:#aaa; display:block; margin-bottom:4px;">Aufnahme ' + (index+1) + '</span>';
                            }}
                            imgsHtml += '<img src="' + imgUrl + '" class="panel-img" onclick="openLightbox(\\'' + imgUrl + '\\', \\'' + safeTitle + '\\')" title="Klicken für Großansicht" />';
                            imgsHtml += '<div>';
                            imgsHtml += '<button class="atlas-btn" onclick="activateSkyOverlay(\\'' + imgUrl + '\\', \\'' + safeTitle + '\\', ' + data.ra + ', ' + data.dec + ', ' + defFov + ', true)">🌌 Im Himmel überblenden</button>';
                            imgsHtml += '<button class="atlas-btn" onclick="openLightbox(\\'' + imgUrl + '\\', \\'' + safeTitle + '\\')">🔍 Vollbild</button>';
                            imgsHtml += '</div></div>';
                        }});
                        document.getElementById('panel-images').innerHTML = imgsHtml;
                        document.getElementById('side-panel').style.right = '0px';

                        if (data.images.length > 0) {{
                            activateSkyOverlay(data.images[0], data.name, data.ra, data.dec, defFov, true);
                        }}
                    }} else {{
                        document.getElementById('side-panel').style.right = '-450px';
                    }}
                }});
            }});
        }}

        window.addEventListener('load', function() {{
            loadAladinScript(0);
        }});
    </script>
</body>
</html>
"""
            with open(output_html_path, "w", encoding="utf-8") as f:
                f.write(html_content)

            if for_web:
                self.logic.open_system_folder(web_base_dir)
                if callback: callback(True, f"Website-Paket erstellt in: {web_base_dir}")
            else:
                webbrowser.open(f"file:///{output_html_path.replace('\\', '/')}")
                if callback: callback(True, "Himmelsatlas erfolgreich im Browser geöffnet!")
            return True
            
        except Exception as e:
            logging.error(f"Fehler beim Erstellen des Atlas: {e}\n{traceback.format_exc()}")
            if callback: callback(False, f"Kritischer Fehler: {e}")
            return False