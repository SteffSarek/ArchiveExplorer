🌌 AstroArchive Explorer
Das ultimative Verwaltungs- und Analyse-Tool für Astrofotos von Smart-Teleskopen.
AstroArchive Explorer ist eine in Python geschriebene Desktop-Anwendung, die speziell für Nutzer von Smart-Teleskopen (wie dem ZWO Seestar S50, S30 Pro oder Dwarf II / 3) entwickelt wurde. Es nimmt dir das Chaos der Dateiverwaltung ab, organisiert deine FITS-Rohdaten automatisch und bietet mächtige Planungs- und Analysewerkzeuge.

✨ Hauptfunktionen
📂 Automatischer Import & Archivierung: Liest FITS-Header aus und sortiert deine Aufnahmen vollautomatisch nach Objekt, Sternbild und Teleskop.
📚 Integrierte Kataloge: Beinhaltet Messier, Caldwell, NGC und IC Objekte inklusive Kreuzreferenzen und Eigennamen.
🗺️ Interaktive Sternkarte: Zeigt all deine fotografierten Objekte auf einer anpassbaren Himmelskarte an.
🔭 Smarte Beobachtungsliste: Plane deine nächste Nacht. Berechnet Live-Horizonthöhen, Transitzeiten und warnt vor störendem Mondlicht.
📱 Mobile Sync: Exportiere deine aktuelle Beobachtungsliste als interaktive HTML-App für dein Smartphone – perfekt für draußen auf dem Feld!
🧩 Plate Solving & Aladin: Löse Bilder direkt lokal mit ASTAP (oder per Cloud) und öffne sie zielsicher im Aladin Sky Atlas.
📊 Statistik & Reports: Behalte deinen Fortschritt (z.B. "Alle Messier-Objekte") im Blick und exportiere PDF/HTML-Reports.
📥 Installation & Download
Für normale Nutzer (Ohne Programmieren)
Du möchtest das Programm einfach nur nutzen?
Gehe rechts auf dieser Seite zum Bereich [Releases].
Lade dir die neueste AstroArchive_vX.X.zip Datei herunter.
Entpacke den Ordner an einen beliebigen Ort auf deiner Festplatte.
Starte die AstroArchiveExplorer.exe.
Für Entwickler (Aus dem Quellcode ausführen)
Wenn du den Python-Code selbst ausführen oder anpassen möchtest:
Klone dieses Repository auf deinen PC:
code
Bash
git clone https://github.com/SteffSarek/ArchiveExplorer.git
Installiere die benötigten Bibliotheken (Requirements). Es wird eine virtuelle Umgebung empfohlen:
code
Bash
pip install customtkinter pillow astropy pandas numpy matplotlib requests beautifulsoup4 ephem skyfield sep
Starte das Programm:
code
Bash
python main.py
⚙️ Externe Abhängigkeiten (Optional)
Um den vollen Funktionsumfang (Metrische Analyse, Plate Solving) nutzen zu können, empfiehlt es sich, folgende kostenlose Programme installiert zu haben. Die Pfade können in den Einstellungen des AstroArchive Explorers hinterlegt werden:
Siril: Für das Stacking und die metrische FWHM/SNR Analyse.
ASTAP: Für rasend schnelles lokales Plate Solving (benötigt auch die ASTAP Sternendatenbank, z.B. H18).
Aladin Desktop: Zur professionellen Betrachtung und Abgleich deiner gelösten FITS-Bilder.
Stellarium: Aktiviere das "Remote Control" Plugin in Stellarium, um Koordinaten direkt aus dem AstroArchive an das Planetarium zu senden.
📝 Lizenz & Credits
Dieses Projekt steht unter der GNU General Public License v3.0.
Entwickelt von Stefan Raphael (2025-2026).
Ohne Gewähr auf Richtigkeit der astronomischen Berechnungen.