# Security Review – jannik000/youtube-dl

Stand: 2026-10-07 · Basis: Upstream `Jeeaaasus/youtube-dl` @ `69c3b69` (yt-dlp 2026.08.19)

## Umfang und Bedrohungsmodell

Geprüft wurden die Web-UI (`root/app/youtube-dl-webui/`), alle Shell-Skripte (`root/entrypoint.sh`,
`root/etc/cont-init.d/*`, `root/app/youtube-dl/*.sh`), die Supervisor-Konfiguration, das `Dockerfile`,
`root/app/requirements.txt` und die GitHub-Actions-Workflows.

Einsatz: Docker Compose auf einer Synology, Web-UI nur im LAN unter `youtube.jannikseuss.de`,
Nutzung vor allem per iOS-Kurzbefehl (`POST /download`).

Angreifer, die berücksichtigt wurden:

| Angreifer | Zugang |
| :--- | :--- |
| A1 – Gerät im LAN | beliebiges Gerät im Heimnetz (Gast, IoT, kompromittierter Rechner) erreicht die Web-UI direkt |
| A2 – fremde Website | eine Website, die jemand im LAN im Browser öffnet, kann Formulare an die Web-UI schicken (CSRF) oder per DNS-Rebinding Requests stellen |
| A3 – gestohlener API-Token | z. B. aus dem iOS-Kurzbefehl |
| A4 – Supply-Chain | kompromittiertes Paket/Release (PyPI, GitHub-Releases, Actions) |

Schweregrade: **Kritisch** (Codeausführung ohne Vorbedingung), **Hoch**, **Mittel**, **Niedrig**, **Info**.

Die mit „verifiziert“ markierten Befunde wurden lokal gegen den unveränderten Upstream-Code reproduziert
(Web-UI unter uvicorn mit Stub-`yt-dlp`, `youtube-dl.sh` mit umgebogenen Pfaden).

## Übersicht

| ID | Befund | Fundstelle | Schweregrad | Status |
| :--- | :--- | :--- | :---: | :--- |
| W1 | Command Injection über die Download-URL | `youtube-dl-webui.py` `download_bg()` | Kritisch | behoben |
| W2 | Keine Authentifizierung der Web-UI | `youtube-dl-webui.py` (alle Routen) | Kritisch | behoben |
| W3 | CSRF auf allen zustandsändernden Endpunkten, Restart per GET | `youtube-dl-webui.py`, Templates | Hoch | behoben |
| W4 | Reflected XSS in `/download/{download_id}` | `dashboard.html` | Hoch | behoben |
| W5 | Option-Injection: URL wird als yt-dlp-Option gelesen | `download_bg()` | Hoch | behoben |
| W6 | Codeausführung über `args.conf`/`channels.txt` (yt-dlp-Optionen) | `save_args()`, `save_channels()` | Hoch | gemindert |
| W7 | SSRF: beliebige Ziel-URLs | `download_url()` | Mittel | gemindert |
| W8 | Log-Endpunkte: Informationsleck, ungeprüfte IDs, Volllast-Polling | `/log/*`, `dashboard.html` | Niedrig | behoben |
| W9 | Fehlende Security-Header (Clickjacking, CSP) | Web-UI | Niedrig | behoben |
| W10 | Unbegrenzte parallele Downloads, Logs in `/tmp` wachsen | `download_url()` | Niedrig | dokumentiert |
| S1 | `eval` der `\|`-Args aus `channels.txt` | `youtube-dl.sh` | Hoch | behoben |
| S2 | Shell-Expansion von `--output`-Zeilen aus `args.conf` | `youtube-dl.sh` | Hoch | behoben |
| S3 | Env-Werte werden als Befehle ausgeführt (`if $var`) | `youtube-dl.sh`, `80-webui` | Info | behoben |
| C1 | Self-Updater lädt yt-dlp ungepinnt zur Laufzeit nach | `updater.sh` | Mittel | behoben (Standard aus) |
| C2 | App-Code gehört dem Laufzeit-User `abc` | `90-user-permissions` | Niedrig | behoben |
| C3 | `PUID=0`/`PGID=0` lässt alles als root laufen | `90-user-permissions` | Niedrig | behoben |
| C4 | Supervisor: Zugangsdaten, `terminate` als root | `supervisord.conf` | Info | kein Handlungsbedarf |
| C5 | Build lädt ffmpeg/deno „latest“ ohne Prüfsumme | `Dockerfile` | Niedrig | gemindert |
| D1 | Python-Abhängigkeiten ungepinnt | `requirements.txt` | Niedrig | behoben |
| G1 | CI: fremde Registry/Secrets, Drittanbieter-Action, veraltete Actions, keine `permissions` | `.github/workflows/*` | Mittel | behoben |
| G2 | Release-Tag wird ungeprüft in `sed` und Image-Tags übernommen | `release-checker.yml`, Build-Workflows | Niedrig | behoben |

## Nachprüfung vor dem PR

Vor dem Merge wurden die Fixes ein zweites Mal geprüft:
- unabhängiges Review in vier Bereichen (Web-UI, Validator, Container/Skripte, CI/Doku), jeder Befund
  von einem zweiten Prüfer gegengeprüft;
- Browser-Test der Web-UI mit Chromium (Login, Formulare, CSRF aus fremder Origin, XSS, Read-only,
  Base-Path, iOS-Endpunkt);
- E2E-Test des echten Images auf GitHub-Runnern (`tests/e2e/run.sh`, Job `e2e` in `ci.yml`).

Dabei fielen folgende Fehler in der ersten Fassung der Fixes auf; alle sind behoben und durch Tests
abgedeckt:

