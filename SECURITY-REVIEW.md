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
| W1 | Command Injection über die Download-URL | `youtube-dl-webui.py` `download_bg()` | Kritisch | offen |
| W2 | Keine Authentifizierung der Web-UI | `youtube-dl-webui.py` (alle Routen) | Kritisch | offen |
| W3 | CSRF auf allen zustandsändernden Endpunkten, Restart per GET | `youtube-dl-webui.py`, Templates | Hoch | offen |
| W4 | Reflected XSS in `/download/{download_id}` | `dashboard.html` | Hoch | offen |
| W5 | Option-Injection: URL wird als yt-dlp-Option gelesen | `download_bg()` | Hoch | offen |
| W6 | Codeausführung über `args.conf`/`channels.txt` (yt-dlp-Optionen) | `save_args()`, `save_channels()` | Hoch | offen |
| W7 | SSRF: beliebige Ziel-URLs | `download_url()` | Mittel | offen |
| W8 | Log-Endpunkte: Informationsleck, ungeprüfte IDs, Volllast-Polling | `/log/*`, `dashboard.html` | Niedrig | offen |
| W9 | Fehlende Security-Header (Clickjacking, CSP) | Web-UI | Niedrig | offen |
| W10 | Unbegrenzte parallele Downloads, Logs in `/tmp` wachsen | `download_url()` | Niedrig | offen |
| S1 | `eval` der `\|`-Args aus `channels.txt` | `youtube-dl.sh` | Hoch | offen |
| S2 | Shell-Expansion von `--output`-Zeilen aus `args.conf` | `youtube-dl.sh` | Hoch | offen |
| S3 | Env-Werte werden als Befehle ausgeführt (`if $var`) | `youtube-dl.sh`, `80-webui` | Info | offen |
| C1 | Self-Updater lädt yt-dlp ungepinnt zur Laufzeit nach | `updater.sh` | Mittel | offen |
| C2 | App-Code gehört dem Laufzeit-User `abc` | `90-user-permissions` | Niedrig | offen |
| C3 | `PUID=0`/`PGID=0` lässt alles als root laufen | `90-user-permissions` | Niedrig | offen |
| C4 | Supervisor: Zugangsdaten, `terminate` als root | `supervisord.conf` | Info | kein Handlungsbedarf |
| C5 | Build lädt ffmpeg/deno „latest“ ohne Prüfsumme | `Dockerfile` | Niedrig | offen |
| D1 | Python-Abhängigkeiten ungepinnt | `requirements.txt` | Niedrig | offen |
| G1 | CI: fremde Registry/Secrets, Drittanbieter-Action, veraltete Actions, keine `permissions` | `.github/workflows/*` | Mittel | offen |
| G2 | Release-Tag wird ungeprüft in `sed` und Image-Tags übernommen | `release-checker.yml`, Build-Workflows | Niedrig | offen |

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
  `POST /download` und `POST /api/download`, nicht für Logs, Restart oder Config. Ein gestohlener
  Token (A3) erlaubt also nur Downloads.
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

**Fix:** `download_id` muss eine UUID sein (sonst 404). Werte in JavaScript-Kontexten werden mit
`|tojson` eingesetzt. Zusätzlich gibt es eine CSP mit Nonce für Inline-Skripte (W9).

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
Fehlermeldungen. Ausgeliefert werden nur die letzten 256 KiB, das Polling-Intervall ist 2 s.

### W9 – Fehlende Security-Header · Niedrig

**Befund:** Kein `frame-ancestors`/`X-Frame-Options`, also Clickjacking auf „Save“/„Restart“ möglich.
Keine CSP. Das Semantic-UI-CSS kommt vom CDN ohne SRI.

**Fix:** Middleware setzt:
- `Content-Security-Policy`: `default-src 'self'`, Skripte nur mit Nonce, Styles/Fonts zusätzlich
  von `cdn.jsdelivr.net`, `frame-ancestors 'none'`, `form-action 'self'`, `base-uri 'none'`,
  `object-src 'none'`
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

**Fix:** Neue Variable `youtubedl_autoupdate`, **Standard `false`**. `youtubedl_autoupdate=true`
stellt das alte Verhalten her. Aktuelles yt-dlp kommt stattdessen über neue Images, die die CI bei
jedem yt-dlp-Release automatisch baut (`docker compose pull`). Die `v<VERSION>`-Images enthalten den
Updater weiterhin gar nicht.

### C2 – App-Code gehört dem Laufzeit-User · Niedrig

**Fundstelle:** `root/etc/cont-init.d/90-user-permissions`, `chown -R abc:abc /app`.

**Befund:** Wer einmal Code als `abc` ausführt, kann `/app` (Web-UI, Download-Skript) verändern und
sich bis zur Neuerstellung des Containers festsetzen. `/home/abc/.venv` muss nur bei aktivem Updater
`abc` gehören.

**Fix:** `/app` bleibt `root`-eigen und nur lesbar. Das venv wird nur bei
`youtubedl_autoupdate=true` an `abc` übergeben, sonst `root:root`. `chown -R` auf `/config` und
`/downloads` bleibt; Hardlinks aus dem Container-Dateisystem in diese Volumes sind nicht möglich, weil
sie auf anderen Dateisystemen liegen.

### C3 – `PUID=0`/`PGID=0` · Niedrig

