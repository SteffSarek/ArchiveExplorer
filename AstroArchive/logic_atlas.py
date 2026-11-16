import os
import json
import logging
import webbrowser
import traceback
import re
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
                # Teleskop-Zusätze bei Nicht-Standard-Objekten (z.B. Kometen) entfernen
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

                    otype = self.logic.determine_object_type(pretty_name, pretty_name)
                    
                    if pretty_name not in atlas_objects_dict:
                        atlas_objects_dict[pretty_name] = {
                            "name": pretty_name,
                            "ra": float(ra),
                            "dec": float(dec),
                            "fov": default_fov,
                            "type": otype,
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

            back_btn_text = "🏠 Zurück zu Astro-Nerd.info" if for_web else "✖ Atlas schließen"

            # 2. Eigenständiger 3D Sky Atlas & Lokale Planerkarte
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

        /* --- TITEL, ZURÜCK-BUTTON & SUCHE OBEN MITTE --- */
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

        /* --- STEUERPULT OBEN LINKS (Unterhalb der Top-Bar) --- */
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

        /* --- BILD-OVERLAY MITTEN IM KOORDINATENGITTER --- */
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

        /* --- STATUSLEISTE UNTEN LINKS & ZOOM UNTEN RECHTS --- */
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

        /* --- SEITENLEISTE RECHTS --- */
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
        <button id="btn-home" onclick="exitAtlas()" title="Zurück zur Hauptseite / Schließen">{back_btn_text}</button>
        <div id="atlas-title">
            <h1>Mein Himmelsatlas</h1>
            <span>Lokale Planerkarte · {len(atlas_objects)} eigene Katalogobjekte</span>
        </div>
        <input type="text" id="search-input" list="obj-datalist" placeholder="🔍 Objekt suchen (z.B. M 31)..." onchange="onSearchSelect(this.value)" />
        <datalist id="obj-datalist"></datalist>
    </div>

    <!-- STEUERPULT OBEN LINKS FÜR DAS EINGEPASSTE BILD -->
    <div id="overlay-controls">
        <div id="ov-title">Objektname</div>
        <div id="ov-sub">Bild mit Rahmen eingepasst · Maßstabsgetreu · Ziehen: verschieben · Mausrad: zoomen · Deckkraft & Drehung regelbar</div>

        <div class="ov-row">
            <div class="ov-label-row">
                <span>Deckkraft</span>
                <span class="ov-val" id="lbl-opacity">100 %</span>
            </div>
            <div class="ov-slider-wrap">
                <input type="range" id="slider-opacity" class="ov-slider" min="0" max="100" value="100" oninput="updateOverlayParams()">
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

    <!-- DAS BILD-OVERLAY MITTEN AUF DER STERNKARTE -->
    <div id="sky-overlay-anchor">
        <div id="sky-overlay-box">
            <div id="sky-overlay-caption"></div>
            <img id="sky-overlay-img" src="" alt="Sky Overlay" />
        </div>
    </div>

    <!-- FUSSZEILE LINKS & STEUERUNG RECHTS -->
    <div id="bottom-status">
        <b id="status-obj-lbl">Gesamter Himmel</b> · Sky Atlas · Lokale Planerkarte · <b>{len(atlas_objects)} Katalogobjekte</b> · RA/Dec-Gitter · Zoomabhängige Feldsterne (bis 15 mag)
    </div>

    <div id="bottom-controls">
        <button class="ctrl-btn" onclick="zoomBy(1.3)" title="Herauszoomen">−</button>
        <span id="fov-readout">65,0° × 65,0°</span>
        <button class="ctrl-btn" onclick="zoomBy(0.75)" title="Hineinzoomen">+</button>
        <button class="ctrl-btn" onclick="fitOverlayInView()">Bild einpassen</button>
        <button class="ctrl-btn active" id="btn-grid" onclick="toggleGrid()" title="RA/Dec-Gitter ein/aus">⊞</button>
        <button class="ctrl-btn" onclick="resetSkyView()" title="Gesamten Himmel zeigen">↺</button>
    </div>

    <div id="side-panel">
        <button id="close-panel" onclick="document.getElementById('side-panel').style.right = '-430px';">✖</button>
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

        // Erkennt automatisch, ob der Atlas in deiner WordPress-Seite eingebettet ist:
        const isEmbeddedInWordPress = (window.self !== window.top);
        if (isEmbeddedInWordPress) {{
            document.getElementById('btn-home').innerText = '⛶ Vollbild';
            document.getElementById('btn-home').title = 'Atlas im Vollbildmodus umschalten';
        }}

        function exitAtlas() {{
            if (isEmbeddedInWordPress) {{
                // In WordPress eingebettet -> Schaltet echtes Vollbild ein/aus!
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
            showGrid: true
        }};

        let anim = {{ active: false, startRa: 0, startDec: 0, startFov: 60, targetRa: 0, targetDec: 0, targetFov: 5, t0: 0, dur: 650 }};

        let activeOverlay = {{
            active: false,
            name: "",
            ra: 0,
            dec: 0,
            fov: 1.0,
            rot: 0,
            opacity: 1.0,
            showFrame: true
        }};

        const constellations = [
            {{ name: "Orion", ra: 83.8, dec: 3.0, lines: [[88.79,7.41,81.28,6.35],[81.28,6.35,83.0, -0.3],[83.0,-0.3,84.05,-1.2],[84.05,-1.2,85.19,-1.94],[85.19,-1.94,88.79,7.41],[83.0,-0.3,78.63,-8.2],[78.63,-8.2,86.94,-9.67],[86.94,-9.67,85.19,-1.94]] }},
            {{ name: "Cygnus (Schwan)", ra: 309.3, dec: 42.0, lines: [[310.36,45.28,305.56,40.26],[305.56,40.26,292.68,27.96],[296.24,45.13,305.56,40.26],[305.56,40.26,311.55,33.97]] }},
            {{ name: "Cassiopeia", ra: 15.0, dec: 60.0, lines: [[2.29,59.15,10.13,56.54],[10.13,56.54,14.18,60.72],[14.18,60.72,21.45,60.24],[21.45,60.24,28.60,63.67]] }},
            {{ name: "Ursa Major (Großer Bär)", ra: 185.0, dec: 55.0, lines: [[165.93,61.75,165.46,56.38],[165.46,56.38,178.46,53.69],[178.46,53.69,183.86,57.03],[183.86,57.03,165.93,61.75],[183.86,57.03,193.51,55.96],[193.51,55.96,200.98,54.93],[200.98,54.93,206.89,49.31]] }},
            {{ name: "Andromeda", ra: 12.0, dec: 38.0, lines: [[2.09,29.09,9.83,30.86],[9.83,30.86,17.43,35.62],[17.43,35.62,30.97,42.33]] }},
            {{ name: "Leo (Löwe)", ra: 160.0, dec: 18.0, lines: [[152.09,11.97,154.99,19.84],[154.99,19.84,177.26,14.57],[177.26,14.57,168.53,20.52],[168.53,20.52,154.99,19.84]] }}
        ];

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

        const DEG2RAD = Math.PI / 180.0;
        const RAD2DEG = 180.0 / Math.PI;

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

            if (cam.fov > 12) {{
                ctx.strokeStyle = 'rgba(56, 189, 248, 0.22)';
                ctx.lineWidth = 1.2;
                ctx.fillStyle = 'rgba(125, 211, 252, 0.45)';
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
                {{ cellDeg: 5.0, minFov: 0, maxFov: 130, count: 6, maxR: 2.0, alpha: 0.85 }},
                {{ cellDeg: 1.5, minFov: 0, maxFov: 40,  count: 8, maxR: 1.5, alpha: 0.70 }},
                {{ cellDeg: 0.4, minFov: 0, maxFov: 12,  count: 12, maxR: 1.2, alpha: 0.60 }}
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

            myPhotos.forEach(obj => {{
                const p = world2pix(obj.ra, obj.dec);
                if (!p || p[0] < -50 || p[0] > canvas.width + 50 || p[1] < -50 || p[1] > canvas.height + 50) return;

                const isSelected = activeOverlay.active && activeOverlay.name === obj.name;
                const radius = cam.fov < 10 ? 11 : 7;

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
                    ctx.fillStyle = isSelected ? '#38bdf8' : '#5eead4';
                    ctx.fillText(shortLabel, p[0] + radius + 5, p[1] + 4);
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

                        anchor.style.left = pCenter[0] + 'px';
                        anchor.style.top = pCenter[1] + 'px';
                        anchor.style.transform = 'translate(-50%, -50%) rotate(' + totalAngle.toFixed(2) + 'deg)';
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
                activateSkyOverlay(data.images[0], data.name, data.ra, data.dec, defFov, zoomIn);
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

            if (zoomToTarget) {{
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

            document.getElementById('sky-overlay-img').style.opacity = activeOverlay.opacity;

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
                if callback: callback(True, f"Website-Paket erstellt in: {web_base_dir}")
            else:
                webbrowser.open(f"file:///{output_html_path.replace('\\', '/')}")
                if callback: callback(True, "Himmelsatlas erfolgreich im Browser geöffnet!")
            return True
            
        except Exception as e:
            logging.error(f"Fehler beim Erstellen des Atlas: {e}\n{traceback.format_exc()}")
            if callback: callback(False, f"Kritischer Fehler: {e}")
            return False