| ID | Befund | Schweregrad | Status |
| :--- | :--- | :---: | :--- |
| R1 | Validator ließ relative Ausgabepfade zu; yt-dlp läuft in `/config`, also hätten `-o`/`--print-to-file` `pre-/post-execution.sh` schreiben können (umgeht W6) | Mittel | behoben |
| R2 | Validator: Alias `--ppa` umging die Denylist; `-o/downloads/a.mp4` wurde fälschlich abgelehnt | Mittel | behoben |
| R3 | `abc` → root: venv im `PATH` der root-Prozesse, `abc` konnte das venv in seinem HOME ersetzen (C2/C4) | Mittel | behoben |
| R4 | Container startete gar nicht: neues Init-Skript ohne Ausführungsrecht (vom E2E-Test gefunden) | Hoch (Betrieb) | behoben |
| R5 | Mit `PUID` ≠ 911 (z. B. 1026) war `/home/abc` (0700) unzugänglich, yt-dlp/Web-UI hätten nicht starten können | Hoch (Betrieb) | behoben |
| R6 | Request-Bodies wurden vor der Auth geparst (unauthentifizierte Uploads nach `/tmp`) | Niedrig | behoben |
| R7 | `/docs`, `/redoc`, `/openapi.json` ohne Auth erreichbar | Niedrig | behoben |
| R8 | Images wurden ohne Tests veröffentlicht, auch für jeden Branch | Niedrig | behoben |
| R9 | `youtubedl_interval` mit mehreren Einheiten (`1d 3h`) führte zu Download-Durchläufen direkt hintereinander | Mittel | behoben |
| R10 | Nicht parsebare `\| args` → URL wurde ohne ihre Limits geladen; leeres letztes Argument ging verloren | Niedrig | behoben |
| R11 | CSP blockierte Semantic-UI-Schriften; `app.js` 404 hinter präfix-entfernendem Proxy; URLs mit Leerzeichen am Rand abgelehnt | Niedrig | behoben |
| R12 | Doku beschrieb Mechanismen, die so nicht umgesetzt waren (Nonce, `tojson`, Updater in `v<VERSION>`) | Niedrig | behoben |
| R13 | Zwei Unit-Tests bestanden aus dem falschen Grund, ein E2E-Check konnte nie bestehen, Lücken in der Abdeckung | Mittel | behoben |

Eine zweite Nachprüfung (gleiches Verfahren) bestätigte, dass R1–R13 behoben sind, und fand:

| ID | Befund | Schweregrad | Status |
| :--- | :--- | :---: | :--- |
| R14 | Validator trimmte `-o`/`--print-to-file`/`--download-archive`, yt-dlp nicht: ein führendes Leerzeichen machte den Pfad relativ (→ `/config/ /downloads/…`) | Niedrig | behoben |
| R15 | `--write-pages` schreibt Debug-Dumps ins Arbeitsverzeichnis `/config` | Niedrig | behoben |
| R16 | Kaputte URLs (IPv6-Klammern, NFKC-Tricks) führten zu 500 statt 400; 500er ohne Security-Header | Niedrig | behoben |
| R17 | Zwei Regressionstests (CSP-Fonts, strikte UUID) hätten ein Zurückdrehen nicht bemerkt; E2E bewies nicht, dass yt-dlp unter Supervisor wirklich lief | Niedrig | behoben |
| R18 | ffmpeg-Binaries gehörten uid 1001 (siehe C5) | Niedrig | behoben |
| R19 | `release.yml` führte die neue, ungeprüfte yt-dlp-Version im selben Job aus, der Schreibrechte auf Repo und Registry hatte | Mittel | behoben |
| R20 | Version wurde vor dem Image-Build committet; ein fehlgeschlagener Build wurde nie wiederholt | Niedrig | behoben |
| R21 | `release.yml` ließ sich von jedem Branch aus starten und hätte `:latest` aus ungemergtem Code gebaut | Niedrig | behoben |
| R22 | `youtubedl_webuipath=/` (bisheriger Doku-Default) erzeugte `//…`-Links | Niedrig | behoben |
| R23 | Doku: Supervisor-Befehlsauflösung, CSP-Details (W9) | Niedrig | behoben |
| R24 | Ein öffentliches Branch-Image (`:claude-init-ftoj5p`, Stand vor R1/R3) liegt noch in GHCR | Niedrig | **manuell löschen** |

Eine abschließende Prüfung nur der Änderungen aus der zweiten Nachprüfung fand noch:

| ID | Befund | Schweregrad | Status |
| :--- | :--- | :---: | :--- |
| R25 | Validator: `-P home` tief + `-P temp:`/Typ-Verzeichnis flach + relatives `-o '../…'` verließ `/downloads` (yt-dlp schreibt nach `home/typ/template`); jedes Template wird jetzt gegen jede Basis geprüft | Niedrig | behoben |
| R26 | `release.yml`: Versions-Commit scheiterte, wenn `master` sich während des Laufs bewegte; Ausgaben des nicht vertrauenswürdigen `verify`-Jobs wurden in `publish` nicht erneut geprüft; QEMU-Image aus dem gemeinsamen Actions-Cache lief privilegiert | Niedrig | behoben |
| R27 | Doku: in Forks laufen geplante Workflows erst nach manuellem Aktivieren, `:latest` entsteht nicht durch den Merge; Optionsliste in W6 unvollständig | Niedrig | behoben |

Nach dem Merge fielen zwei Fehler auf, die schon im Upstream-Code steckten (Hotfix, lokal reproduziert:
Supervisor 4.2.5 mit Stub-Skript, `download_bg()` mit Python-Kindprozess statt yt-dlp):