`usermod -o -u 0 abc` macht `abc` zu root; alle Prozesse inklusive Web-UI laufen dann als root.
**Fix:** `90-user-permissions` bricht mit klarer Meldung ab, wenn `PUID` oder `PGID` 0 ist.

### C4 – Supervisor · Info

Die Zugangsdaten für den Unix-Socket werden beim ersten Start zufällig erzeugt (`91-supervisor-credentials`).
Die Konfiguration ist für `abc` lesbar; das ist nötig, damit die Web-UI `supervisorctl restart`
ausführen kann. `abc` kann damit nur die definierten Programme steuern. Das als root laufende
`terminate` beendet supervisord, ermöglicht also nur einen DoS gegen den eigenen Container und keine
Rechteausweitung. Der Socket liegt unter `/etc/supervisor` mit Modus 0700 und gehört `abc`. Belassen.

### C5 – Build lädt ffmpeg/deno „latest“ ohne Prüfsumme · Niedrig

**Fundstelle:** `Dockerfile`.

**Befund:** ffmpeg (yt-dlp/FFmpeg-Builds bzw. johnvansickle.com) und deno werden als „latest“ ohne
Prüfsumme heruntergeladen; das Basis-Image ist nicht per Digest gepinnt. Eine Prüfsumme von derselben
Quelle schützt nur gegen Übertragungsfehler, nicht gegen ein kompromittiertes Release.

**Empfehlung (nicht umgesetzt, um Upstream-Merges nicht zu erschweren):** Versionen und SHA-256 im
`Dockerfile` pinnen und per Dependabot/Renovate aktualisieren.

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

**Fix:** Direkte und transitive Abhängigkeiten mit exakten Versionen gepinnt, `pip-audit` läuft in
der CI, Dependabot hält die Pins aktuell. yt-dlp selbst bleibt bewusst ungepinnt; die Release-Builds
pinnen es auf das jeweilige Release.

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
- Der Release-Checker committet mit `GITHUB_TOKEN` und startet die Release-Builds per
  `workflow_dispatch`. Kein PAT nötig, weil `workflow_dispatch` über `GITHUB_TOKEN` ausgelöst werden darf.
- Neuer Test-Workflow (pytest, `pip-audit`, shellcheck) für Pushes und Pull Requests.

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
- Beides geht durch `yt_dlp.parse_options()`. So werden Abkürzungen, gebündelte Kurzoptionen und
  `--alias` genauso aufgelöst wie zur Laufzeit.
- Geprüft werden die resultierenden Optionswerte. Abgelehnt werden:
  - `--exec`/`--exec-before-download` (alle Varianten), `--netrc-cmd`
  - `--plugin-dirs`, `--use-postprocessor`
  - `--config-locations`, `--batch-file`, `--load-info-json`, `--alias`
  - `--ffmpeg-location`, `--downloader` mit Pfad oder `ffmpeg`, `--downloader-args`, `--postprocessor-args`
  - `--cookies-from-browser`, `--cookies`, `--netrc-location`, `--cache-dir`
  - `--output`/`--paths`/`--print-to-file`/`--download-archive`, sofern sie aus `/downloads` hinausführen
    (`--download-archive` darf zusätzlich `/config/archive.txt` sein)
  - `--enable-file-urls`, `--load-info-json`, `--update`/`--update-to`
- Zusätzlich sind Shell-Metazeichen-Konstrukte `$(` und Backticks abgelehnt. Sie würden zwar seit S2
  nicht mehr ausgewertet, deuten aber auf alte, nun wirkungslose Konfiguration hin.

Grenzen:
1. **Denylist:** Künftige yt-dlp-Versionen können neue gefährliche Optionen einführen, die hier nicht
   erfasst sind. Der Test-Workflow prüft die Liste nur gegen die aktuell gepinnte Version.
2. **Bedeutung statt Syntax:** Erlaubte Optionen können in Kombination unerwünscht wirken, etwa
   `--match-filter`/`--download-sections` zur Ressourcenerschöpfung oder Output-Templates, die sehr
   viele Dateien erzeugen. Ausgabepfade werden vor der Template-Auswertung geprüft. Template-Felder
   wie `%(title)s` können keine Pfadtrenner einschleusen, weil yt-dlp sie sanitisiert; Templates mit
   `..` als festem Teil werden abgelehnt.
3. **API-Kopplung:** `yt_dlp.parse_options` ist keine stabil dokumentierte API. Ändert sich ihr
   Verhalten, schlägt die Prüfung fehl und das Speichern wird abgelehnt (fail closed), statt still
   durchzulassen.
4. **Andere Schreibwege:** Die Prüfung greift nur beim Speichern über die Web-UI. Wer `/config` direkt
   beschreiben kann (Synology-Freigabe, `docker exec`, `docker cp`), umgeht sie; das ist gewollt.
5. **Nicht geprüft:** `archive.txt` enthält nur IDs und wird nicht interpretiert.

## Verbleibende Empfehlungen

- Web-UI nur über HTTPS erreichbar machen (DSM-Reverse-Proxy mit Zertifikat) und den Container-Port nur
  an `127.0.0.1` binden, siehe Compose-Beispiel im README.
- `WEBUI_ALLOWED_DOMAINS` setzen, wenn nur YouTube heruntergeladen wird.
- `WEBUI_READONLY=true`, wenn die Config nur selten geändert wird.
- C5 (Pins und Hashes für ffmpeg/deno/Basis-Image) bei Gelegenheit umsetzen.