| ID | Befund | Schweregrad | Status |
| :--- | :--- | :---: | :--- |
| R28 | `root/etc/supervisor/conf.d/youtube-dl.conf`: `supervisorctl restart youtube-dl` (Web-UI „Restart youtube-dl“, `POST /restart-youtube-dl`) beendete nur das äußere `bash -c`. `youtube-dl.sh`, sein yt-dlp und `tee` liefen verwaist weiter, neben dem neuen Durchlauf (gleiches Archiv, gleiche Dateien, doppelte Anfragen an YouTube; das alte `tee` schrieb weiter in das vom neuen gekürzte Log). Fix: `stopasgroup`/`killasgroup`: Stop und Restart durch supervisord schicken SIGTERM an die ganze Gruppe. SIGKILL an die Gruppe folgt nur, wenn `bash -c` nach `stopwaitsecs` noch läuft; ein Mitglied, das SIGTERM ignoriert (etwa ein Nutzer-Skript mit `trap '' TERM`), überlebt deshalb, und ein unerwarteter Tod von `bash -c` erreicht die Gruppe gar nicht. Folge in `root/app/youtube-dl/youtube-dl.sh`: Der Zweig für `youtubedl_interval=false` startet nur noch `terminate` und wartet dann (`sleep infinity`); `supervisorctl stop all` hätte das Skript jetzt selbst beendet, bevor `terminate` startet (Container endet nie), ein Beenden des Skripts hätte per `autorestart` einen zweiten Durchlauf begonnen. Exit-Code des Containers wie bisher 0 (supervisord endet nach SIGQUIT mit 0). E2E: Szenario A prüft, dass der Restart wirklich einen neuen Durchlauf startet, und danach genau ein `youtube-dl.sh` und ein `tee`; Szenario E den Ein-Durchlauf-Modus (genau ein Start durch supervisord, Container beendet mit 0) | Mittel (Betrieb) | behoben |
| R29 | `root/app/youtube-dl-webui/youtube-dl-webui.py` `download_bg()`: Die Ausgabe wurde zeilenweise gelesen; eine Zeile über 64 KiB (Fortschritt ohne `--newline` in `args.conf`, langes `--print`) brach das Lesen mit `ValueError` ab. Danach las niemand mehr die Pipe: Sobald sie voll war, blockierte yt-dlp für immer, und der Prozess wurde nie abgewartet; das Log endete nach den bis dahin gelesenen Zeilen mit `Error: Separator is (not) found …`. Fix: Kopieren in 64-KiB-Blöcken mit inkrementellem UTF-8-Decoder bis EOF, danach `wait()`. yt-dlp läuft in einer eigenen Session; bei Fehler oder Abbruch der Task wird diese Prozessgruppe getötet (auch ffmpeg und externe Downloader, die yt-dlps Ausgabe teilen), die Pipe geleert und yt-dlp abgewartet (vor Python 3.13 kehrt `wait()` in asyncio erst zurück, wenn die Pipe geschlossen ist; das Image nutzt 3.11). Tests: `root/app/youtube-dl-webui/tests/test_download_bg.py`, mit asyncio und uvloop | Niedrig (Betrieb) | behoben |

---

## Web-UI

### W1 – Command Injection über die Download-URL · Kritisch · verifiziert

**Fundstelle:** `root/app/youtube-dl-webui/youtube-dl-webui.py`, `download_bg()` (Zeilen 19–23 im Original).

```python
result = await asyncio.create_subprocess_shell(
    f'{youtubedl_binary} \'{url}\' --no-playlist-reverse ... {youtubedl_args_format}', ...)
```

**Ausnutzbarkeit:** Die URL aus dem Formularfeld wird in einfache Anführungszeichen gesetzt und an
`/bin/sh` übergeben. Ein `'` in der URL beendet das Quoting, der Rest wird als Shell-Befehl ausgeführt
(als User `abc`, mit Schreibzugriff auf `/config` und `/downloads`). Ohne Authentifizierung (W2) und
ohne CSRF-Schutz (W3) reicht dafür ein einziger Request aus dem LAN **oder** ein Formular auf einer
beliebigen Website, die jemand im LAN aufruft. Lokal mit einem Request inkl. fremdem `Origin`-Header
reproduziert: Der eingeschleuste Befehl legte eine Datei an.

**Fix:** `asyncio.create_subprocess_exec()` mit Argumentliste, kein Shell-Aufruf mehr. Die URL steht
nach `--` am Ende. Die Format-Args werden als Liste übergeben (`['--format', fmt]`) statt als
zusammengesetzter String; dadurch ist auch `shlex.split` nicht nötig. Zusätzlich URL-Validierung (W7).
Ein grep-Check in den Tests stellt sicher, dass weder `shell=True` noch `create_subprocess_shell`
im Code vorkommen.

### W2 – Keine Authentifizierung · Kritisch · verifiziert

**Fundstelle:** alle Routen in `youtube-dl-webui.py`.

**Ausnutzbarkeit:** Jedes Gerät im LAN kann Downloads starten, Logs lesen, yt-dlp neu starten und
`args.conf`, `channels.txt` und `archive.txt` überschreiben. Über `args.conf` sind yt-dlp-Optionen
mit Codeausführung setzbar (W6), über `channels.txt` sogar direkt Shell-Code (S1). Lokal
reproduziert: `args.conf` ließ sich ohne Zugangsdaten mit beliebigem Inhalt überschreiben.

**Fix:**
- Browser: HTTP Basic Auth (`WEBUI_USERNAME`, `WEBUI_PASSWORD`).
- Kurzbefehl/Skripte: `Authorization: Bearer <WEBUI_API_TOKEN>`. Der Token gilt **nur** für
  `POST /download` und `POST /api/download` (beide antworten bei Token-Auth mit JSON `{"id": …}`),
  nicht für Logs, Restart oder Config. Ein gestohlener Token (A3) erlaubt also nur Downloads.
- Die Prüfung läuft in einer Middleware anhand der Header, **bevor** eine Route oder das
  Body-Parsing startet. Bodies nicht authentifizierter Clients werden nie gelesen oder nach `/tmp`
  gespoolt. Ohne Login erreichbar sind nur `/favicon.ico` und `/static/app.js`; auch unbekannte
  Routen liefern 401. Die Routen prüfen zusätzlich selbst (zweite Schicht).
- Die automatische API-Doku von FastAPI (`/docs`, `/redoc`, `/openapi.json`) ist abgeschaltet.
- Vergleich mit `secrets.compare_digest` auf Bytes.
- Fail closed: Ist die Web-UI aktiv, aber weder Basic-Zugangsdaten noch Token gesetzt (oder nur
  Benutzername ohne Passwort), startet sie nicht. `80-webui` meldet das gut sichtbar im
  Container-Log; die Python-App bricht zusätzlich selbst ab.
- Zugangsdaten nur aus Env-Variablen, nirgends im Repo.

**Restrisiko:** Basic Auth und Bearer-Token sind nur über HTTPS vertraulich (siehe README). Es gibt
keine Sperre nach Fehlversuchen; lange, zufällige Werte verwenden.

### W3 – CSRF, Restart per GET · Hoch · verifiziert

**Fundstelle:** `POST /download`, `POST /edit/*/save`, `GET /restart-youtube-dl`; Formulare in den Templates.

**Ausnutzbarkeit:** Alle Endpunkte nehmen einfache Formular-POSTs ohne Token an, der Restart sogar
per GET (ein `<img>`-Tag genügt). Eine fremde Website (A2) kann damit im Browser eines LAN-Nutzers
W1 auslösen oder die Config überschreiben, ohne dass der Nutzer etwas merkt. Nach Einführung von
Basic Auth wird es nicht besser: Browser senden gespeicherte Basic-Zugangsdaten bei Cross-Site-Requests
automatisch mit. Per DNS-Rebinding ist zudem Lesen möglich, solange keine Auth existiert.

**Fix:**
- CSRF-Token (zufällig pro Prozessstart) als verstecktes Feld in allen Formularen, geprüft mit
  `compare_digest`.
- Zusätzlich wird `Sec-Fetch-Site` geprüft: `cross-site` und `same-site` werden abgelehnt, auch
  wenn der Token stimmt. `same-site` umfasst andere Subdomains von `jannikseuss.de`.
- Restart nur noch per `POST` mit Token.
- Requests mit gültigem Bearer-Token sind von der CSRF-Prüfung ausgenommen: Browser setzen
  `Authorization`-Header nicht von sich aus, und es gibt keine CORS-Freigabe.
- DNS-Rebinding wird durch die Pflicht-Auth entschärft (der Browser hat für die fremde Domain
  keine Zugangsdaten).

### W4 – Reflected XSS in `/download/{download_id}` · Hoch · verifiziert

**Fundstelle:** `templates/dashboard.html`, `fetch(\`{{ base_path }}/log/download/{{ download_id }}\`)`;
`download_status()` prüft `download_id` nicht.

**Ausnutzbarkeit:** `download_id` kommt ungeprüft aus dem Pfad und landet in einem
JavaScript-Template-Literal. Jinjas HTML-Autoescaping escapt `` ` `` und `${` nicht, deshalb wird ein
`${…}`-Ausdruck im Pfad im Browser ausgeführt (lokal im ausgelieferten HTML bestätigt). Ein Link reicht;
bei aktiver Basic Auth läuft das Skript mit den Rechten des angemeldeten Nutzers. Damit wären
CSRF-Token und Origin-Prüfung aus W3 wirkungslos: Das Skript läuft auf derselben Origin, kann den
Token lesen und die Config ändern.

**Fix:** `download_id` muss eine UUID in kanonischer Schreibweise sein (sonst 404). Es gibt keine
Inline-Skripte mehr: Das Log-Polling liegt in `static/app.js`, das seine Parameter aus
`data-*`-Attributen liest (HTML-escaped, nie in Skript-Quelltext eingesetzt). Die CSP erlaubt
Skripte nur von `'self'` (W9), eingeschleuster Inline-Code würde also auch sonst nicht laufen.

### W5 – Option-Injection über die URL · Hoch · verifiziert

**Fundstelle:** `download_bg()`.

**Ausnutzbarkeit:** Auch ohne Shell wird ein „URL“-Wert, der mit `-` beginnt, von yt-dlp als Option
gelesen (z. B. `--exec=…`). Lokal bestätigt: Der Stub erhielt den Wert als erstes Argument. Damit
wäre ein reiner Umbau auf `exec` ohne weitere Maßnahmen weiterhin Codeausführung.

**Fix:** `--` vor der URL (mit yt-dlps eigenem Parser geprüft: danach wird alles als URL behandelt)
und die Schema-Prüfung aus W7.

### W6 – Codeausführung über `args.conf`/`channels.txt` · Hoch · verifiziert

**Fundstelle:** `save_args()`, `save_channels()`; Verarbeitung in `youtube-dl.sh`.

**Ausnutzbarkeit:** Wer diese Dateien schreiben kann, kann über yt-dlp-Optionen Code ausführen, unter
anderem mit `--exec`, `--netrc-cmd`, `--plugin-dirs`, `--use-postprocessor` und `--ffmpeg-location`.
Möglich ist auch, Dateien zu schreiben, die später ausgeführt werden: `-o`/`--paths`/`--print-to-file`
auf `/config/pre-execution.sh` oder `post-execution.sh`. Mit yt-dlps Parser lokal bestätigt: Die
Abkürzungen `--plugin-d` und `--config-loc` werden akzeptiert, und mit
`--alias <name> '--exec {0}'` lässt sich ein neuer Optionsname für `--exec` definieren. Eine reine
String-Denylist ist deshalb trivial zu umgehen.

**Fix (Defense in Depth, die eigentliche Grenze bleibt die Authentifizierung):**
- `WEBUI_READONLY=true`: Config-Dateien sind in der Web-UI nur lesbar, Speichern liefert 403.
- Beim Speichern wertet yt-dlps eigener Parser (`yt_dlp.parse_options`) den Inhalt aus. Er löst
  Abkürzungen, gebündelte Kurzoptionen und `--alias` so auf wie yt-dlp selbst. Abgelehnt wird über
  die ausgewerteten Optionswerte, nicht über Strings. Details und Grenzen siehe
  „Grenzen der Options-Prüfung“ unten.
- Abschaltbar mit `WEBUI_ALLOW_UNSAFE_ARGS=true` für bewusst gewollte Sonderfälle.

### W7 – SSRF über beliebige Ziel-URLs · Mittel

**Fundstelle:** `download_url()`.

**Ausnutzbarkeit:** yt-dlps generischer Extractor ruft jede übergebene URL ab, also auch interne
Dienste (Router, NAS-Admin, `localhost` im Container). Die Antworten landen in `/downloads`, nicht in
der Web-UI; das Leck ist also „blind“, Requests mit Seiteneffekten sind aber möglich. Andere Schemata
(`file:` usw.) lehnt yt-dlp standardmäßig selbst ab.

**Fix:** Nur `http`/`https` mit Hostname. Abgelehnt werden Zugangsdaten in der URL (`user@host`),
Backslashes, Leer- und Steuerzeichen und URLs über 2048 Zeichen. Optional gibt es eine
Domain-Allowlist `WEBUI_ALLOWED_DOMAINS`, z. B. `youtube.com,youtu.be`; Subdomains sind dabei
eingeschlossen. Ohne Allowlist bleibt SSRF für Inhaber gültiger Zugangsdaten möglich (dokumentiert).

### W8 – Log-Endpunkte · Niedrig · verifiziert

**Fundstelle:** `/log/youtube-dl`, `/log/download/{filename}`, `dashboard.html`.

**Befund:**
- Ohne Auth liest jeder im LAN das komplette Log mit Kanallisten, Titeln, Namen privater
  Playlists und Pfaden.
- Fehlermeldungen geben interne Pfade und Exception-Texte zurück (lokal bestätigt).
- Path-Traversal ist **nicht** möglich: Starlette dekodiert `%2F` vor dem Routing, `{filename}`
  matcht kein `/`, und Präfix/Suffix sind fest (lokal geprüft, liefert 404). Trotzdem wurde
  `filename` nicht validiert.
- Das Dashboard lädt das komplette, unbegrenzt wachsende Log alle 200 ms neu.

Positiv: Die Log-Anzeige nutzt `textContent`, nicht `innerHTML`. Damit gibt es keine XSS über Log-Inhalte.

**Fix:** Auth (W2). Die ID wird als UUID validiert (sonst 404). Es gibt nur noch generische
Fehlermeldungen. Beide Log-Endpunkte liefern nur die letzten 256 KiB, das Polling-Intervall ist 2 s.

### W9 – Fehlende Security-Header · Niedrig

**Befund:** Kein `frame-ancestors`/`X-Frame-Options`, also Clickjacking auf „Save“/„Restart“ möglich.
Keine CSP. Das Semantic-UI-CSS kommt vom CDN ohne SRI.

**Fix:** Middleware setzt:
- `Content-Security-Policy`: `default-src 'self'`, `script-src 'self'` (keine Inline-Skripte),
  `style-src 'self' 'unsafe-inline'` (Style-Attribute in den Templates) plus `cdn.jsdelivr.net`
  und `fonts.googleapis.com`, `font-src 'self' data:` plus `cdn.jsdelivr.net` und
  `fonts.gstatic.com` (Semantic UI lädt Lato von Google Fonts und bettet seine Icon-Fonts als
  `data:`-URIs ein), `img-src 'self' data:`, `connect-src 'self'`, `frame-ancestors 'none'`,
  `form-action 'self'`, `base-uri 'none'`, `object-src 'none'`. `'unsafe-inline'` für Styles ist
  die einzige Lockerung: Sollte je eine HTML-Injection gefunden werden, wäre CSS-Injection möglich,
  Skript-Ausführung nicht.
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: same-origin`
- `X-Frame-Options: DENY`

Das CDN-Stylesheet bleibt; per CSP ist es auf CSS/Fonts beschränkt.

### W10 – Ressourcenverbrauch · Niedrig

Jeder Download-Request startet einen eigenen yt-dlp-Prozess ohne Obergrenze. Wegen
`--playlist-end -1` lädt eine Kanal- oder Playlist-URL den kompletten Kanal. Pro Download bleibt eine
Logdatei in `/tmp` liegen. Nach Einführung der Auth können das nur noch Berechtigte auslösen;
dokumentiert, nicht geändert.

---

## Shell-Skripte

### S1 – `eval` der `|`-Args aus `channels.txt` · Hoch · verifiziert

**Fundstelle:** `root/app/youtube-dl/youtube-dl.sh`, `eval "$exec $extra_url_args"`.

**Ausnutzbarkeit:** Alles hinter `|` in einer Zeile von `channels.txt` wird von der Shell ausgewertet.
Ein Shell-Metazeichen wie `;` genügt, um beim nächsten Durchlauf beliebige Befehle als `abc`
auszuführen; lokal reproduziert. Über die Web-UI (W2) war `channels.txt` ohne Auth schreibbar.

**Fix:** Kein `eval` mehr. Der Befehl ist ein Bash-Array; die `|`-Args werden mit Pythons `shlex`
(POSIX-Modus) in ein Array zerlegt. Anführungszeichen wie in den README-Beispielen funktionieren weiter,
Shell-Syntax (`;`, `$(…)`, Variablen) wird nicht mehr interpretiert. Die Zerlegung entspricht jetzt
der des Web-UI-Validators.

### S2 – Shell-Expansion von `--output` aus `args.conf` · Hoch · verifiziert

**Fundstelle:** `youtube-dl.sh`: Zeilen `^(--output |-o ).*\$\(` werden zusätzlich roh an den
`eval`-String gehängt.

**Ausnutzbarkeit:** Eine `--output`-Zeile mit `$(…)` in doppelten Anführungszeichen führt den Inhalt
als Shell-Befehl aus; lokal reproduziert. Über die Zeile lässt sich auch beliebiger weiterer
Shell-Code anhängen.

**Fix:** Feature entfernt. Datumsangaben usw. gehen mit yt-dlp-Templates (z. B.
`%(upload_date>%Y)s`). **Breaking Change** für Konfigurationen, die `$(…)` in `--output` nutzen.

### S3 – Env-Werte werden als Befehle ausgeführt · Info

`if $youtubedl_webui`, `if $youtubedl_debug` usw. führen den Wert der Variable als Befehl aus. Env-Werte
setzt nur der Betreiber, die Vertrauensgrenze wird also nicht überschritten. `True` (großgeschrieben)
oder Tippfehler führen aber zu `command not found` und stillem Fehlverhalten.
**Fix:** In den angefassten Skripten Vergleich mit `[ "$var" = true ]`.

### Weitere geprüfte Punkte ohne Befund

- `pre-execution.sh`/`post-execution.sh` werden bewusst als `abc` ausgeführt. Die Web-UI kann sie
  nicht schreiben, yt-dlp-Ausgabepfade auf `/config` werden vom Validator abgelehnt (W6).
- `05-default-confs` (root) kopiert nach `/config`, wenn `args.conf` fehlt. Ein von `abc` angelegter
  hängender Symlink wird von GNU `cp` nicht verfolgt („not writing through dangling symlink“, geprüft).
- Die Temp-Dateien in `/tmp` werden nur vom User `abc` genutzt; im Container gibt es keine weiteren
  unprivilegierten Nutzer, Race Conditions sind daher nicht relevant.
- `entrypoint.sh` führt nur die festen `cont-init.d`-Skripte aus dem Image aus.

---

## Container, Rechte, Supply-Chain

### C1 – Self-Updater · Mittel

**Fundstelle:** `root/app/youtube-dl/updater.sh`, `youtube-dl-updater.conf`.

**Befund:** Alle 3 h installiert `pip install --upgrade yt-dlp[default]` die jeweils neueste Version von
yt-dlp **und** fehlender oder veralteter Abhängigkeiten von PyPI. Das geschieht ohne Pin, ohne
Hash-Prüfung und in den laufenden Container, in dasselbe venv wie die Web-UI. Im `unstable`-Image
kommt der Code sogar direkt vom Git-`master`. Ein kompromittiertes Release oder Konto (A4) führt
damit innerhalb von Stunden zu Codeausführung, ohne dass ein neues Image gebaut oder geprüft wird.
Auch der Rückweg ist schwierig: Nach einem Container-Neustart ist das alte Paket schon ersetzt.

**Fix:** Neue Variable `youtubedl_autoupdate`, **Standard `false`** (nur das rollende `:unstable`
wird mit `true` gebaut). `youtubedl_autoupdate=true` stellt das alte Verhalten her. Aktuelles yt-dlp
kommt stattdessen über neue Images, die die CI bei jedem yt-dlp-Release automatisch baut und vorher
testet (`docker compose pull`). Anders als upstream enthalten auch die `v<VERSION>`-Images den
(abgeschalteten) Updater; wer ihn dort per Env einschaltet, verlässt die gepinnte Version.

### C2 – App-Code gehört dem Laufzeit-User · Niedrig

**Fundstelle:** `root/etc/cont-init.d/90-user-permissions` (`chown -R abc:abc /app` und
`/home/abc` samt venv), `Dockerfile` (`PATH` mit dem venv vorn), Supervisor-Konfiguration.

**Befund:** Wer einmal Code als `abc` ausführt, kann `/app` (Web-UI, Download-Skript) und das venv
mit yt-dlp verändern und sich bis zur Neuerstellung des Containers festsetzen. Schlimmer (in der
Nachprüfung gefunden, R3): Das venv stand im `PATH` aller **root**-Prozesse: Init-Skripte bei jedem
Start, supervisord und das root-`terminate`, das `abc` per `supervisorctl` starten darf. Ein von
`abc` abgelegtes `/home/abc/.venv/bin/cat` o. Ä. wäre damit als root gelaufen.

**Fix:**
- `/app` und das venv bleiben `root`-eigen und nur lesbar. Das venv liegt jetzt unter `/opt/venv`,
  also in einem root-eigenen Elternverzeichnis; `abc` kann es auch nicht umbenennen und ersetzen.
  An `abc` übergeben wird es nur bei `youtubedl_autoupdate=true`.
- `entrypoint.sh` setzt für alle root-Prozesse einen `PATH` nur aus Systemverzeichnissen. Die
  `abc`-Programme bekommen das venv per `environment=` in ihrer Supervisor-Konfiguration, uvicorn
  wird mit absolutem Pfad gestartet, `terminate.sh` nutzt nur absolute Pfade.
- `abc`s HOME (`/home/abc`) gehört immer `abc`, auch nach einem `PUID`-Wechsel (sonst kein Cache).
- `chown -R` auf `/config` und `/downloads` bleibt; Hardlinks aus dem Container-Dateisystem in diese
  Volumes sind nicht möglich, weil sie auf anderen Dateisystemen liegen.
- Der E2E-Test prüft root-Eigentum, die Unersetzbarkeit des venv und den `PATH` von supervisord.

**Restrisiko:** Mit `youtubedl_autoupdate=true` gehört das venv `abc`. Ein `docker exec` als root,
das `yt-dlp` o. Ä. über den Image-`PATH` aufruft, würde dann von `abc` kontrollierten Code ausführen.

### C3 – `PUID=0`/`PGID=0` · Niedrig

`usermod -o -u 0 abc` macht `abc` zu root; alle Prozesse inklusive Web-UI laufen dann als root.
**Fix:** `90-user-permissions` bricht mit klarer Meldung ab, wenn `PUID` oder `PGID` 0 ist.

### C4 – Supervisor · Info

Die Zugangsdaten für den Unix-Socket werden beim Start zufällig erzeugt (`91-supervisor-credentials`,
jetzt mit getrenntem Benutzernamen und Passwort). Die Konfiguration ist für `abc` lesbar; das ist
nötig, damit die Web-UI `supervisorctl restart` ausführen kann. `abc` kann damit nur die definierten
Programme steuern. supervisord und `terminate` laufen als **root**; `abc` darf `terminate` starten.
Das ist nur ein DoS gegen den eigenen Container, **sofern** root keine von `abc` kontrollierten
Programme ausführt. Die ursprüngliche Einschätzung „keine Rechteausweitung“ stimmte deshalb erst
nach dem Fix aus C2 (bereinigter root-`PATH`, absolute Pfade in `terminate.sh`). Der Socket liegt
unter `/etc/supervisor` mit Modus 0700 und gehört `abc`.

### C5 – Build lädt ffmpeg/deno „latest“ ohne Prüfsumme · Niedrig

**Fundstelle:** `Dockerfile`.

**Befund:** ffmpeg (yt-dlp/FFmpeg-Builds bzw. johnvansickle.com) und deno werden als „latest“ ohne
Prüfsumme heruntergeladen; das Basis-Image ist nicht per Digest gepinnt. Eine Prüfsumme von derselben
Quelle schützt nur gegen Übertragungsfehler, nicht gegen ein kompromittiertes Release.

Zusätzlich lud arm64 ffmpeg von einem privaten Server (johnvansickle.com), dessen fehlerhafte
Antwort einen Build brach (`xz: File format not recognized`); `wget -q … | tar` erkannte den
Download-Fehler gar nicht. Die Archive gehören außerdem uid 1001, und tar übernimmt das als root:
Mit `PUID=1001` hätte `abc` die ffmpeg-Binaries ändern können (R18).

**Fix (gemindert):** ffmpeg kommt für beide Architekturen von yt-dlp/FFmpeg-Builds (`linux64`,
`linuxarm64`). ffmpeg und deno werden mit Wiederholungen geladen, vor dem Entpacken gegen die im
selben Release veröffentlichte Prüfsumme geprüft (`set -eux`) und root-eigen installiert. CI baut
zusätzlich das arm64-Image bei jedem Push. Das schützt vor kaputten oder abgeschnittenen Downloads,
nicht vor einem kompromittierten Release.

**Empfehlung (nicht umgesetzt, um Upstream-Merges nicht zu erschweren):** Versionen und SHA-256 im
`Dockerfile` fest pinnen und per Dependabot/Renovate aktualisieren; Basis-Image per Digest pinnen.

### D1 – Python-Abhängigkeiten ungepinnt · Niedrig

**Fundstelle:** `root/app/requirements.txt`.

**Befund:** Die Abhängigkeiten sind ungepinnt; jeder Build zieht, was gerade aktuell ist. `pip-audit`
meldet für die heute aufgelösten Versionen **keine bekannten Schwachstellen** (2026-10-07). Frühere
CVEs zeigen aber, dass der Stack relevant ist:
- Starlette CVE-2024-47874 und CVE-2025-54121 (Multipart-DoS)
- Starlette CVE-2025-62727 (Range-Header-ReDoS in `FileResponse`, das hier für das Favicon genutzt wird)
- python-multipart CVE-2024-24762 und CVE-2024-53981 (DoS)
- h11 CVE-2025-43859 (Request Smuggling)
- Jinja2 CVE-2024-56201, CVE-2024-56326 und CVE-2025-27516 (Sandbox; hier nicht nutzbar, da keine
  Templates von außen kommen)

**Fix:** Direkte und transitive Abhängigkeiten der Web-UI mit exakten Versionen gepinnt, `pip-audit`
läuft in der CI, Dependabot hält die Pins aktuell. yt-dlp selbst steht bewusst nicht in
`requirements.txt`; die Image-Builds pinnen es per Build-Arg auf das jeweilige Release.

**Restrisiko:** Die Abhängigkeiten von yt-dlp (`brotli`, `certifi`, `mutagen`, `pycryptodomex`,
`requests`, `urllib3`, `websockets`, `yt-dlp-ejs`, …) sind nicht gepinnt und werden von `pip-audit`
nicht erfasst; sie kommen beim Build in der jeweils aktuellen Version.

### G1 – GitHub Actions · Mittel

**Fundstelle:** `.github/workflows/*.yml`.

**Befunde:**
- Images gehen an das Docker-Hub-Konto des Upstream-Autors (`jeeaaasustest`), mit Secrets
  `DOCKERHUB_*`.
- `release-checker.yml` braucht einen PAT (`REPO_SCOPED_TOKEN`) mit Schreibrechten.
- Die Drittanbieter-Action `FranzDiebold/github-env-vars-action@v2` läuft im selben Job vor Login und
  Build. Sie könnte Dockerfile oder Workspace manipulieren und ist per veränderlichem Tag eingebunden.
- Alle Actions sind per Tag statt Commit-SHA eingebunden und stark veraltet (`@v1`/`@v2`, Node 12).
- Keine `permissions:`, also gelten die Repository-Standardrechte für `GITHUB_TOKEN`.
- Es wird das veraltete `::set-output` verwendet.

**Fix:**
- Push nach `ghcr.io/jannik000/youtube-dl` mit `GITHUB_TOKEN` (`packages: write`), keine fremden Secrets.
- Die Drittanbieter-Action ist entfernt; stattdessen `github.ref_name` und Bash.
- Actions auf aktuelle Versionen per Commit-SHA gepinnt, `permissions` minimal pro Workflow.
- `release.yml` (nur `master`) prüft zweimal täglich auf ein neues yt-dlp-Release. Job `verify`
  läuft nur lesend und ohne gespeicherte Zugangsdaten: Er testet die neue Version (pytest und E2E
  mit genau dieser Version). Nur dort läuft neuer, noch ungeprüfter yt-dlp-Code (R19). Job
  `publish` hat Schreibrechte, führt yt-dlp nicht auf dem Runner aus, baut und pusht die Images und
  committet die Version erst danach. Fehlt das Image zur aktuellen Version, wird beim nächsten
  Lauf gebaut, ein fehlgeschlagener Publish wird also wiederholt (R20). Kein PAT nötig.
- `ci.yml` (pytest, `pip-audit`, shellcheck, E2E, arm64-Build) läuft für alle Pushes und Pull
  Requests. `:unstable` wird nur auf `master` und erst nach allen Tests gepusht. Für andere
  Branches, PRs und Dependabot werden keine Images mehr veröffentlicht (R8).

### G2 – Release-Tag ungeprüft · Niedrig

Der Tag-Name aus der GitHub-API von yt-dlp landet ungeprüft in `release-versions/latest.txt`. Von dort
geht er in einen `sed`-Ausdruck auf das `Dockerfile` und in Image-Tags. **Fix:** Er wird gegen
`^[0-9]{4}\.[0-9]{2}\.[0-9]{2}(\.[0-9]+)?$` geprüft, sonst bricht der Workflow ab.

---

## Grenzen der Options-Prüfung (W6)

Die Prüfung beim Speichern ist **Defense in Depth, keine Sandbox**. Die eigentliche Schutzgrenze ist:
Nur Admins (Basic Auth) dürfen schreiben, und mit `WEBUI_READONLY=true` niemand.

So funktioniert sie:
- `args.conf` wird wie von yt-dlp zerlegt (shlex mit Kommentaren). Jede `channels.txt`-Zeile mit `|`
  wird wie in `youtube-dl.sh` zerlegt.
- Die Tokens werden mit yt-dlps eigenem Parser nach optparse-Regeln durchlaufen: Abkürzungen und
  Aliase (z. B. `--ppa` für `--postprocessor-args`) werden auf das **Option-Objekt** aufgelöst,
  Optionswerte, die mit `-` beginnen, und angehängte Kurzoptionswerte (`-o/downloads/a.mp4`) werden
  korrekt als Werte erkannt. Mehrdeutige oder unbekannte Optionen werden abgelehnt.
- Zusätzlich geht alles durch `yt_dlp.parse_options()`, und die resultierenden Optionswerte werden
  geprüft (zweite, namensunabhängige Schicht). Abgelehnt werden:
  - `--exec`/`--exec-before-download` (alle Varianten), `--netrc-cmd`
  - `--plugin-dirs`, `--use-postprocessor`
  - `--config-locations`, `--batch-file`, `--load-info-json`, `--alias`
  - `--ffmpeg-location`, `--downloader`/`--external-downloader` (jeder Wert), `--downloader-args`, `--postprocessor-args`
  - `--write-pages` (schreibt Debug-Dumps ins Arbeitsverzeichnis `/config`, R15)
  - `--cookies-from-browser`, `--cookies`, `--netrc-location`, `--cache-dir`
  - `--output`/`--paths`/`--print-to-file`/`--download-archive`, sofern sie aus `/downloads` hinausführen
    (`--download-archive` darf zusätzlich `/config/archive.txt` sein). Pfade werden aufgelöst wie in
    yt-dlp: relativ zum Arbeitsverzeichnis **`/config`** bzw. zum `--paths`-`home`. Ein relatives
    `-o x` oder `--print-to-file … post-execution.sh` landet also in `/config` und wird abgelehnt
    (in der Nachprüfung gefunden, R1). Werte mit `~` oder `$` werden abgelehnt, weil yt-dlp sie
    abhängig von der Laufzeitumgebung expandiert.
  - `--enable-file-urls`, `--load-info-json`, `--update`/`--update-to`
- Zusätzlich sind Shell-Metazeichen-Konstrukte `$(` und Backticks abgelehnt. Sie würden zwar seit S2
  nicht mehr ausgewertet, deuten aber auf alte, nun wirkungslose Konfiguration hin.

Grenzen:
1. **Denylist:** Künftige yt-dlp-Versionen können neue gefährliche Optionen einführen, die hier nicht
   erfasst sind. Die Tests laufen gegen die gepinnte Version, und `release.yml` testet jede neue
   Version vor der Veröffentlichung; neue *gefährliche* Optionen erkennt das aber nicht automatisch.
2. **Bedeutung statt Syntax:** Erlaubte Optionen können in Kombination unerwünscht wirken, etwa
   `--match-filter`/`--download-sections` zur Ressourcenerschöpfung oder Output-Templates, die sehr
   viele Dateien erzeugen. Ausgabepfade werden vor der Template-Auswertung geprüft. Template-Felder
   wie `%(title)s` können keine Pfadtrenner einschleusen, weil yt-dlp sie sanitisiert; feste `..`
   werden mit aufgelöst und dürfen nicht aus `/downloads` herausführen.
3. **Getrennte Prüfung:** `args.conf` und jede `| args`-Zeile werden einzeln geprüft. Eine Zeile mit
   relativem `-o` wird also auch dann abgelehnt, wenn `args.conf` ein passendes `-P /downloads`
   setzt; in `channels.txt` daher absolute `/downloads/…`-Pfade verwenden.
4. **API-Kopplung:** `yt_dlp.parse_options` und die Parser-Interna (`_match_long_opt`, `_long_opt`)
   sind keine stabil dokumentierte API. Ändert sich ihr Verhalten, schlägt die Prüfung fehl und das
   Speichern wird abgelehnt (fail closed), statt still durchzulassen.
5. **Andere Schreibwege:** Die Prüfung greift nur beim Speichern über die Web-UI. Wer `/config` direkt
   beschreiben kann (Synology-Freigabe, `docker exec`, `docker cp`), umgeht sie; das ist gewollt.
6. **Nicht geprüft:** `archive.txt` enthält nur IDs und wird nicht interpretiert.

## Verbleibende Empfehlungen

- Web-UI nur über HTTPS erreichbar machen (DSM-Reverse-Proxy mit Zertifikat) und den Container-Port nur
  an `127.0.0.1` binden, siehe Compose-Beispiel im README.
- `WEBUI_ALLOWED_DOMAINS` setzen, wenn nur YouTube heruntergeladen wird.
- `WEBUI_READONLY=true`, wenn die Config nur selten geändert wird.
- C5 (Pins und Hashes für ffmpeg/deno/Basis-Image) bei Gelegenheit umsetzen.
